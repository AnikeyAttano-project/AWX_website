import uuid
import logging
import io
import hmac
import hashlib
import html
import json
import qrcode
import base64
import aiosqlite
from contextlib import asynccontextmanager
from collections import defaultdict
from datetime import datetime, timedelta

from fastapi import FastAPI, Request, HTTPException, APIRouter, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from config import settings
from database import (
    init_db, create_order, get_order, save_platega_tx,
    mark_paid, save_subscription, get_order_by_tx, mark_order_error,
    get_user_subscriptions, get_user_order, set_order_user,
)
from platega_client import create_payment, check_status, PlategaError
from xui_client import (
    create_client, get_subscription_url, _parse_inbound_ids,
    get_sub_links, renew_client, check_client_status,
    XuiError,
)
from admin import admin_router
from auth import auth_router, get_optional_user

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Rate limiting — хранилище запросов по IP
rate_limit_storage = defaultdict(list)


def check_rate_limit(ip: str, max_requests: int = 10, window_minutes: int = 60) -> bool:
    """
    Проверяет rate limit для IP адреса.
    Возвращает True если запрос разрешён, False если превышен лимит.
    """
    now = datetime.now()
    window_start = now - timedelta(minutes=window_minutes)

    # Очищаем старые записи
    rate_limit_storage[ip] = [
        req_time for req_time in rate_limit_storage[ip]
        if req_time > window_start
    ]

    # Проверяем лимит
    if len(rate_limit_storage[ip]) >= max_requests:
        return False

    # Записываем новый запрос
    rate_limit_storage[ip].append(now)
    return True


