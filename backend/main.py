import uuid
import logging
import io
import qrcode
import base64
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from config import settings
from database import (
    init_db, create_order, get_order, save_platega_tx,
    mark_paid, save_subscription, get_order_by_tx,
)
from platega_client import create_payment, check_status, PlategaError
from xui_client import create_client, get_sub_links, XuiError

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    logger.info("Database initialized")
    yield


app = FastAPI(title="VPN Shop", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # в проде укажите свой домен
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ————————————————— МОДЕЛИ —————————————————

class CreateOrderRequest(BaseModel):
    tariff: str  # "month" | "quarter" | "year"


# ————————————————— ЭНДПОИНТЫ API —————————————————

@app.get("/api/tariffs")
async def list_tariffs():
    """Список доступных тарифов — для витрины."""
    return [
        {"slug": slug, "days": t["days"], "price": t["price"], "title": t["title"]}
        for slug, t in settings.tariffs.items()
    ]


@app.post("/api/order/create")
async def api_create_order(req: CreateOrderRequest):
    """
    Шаг 1: пользователь выбирает тариф.
    Создаёт заказ в БД и платёжную ссылку в Platega.
    """
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

    return {
        "order_id": order_id,
        "payment_url": payment["payment_url"],
        "amount": tariff["price"],
    }


@app.get("/api/order/{order_id}/status")
async def api_order_status(order_id: str):
    """
    Витрина опрашивает этот эндпоинт после редиректа с оплаты.
    Возвращает sub-ссылку, если ключ уже создан.
    """
    order = await get_order(order_id)
    if not order:
        raise HTTPException(404, "Заказ не найден")

    # Если ещё не оплачен — проверяем через Platega (polling fallback)
    if order["status"] != "paid" and order.get("platega_tx_id"):
        try:
            status = await check_status(order["platega_tx_id"])
            if status == "succeeded":
                await fulfill_order(order)
                order = await get_order(order_id)  # перечитываем
        except PlategaError as e:
            logger.error("Polling error: %s", e)

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

    Если webhook не настроен — используется polling (см. ниже).
    """
    try:
        body = await request.json()
    except Exception:
        body = {}

    logger.info("Platega webhook received: %s", body)

    tx_id = body.get("id") or body.get("transactionId")
    status = str(body.get("status", "")).upper()
    order_id = body.get("payload") or ""

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

    await fulfill_order(order)

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

async def fulfill_order(order: dict):
    """
    Создаёт клиента в 3x-UI после успешной оплаты.
    Идемпотентно: если sub_url уже есть — ничего не делает.
    """
    if order.get("sub_url"):
        logger.info("Order %s already fulfilled", order["id"])
        return

    if order.get("status") != "paid":
        await mark_paid(order["id"])

    tariff = settings.tariffs.get(order["tariff"])
    days = tariff["days"] if tariff else 30

    email = f"web-{order['id']}@vpn.local"

    try:
        # 1. Создаём клиента
        result = await create_client(
            email=email,
            duration_days=days,
        )
        # 2. Получаем sub-ссылку
        sub = await get_sub_links(result["sub_id"])

        await save_subscription(order["id"], email, result["sub_id"], sub["sub_url"])
        logger.info(
            "Order %s fulfilled: email=%s sub=%s",
            order["id"], email, sub["sub_url"][:50],
        )

    except XuiError as e:
        logger.error("3x-UI error for order %s: %s", order["id"], e)
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

            <div class="bg-yellow-50 border-l-4 border-yellow-500 p-4">
                <p class="text-sm text-yellow-700">
                    <strong>Как использовать:</strong><br>
                    1. Скопируйте ссылку подписки<br>
                    2. Откройте v2rayNG / Hiddify / V2Box / Streisand<br>
                    3. Добавьте подписку → вставьте ссылку<br>
                    4. Обновите серверы и подключайтесь!
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
            html = html.replace(f"{{{{ {key} }}}}", str(value))
        return html


HTML_TEMPLATE = HTMLTemplate()