def verify_platega_signature(body: bytes, signature: str) -> bool:
    """
    Проверяет подпись webhook от Platega через HMAC-SHA256.
    Platega отправляет подпись в заголовке X-Signature.
    """
    if not settings.platega_secret:
        logger.error("PLATEGA_SECRET not set — webhook rejected")
        return False

    expected = hmac.new(
        settings.platega_secret.encode(),
        body,
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(expected, signature)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    logger.info("Database initialized")
    yield


app = FastAPI(title="VPN Shop", lifespan=lifespan)

# CORS: разрешаем только указанные домены (в .env ALLOWED_ORIGINS)
try:
    allowed_origins = json.loads(settings.allowed_origins)
except (AttributeError, json.JSONDecodeError):
    allowed_origins = ["http://localhost:3000", "http://localhost:8000"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
    allow_credentials=True,
)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    """Добавляет заголовки безопасности (CSP) к HTML-ответам."""
    response = await call_next(request)
    if response.headers.get("content-type", "").startswith("text/html"):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' https://cdn.tailwindcss.com 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "frame-ancestors 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
    return response


# ————————————————— АДМИН ПАНЕЛЬ —————————————————
# Маршруты /admin/* — защищены заголовком X-Admin-Key (см. admin.py)
app.include_router(admin_router)

# ————————————————— АВТОРИЗАЦИЯ —————————————————
# Маршруты /api/auth/* — register, login, me
app.include_router(auth_router)

# ————————————————— ЛИЧНЫЙ КАБИНЕТ —————————————————
# Маршруты /api/account/* — подписки, ключи, продление
account_router = APIRouter(prefix="/api/account", tags=["account"])


@account_router.get("/subscriptions")
async def get_subscriptions(user: dict = Depends(get_optional_user)):
    """Список подписок пользователя. Требует авторизации."""
    if not user:
        raise HTTPException(401, "Требуется авторизация")
    orders = await get_user_subscriptions(user["id"])
    return [
        {
            "order_id": o["id"],
            "tariff": o["tariff"],
            "status": o["status"],
            "sub_url": o.get("sub_url"),
            "expires_at": o.get("expires_at"),
            "created_at": o["created_at"],
        }
        for o in orders
    ]


@account_router.get("/subscription/{order_id}")
async def get_subscription_detail(order_id: str, user: dict = Depends(get_optional_user)):
    """Детали подписки + QR-код. Требует авторизации."""
    if not user:
        raise HTTPException(401, "Требуется авторизация")
    order = await get_user_order(user["id"], order_id)
    if not order:
        raise HTTPException(404, "Подписка не найдена")
    result = {
        "order_id": order["id"],
        "tariff": order["tariff"],
        "status": order["status"],
        "sub_url": order.get("sub_url"),
        "expires_at": order.get("expires_at"),
        "created_at": order["created_at"],
    }
    if order.get("sub_url"):
        result["qr_base64"] = _make_qr_base64(order["sub_url"])
    return result


@account_router.post("/renew/{order_id}")
async def renew_subscription(order_id: str, user: dict = Depends(get_optional_user)):
    """Продление подписки. Требует авторизации."""
    if not user:
        raise HTTPException(401, "Требуется авторизация")
    order = await get_user_order(user["id"], order_id)
    if not order:
        raise HTTPException(404, "Подписка не найдена")
    if not order.get("xui_email"):
        raise HTTPException(400, "Нет привязки к 3x-UI клиенту")

    tariff = settings.tariffs.get(order["tariff"])
    days = tariff["days"] if tariff else 30

    try:
        result = await renew_client(order["xui_email"], days)
        # Обновляем expires_at в БД
        new_expires = datetime.utcfromtimestamp(result["new_expiry_ms"] / 1000).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        async with aiosqlite.connect(settings.database_path) as db:
            await db.execute(
                "UPDATE orders SET expires_at = ? WHERE id = ?",
                (new_expires, order_id),
            )
            await db.commit()
        return {"ok": True, "new_expires_at": new_expires}
    except XuiError as e:
        logger.error("Renew failed for order %s: %s", order_id, e)
        raise HTTPException(500, f"Ошибка продления: {e}")


app.include_router(account_router)


# ————————————————— МОДЕЛИ —————————————————

class CreateOrderRequest(BaseModel):
    tariff: str  # "quantum_month" | "quantum_quarter" | "quantum_halfyear" | "quantum_year"


# ————————————————— ЭНДПОИНТЫ API —————————————————

@app.get("/api/tariffs")
async def list_tariffs():
    """Список доступных тарифов — для витрины."""
    return [
        {"slug": slug, "days": t["days"], "price": t["price"], "title": t["title"]}
        for slug, t in settings.tariffs.items()
    ]


@app.post("/api/order/create")
async def api_create_order(req: CreateOrderRequest, request: Request, user: dict = Depends(get_optional_user)):
    """
    Шаг 1: пользователь выбирает тариф.
    Создаёт заказ в БД и платёжную ссылку в Platega.
    Если пользователь авторизован — привязывает заказ к аккаунту.
    """
    # Rate limiting: 10 заказов в час с одного IP
    client_ip = request.client.host
    if not check_rate_limit(client_ip, max_requests=10, window_minutes=60):
        logger.warning("Rate limit exceeded for IP: %s", client_ip)
        raise HTTPException(429, "Слишком много запросов. Попробуйте позже.")

    tariff = settings.tariffs.get(req.tariff)
    if not tariff:
        raise HTTPException(400, "Неизвестный тариф")

    order_id = uuid.uuid4().hex[:12]
    await create_order(order_id, req.tariff, tariff["price"])

    try:
        payment = await create_payment(
            amount=tariff["price"],
            order_id=order_id,
            description=f"VPN подписка {tariff['title']} ({tariff['days']} дней)"
        )
    except PlategaError as e:
        logger.error("Platega error: %s", e)
        raise HTTPException(502, str(e))

    await save_platega_tx(order_id, payment["transaction_id"])

    # Привязываем заказ к пользователю, если авторизован
    if user:
        await set_order_user(order_id, user["id"])

    return {
        "order_id": order_id,
        "payment_url": payment["payment_url"],
        "amount": tariff["price"],
    }


@app.post("/api/order/demo")
async def api_demo_order(req: CreateOrderRequest, request: Request, user: dict = Depends(get_optional_user)):
    """
    ДЕМО-оплата — создает заказ и сразу выдаёт ключ без реальной оплаты.
    Используйте только для тестирования!
    """
    tariff = settings.tariffs.get(req.tariff)
    if not tariff:
        raise HTTPException(400, "Неизвестный тариф")

    order_id = uuid.uuid4().hex[:12]
    await create_order(order_id, req.tariff, tariff["price"])

    # Привязываем к пользователю
    if user:
        await set_order_user(order_id, user["id"])

    # Сразу помечаем как оплаченный
    await mark_paid(order_id)

    # Сразу выдаём ключ
    try:
        await fulfill_order(order_id)
    except Exception as e:
        logger.error("Demo fulfill error: %s", e)

    logger.info("Demo order created: %s (tariff: %s)", order_id, req.tariff)

    return {
        "order_id": order_id,
        "payment_url": f"{settings.site_base_url}/payment/success?order_id={order_id}",
        "amount": 0,
        "demo": True,
    }


@app.get("/api/order/{order_id}/status")
async def api_order_status(order_id: str, request: Request):
    """
    Витрина опрашивает этот эндпоинт после редиректа с оплаты.
    Возвращает sub-ссылку, если ключ уже создан.
    """
    # Rate limiting: 30 запросов в минуту с одного IP
    client_ip = request.client.host
    if not check_rate_limit(client_ip, max_requests=30, window_minutes=1):
        logger.warning("Rate limit exceeded for status endpoint, IP: %s", client_ip)
        raise HTTPException(429, "Слишком много запросов. Попробуйте позже.")

    order = await get_order(order_id)
    if not order:
        raise HTTPException(404, "Заказ не найден")

    # Case 1: paid but no key created (3x-UI error) → retry
    if order["status"] == "paid" and not order.get("sub_url"):
        try:
            await fulfill_order(order_id)
            order = await get_order(order_id)
        except Exception as e:
            logger.error("Retry failed for order %s: %s", order_id, e)

    # Case 2: not yet paid → check Platega (polling fallback)
    elif order["status"] != "paid" and order.get("platega_tx_id"):
        try:
            platega_status = await check_status(order["platega_tx_id"])
            logger.info("Polling order %s: tx=%s status=%s", order_id, order["platega_tx_id"], platega_status)
            if platega_status == "succeeded":
                await fulfill_order(order_id)
                order = await get_order(order_id)
        except PlategaError as e:
            logger.error("Polling error for order %s: %s", order_id, e)

    response = {
        "order_id": order["id"],
        "status": order["status"],
        "tariff": order["tariff"],
    }

    if order.get("sub_url"):
        response["sub_url"] = order["sub_url"]
        response["qr_base64"] = _make_qr_base64(order["sub_url"])

    return response


@app.post("/webhook/platega")
async def platega_webhook(request: Request):
    """
    Webhook от Platega об изменении статуса транзакции.
    Platega шлёт JSON с полями: id, status, payload (наш order_id).
    """
    body = await request.body()
    signature = request.headers.get("X-Signature", "")

    # Проверяем подпись (если настроен PLATEGA_SECRET)
    if not verify_platega_signature(body, signature):
        logger.warning("Invalid webhook signature")
        raise HTTPException(403, "Invalid signature")

    try:
        body_json = json.loads(body)
    except Exception:
        body_json = {}

    logger.info("Platega webhook received: %s", body_json)

    tx_id = body_json.get("id") or body_json.get("transactionId")
    status = str(body_json.get("status", "")).upper()
    order_id = body_json.get("payload") or ""

    if not tx_id:
        return {"ok": False, "error": "no transaction id"}

    # Находим заказ — либо по payload (order_id), либо по tx_id
    order = await get_order(order_id) if order_id else None
    if not order:
        order = await get_order_by_tx(tx_id)
    if not order:
        logger.warning("Order not found for tx=%s", tx_id)
        return {"ok": False, "error": "order not found"}

    # Проверяем реальный статус через API (двойная проверка)
    real_status = await check_status(tx_id)
    if real_status != "succeeded":
        logger.info("Payment not confirmed: tx=%s status=%s", tx_id, real_status)
        return {"ok": True, "msg": "not confirmed yet"}

    await fulfill_order(order["id"])

    return {"ok": True}


@app.get("/payment/success", response_class=HTMLResponse)
async def payment_success(order_id: str):
    """Страница после успешной оплаты — показывает sub-ссылку и QR."""
    return HTML_TEMPLATE.render(order_id=order_id)


@app.get("/payment/failed", response_class=HTMLResponse)
async def payment_failed(order_id: str):
    """Страница после неудачной оплаты."""
    return "<html><body><h2>Оплата не удалась</h2><p>Попробуйте снова.</p></body></html>"


# ————————————————— УТИЛИТЫ —————————————————

async def fulfill_order(order_id: str):
    """
    Создаёт клиента в 3x-UI после успешной оплаты.
    КЛЮЧЕВОЙ МОМЕНТ — создаём клиента сразу во ВСЕХ видимых инбаундах.
    Идемпотентно: если sub_url уже есть — ничего не делает.
    """
    order = await get_order(order_id)

    if order.get("sub_url"):
        logger.info("Order %s already fulfilled", order_id)
        return

    if order.get("status") != "paid":
        await mark_paid(order_id)

    tariff = settings.tariffs.get(order["tariff"])
    days = tariff["days"] if tariff else 30
    devices = tariff["devices"] if tariff else 1

    # Уникальный email на основе тарифа и номера заказа
    email = f"{order['tariff']}-{order_id}@vpn.local"

    try:
        # КЛЮЧЕВОЙ МОМЕНТ —
        # Создаём клиента ВО ВСЕХ видимых инбаундах
        # limit_ip = кол-во устройств, разрешённых тарифом
        client_data = await create_client(
            email=email,
            duration_days=days,
            limit_ip=devices,
        )

        # Получаем URL подписки
        sub_url = await get_subscription_url(client_data["sub_id"])

        # Сохраняем в БД
        expires_at = (datetime.utcnow() + timedelta(days=days)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        await save_subscription(
            order_id, email, client_data["sub_id"], sub_url,
            inbound_ids=",".join(str(x) for x in _parse_inbound_ids()),
            expires_at=expires_at,
        )
        logger.info(
            "Order %s fulfilled: email=%s sub=%s",
            order_id, email, sub_url[:50],
        )

    except XuiError as e:
        # Логируем ошибку, заказ остаётся в статусе 'paid'
        # но без выданного ключа — нужна ручная проверка
        logger.error("Failed to create client for order %s: %s", order_id, e)
        await mark_order_error(order_id, str(e))
        return


def _make_qr_base64(data: str) -> str:
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(data)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return base64.b64encode(buf.read()).decode()


# ————————————————— HTML ШАБЛОН —————————————————

HTML_TEMPLATE_STR = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Спасибо за покупку!</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-100 min-h-screen flex items-center justify-center p-4">
    <div class="bg-white rounded-lg shadow-lg p-8 max-w-2xl w-full">
        <h1 class="text-3xl font-bold text-green-600 mb-4">✅ Спасибо за покупку!</h1>
        <p class="text-gray-700 mb-6">Ваш ключ готовится...</p>

        <div id="status" class="mb-6">
            <div class="animate-pulse flex space-x-4">
                <div class="flex-1 space-y-4 py-1">
                    <div class="h-4 bg-gray-300 rounded w-3/4"></div>
                    <div class="h-4 bg-gray-300 rounded"></div>
                </div>
            </div>
        </div>

        <div id="result" class="hidden">
            <div class="bg-blue-50 border-l-4 border-blue-500 p-4 mb-4">
                <p class="text-sm text-blue-700">
                    <strong>Ваша подписка:</strong><br>
                    <code id="sub_url" class="bg-white px-2 py-1 rounded text-xs break-all"></code>
                </p>
            </div>

            <div class="flex justify-center mb-4">
                <img id="qr_code" src="" alt="QR Code" class="border-4 border-gray-200 rounded">
            </div>

            <div class="bg-yellow-50 border-l-4 border-yellow-500 p-4 mb-4">
                <p class="text-sm text-yellow-700">
                    <strong>⚠️ Важно:</strong> Если вы видите только одну ссылку — при импорте в приложение
                    (v2rayNG, Hiddify, V2Ray Tun) появятся все 8 серверов.
                </p>
            </div>

            <div class="bg-green-50 border-l-4 border-green-500 p-4">
                <p class="text-sm text-green-700">
                    <strong>📱 Инструкция:</strong> Скопируйте ссылку и вставьте в приложение для VPN.
                </p>
            </div>
        </div>
    </div>

    <script>
        const orderId = "{{ order_id }}";
        let attempts = 0;
        const maxAttempts = 30;

        async function checkStatus() {
            attempts++;
            try {
                const resp = await fetch(`/api/order/${orderId}/status`);
                const data = await resp.json();

                if (data.sub_url) {
                    document.getElementById("sub_url").textContent = data.sub_url;
                    document.getElementById("qr_code").src = "data:image/png;base64," + data.qr_base64;
                    document.getElementById("status").classList.add("hidden");
                    document.getElementById("result").classList.remove("hidden");
                } else if (attempts < maxAttempts) {
                    setTimeout(checkStatus, 2000);
                } else {
                    document.getElementById("status").innerHTML =
                        '<p class="text-red-600">⏱ Ключ задерживается. Попробуйте обновить страницу через минуту.</p>';
                }
            } catch (e) {
                console.error(e);
                if (attempts < maxAttempts) {
                    setTimeout(checkStatus, 2000);
                }
            }
        }

        checkStatus();
    </script>
</body>
</html>
"""


class HTMLTemplate:
    def render(self, **kwargs):
        html = HTML_TEMPLATE_STR
        for key, value in kwargs.items():
            # Escape for JavaScript string literal (produces a safe JS string with quotes)
            escaped = json.dumps(str(value))
            # Дополнительная защита для inline <script>:
            # JSON не экранирует <, >, & — заменяем на \uXXXX, чтобы злоумышленник
            # не смог выйти из строки через "</script>".
            escaped = (
                escaped.replace("<", "\\u003c")
                .replace(">", "\\u003e")
                .replace("&", "\\u0026")
            )
            html = html.replace(f"{{{{ {key} }}}}", escaped)
        return html


HTML_TEMPLATE = HTMLTemplate()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
