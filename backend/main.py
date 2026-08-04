import uuid
import logging
import io
import hmac
import hashlib
import html
import json
import math
import asyncio
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
    _db,
    init_db, load_runtime_settings, create_order, get_order, save_platega_tx,
    mark_paid, save_subscription, get_order_by_tx, mark_order_error,
    get_user_subscriptions, get_user_order, set_order_user,
    claim_trial, set_order_custom_name, mark_order_deleted,
    get_user_by_referral_code, apply_referral_code, ensure_referral_code,
    count_referrals, sum_reward_days, get_referral_list,
    get_referral_levels, get_user_referrer, get_active_subscription,
    add_referral_reward, get_setting, get_user_by_id,
    cleanup_expired_orders, cleanup_expired_trials, delete_order,
    create_device_addon, get_device_addons_for_order, get_active_addon_for_order,
    activate_addon, cancel_pending_addon, finalize_addon_cancellation,
    get_addon_by_tx, get_addon_by_id, get_total_extra_devices,
)
from platega_client import create_payment, check_status, PlategaError
from pricing import compute_addon_proration
from xui_client import (
    create_client, get_subscription_url, _parse_inbound_ids,
    update_client_limit,
    get_sub_links, renew_client, check_client_status,
    delete_client, rekey_client,
    XuiError,
)
from admin import admin_router
from admin_debug import admin_debug_router
from auth import auth_router, get_optional_user, require_verified_email, get_current_user

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Rate limiting — хранилище запросов по IP
rate_limit_storage = defaultdict(list)

# Locks для fulfill_order — предотвращение гонки при параллельных webhook/polling/retry
_fulfill_locks: dict[str, asyncio.Lock] = {}
_fulfill_locks_lock = asyncio.Lock()  # Защита самого словаря локов


def get_real_ip(request: Request) -> str:
    """Извлекает реальный IP клиента из proxy-заголовков (X-Forwarded-For / X-Real-IP)."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        # X-Forwarded-For: client, proxy1, proxy2 — берём первый (реальный IP)
        return xff.split(",")[0].strip()
    xri = request.headers.get("x-real-ip", "")
    if xri:
        return xri.strip()
    return request.client.host if request.client else "0.0.0.0"


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


def verify_platega_webhook(headers: dict) -> bool:
    """
    Проверяет webhook от Platega через заголовки X-MerchantId + X-Secret.
    Platega использует те же заголовки что и для API запросов.
    """
    if not settings.platega_secret:
        logger.error("PLATEGA_SECRET not set — webhook rejected")
        return False

    merchant_id = headers.get("x-merchantid", "")
    secret = headers.get("x-secret", "")
    return (merchant_id == settings.platega_merchant_id and
            secret == settings.platega_secret)


async def cleanup_expired_subscriptions():
    """Очистка устаревших подписок и триалов (>14 дней после окончания)."""
    # 1. Удаляем устаревшие заказы
    expired = await cleanup_expired_orders(grace_days=14)
    deleted_count = 0
    for order in expired:
        # Удаляем клиента из 3x-UI (если есть)
        if order.get("xui_email"):
            try:
                await delete_client(order["xui_email"])
            except XuiError as e:
                msg = str(e).lower()
                if "not found" not in msg and "not exist" not in msg:
                    logger.warning("Cleanup: failed to delete 3x-UI client for %s: %s", order["id"], e)
        # Физически удаляем из БД
        await delete_order(order["id"])
        deleted_count += 1

    # 2. Сбрасываем устаревшие триалы
    trials_reset = await cleanup_expired_trials(grace_days=14)

    if deleted_count or trials_reset:
        logger.info("Cleanup: deleted %d orders, reset %d trials", deleted_count, trials_reset)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await load_runtime_settings()
    logger.info("Database initialized, runtime settings loaded")

    # Запускаем фоновую очистку устаревших записей (каждые 6 часов)
    async def periodic_cleanup():
        while True:
            await asyncio.sleep(6 * 3600)  # 6 часов
            try:
                await cleanup_expired_subscriptions()
            except Exception as e:
                logger.error("Periodic cleanup error: %s", e)

    cleanup_task = asyncio.create_task(periodic_cleanup())
    logger.info("Periodic cleanup task started (every 6 hours)")

    yield

    cleanup_task.cancel()
    # Закрываем persistent httpx client для 3x-UI
    from xui_client import close_http_client
    await close_http_client()


app = FastAPI(title="VPN Shop", lifespan=lifespan)

# CORS: разрешаем только указанные домены (в .env ALLOWED_ORIGINS)
try:
    allowed_origins = json.loads(settings.allowed_origins)
except (AttributeError, json.JSONDecodeError):
    allowed_origins = ["http://localhost:3000", "http://localhost:8000"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_methods=["GET", "POST", "DELETE"],
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
app.include_router(admin_debug_router)

# ————————————————— АВТОРИЗАЦИЯ —————————————————
# Маршруты /api/auth/* — register, login, me
app.include_router(auth_router)

# ————————————————— ЛИЧНЫЙ КАБИНЕТ —————————————————
# Маршруты /api/account/* — подписки, ключи, продление
account_router = APIRouter(prefix="/api/account", tags=["account"])


class RenameSubscriptionRequest(BaseModel):
    name: str


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
            "custom_name": o.get("custom_name"),
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
        "custom_name": order.get("custom_name"),
    }
    if order.get("sub_url"):
        result["qr_base64"] = _make_qr_base64(order["sub_url"])
    return result


@account_router.post("/renew/{order_id}")
async def renew_subscription(order_id: str, request: Request, user: dict = Depends(get_optional_user)):
    """
    Продление подписки. Защита от злоупотреблений:
    - Требует авторизации
    - Rate-limit: 1 продление в 24 часа на пользователя
    - Подписка должна быть активной (не удалённой)
    - Продление разрешено только если прошло ≥70% от срока подписки
    """
    if not user:
        raise HTTPException(401, "Требуется авторизация")
    order = await get_user_order(user["id"], order_id)
    if not order:
        raise HTTPException(404, "Подписка не найдена")
    if order.get("status") == "deleted":
        raise HTTPException(400, "Нельзя продлить удалённую подписку")
    if not order.get("xui_email"):
        raise HTTPException(400, "Нет привязки к 3x-UI клиенту")

    # Сначала проверяем 70% срока (быстрый отказ, НЕConsuming rate-limit)
    if order.get("expires_at"):
        try:
            expires_at = datetime.strptime(order["expires_at"], "%Y-%m-%d %H:%M:%S")
            tariff = settings.tariffs.get(order["tariff"])
            total_days = tariff["days"] if tariff else 30
            now = datetime.utcnow()
            order_created = expires_at - timedelta(days=total_days)
            elapsed = (now - order_created).total_seconds()
            required = total_days * 86400 * 0.7  # 70% от срока
            if elapsed < required:
                remaining_pct = int((1 - elapsed / (total_days * 86400)) * 100)
                raise HTTPException(
                    400,
                    f"Продление доступно после использования 70% подписки. "
                    f"Осталось {remaining_pct}% срока. Попробуйте позже."
                )
        except ValueError:
            pass  # Если дата некорректна — пропускаем проверку

    # Rate-limit: 1 продление в 24 часа на пользователя (только после прохождения 70%)
    renew_key = f"renew:{user['id']}"
    if not check_rate_limit(renew_key, max_requests=1, window_minutes=1440):
        logger.warning("Renew rate limit exceeded for user: %s", user["id"])
        raise HTTPException(429, "Продление доступно не чаще 1 раза в сутки. Попробуйте позже.")

    return await _renew_subscription_core(order_id, order)


async def _renew_subscription_core(order_id: str, order: dict) -> dict:
    """
    Внутренняя логика продления подписки (БЕЗ проверок доступа/70%/rate-limit).

    Выделена из renew_subscription, чтобы и HTTP-эндпоинт, и дебаг-симулятор
    /admin/debug/billing/simulate-renew звали одну и ту же логику (не дублировать).
    Проверки, которые должны быть только у пользовательского пути, остаются в
    renew_subscription; сюда они намеренно НЕ попадают.
    """
    tariff = settings.tariffs.get(order["tariff"])
    days = tariff["days"] if tariff else 30

    # Баг4: Обрабатываем cancel_pending add-ons ДО продления
    # Считаем правильный лимит: base + все активные кроме отменяемого
    pending_cancels = [a for a in await get_device_addons_for_order(order_id)
                       if a["status"] == "cancel_pending"]
    if pending_cancels:
        base_devices = tariff.get("devices", 5) if tariff else 5
        # Суммируем все active + cancel_pending extras
        all_addons = await get_device_addons_for_order(order_id)
        total_with_cancel = sum(a["extra_devices"] for a in all_addons
                               if a["status"] in ("active", "cancel_pending"))
        # Вычитаем отменяемые
        cancelled_extras = sum(a["extra_devices"] for a in pending_cancels)
        new_limit = base_devices + total_with_cancel - cancelled_extras
        try:
            await update_client_limit(order["xui_email"], max(base_devices, new_limit))
        except XuiError as e:
            logger.warning("Addon cancel during renew: failed to update limit: %s", e)
        for a in pending_cancels:
            await finalize_addon_cancellation(a["id"])
            logger.info("Addon cancelled at renewal: id=%s order=%s", a["id"], order_id)

    try:
        result = await renew_client(order["xui_email"], days)
        # Обновляем expires_at в БД
        new_expires = datetime.utcfromtimestamp(result["new_expiry_ms"] / 1000).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        async with _db() as db:
            await db.execute(
                "UPDATE orders SET expires_at = ? WHERE id = ?",
                (new_expires, order_id),
            )
            await db.commit()
        logger.info("Subscription renewed: order=%s user=%s days=%d",
                    order_id, order.get("user_id"), days)
        return {"ok": True, "new_expires_at": new_expires}
    except XuiError as e:
        logger.error("Renew failed for order %s: %s", order_id, e)
        error_msg = str(e)
        if "not found" in error_msg.lower() or "not exist" in error_msg.lower():
            raise HTTPException(404, "Клиент не найден в 3x-UI. Возможно, подписка была деактивирована. Попробуйте перевыпустить ключ.")
        raise HTTPException(500, f"Ошибка продления: {e}")


@account_router.post("/subscription/{order_id}/rekey")
async def rekey_subscription(order_id: str, user: dict = Depends(require_verified_email)):
    """Перевыпуск ключа: новый sub_id и sub_url. Требует авторизации."""
    order = await get_user_order(user["id"], order_id)
    if not order:
        raise HTTPException(404, "Подписка не найдена")
    if not order.get("xui_email") or not order.get("xui_sub_id"):
        raise HTTPException(400, "Нет привязки к 3x-UI клиенту")

    tariff = settings.tariffs.get(order["tariff"])
    days = tariff["days"] if tariff else 30
    devices = tariff["devices"] if tariff else 1

    # Сохраняем текущую дату истечения (перевыпуск НЕ продлевает подписку)
    expiry_ms = None
    if order.get("expires_at"):
        try:
            parsed = datetime.strptime(order["expires_at"], "%Y-%m-%d %H:%M:%S")
            if parsed > datetime.utcnow():
                expiry_ms = int(parsed.timestamp() * 1000)
        except ValueError:
            pass
    if expiry_ms is None:
        expiry_ms = int((datetime.utcnow() + timedelta(days=days)).timestamp() * 1000)

    # Новый уникальный email клиента в 3x-UI
    new_email = f"rekey-{order_id}-{uuid.uuid4().hex[:4]}@vpn.local"

    try:
        result = await rekey_client(
            old_email=order["xui_email"],
            new_email=new_email,
            expiry_ms=expiry_ms,
            limit_ip=devices,
        )
        sub_url = await get_subscription_url(result["sub_id"])
        expires_at = datetime.utcfromtimestamp(expiry_ms / 1000).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        await save_subscription(
            order_id, result["email"], result["sub_id"], sub_url,
            inbound_ids=",".join(str(x) for x in _parse_inbound_ids()),
            expires_at=expires_at,
        )
        logger.info("Rekey: order %s new sub_id=%s", order_id, result["sub_id"])
        return {
            "ok": True,
            "order_id": order_id,
            "email": result["email"],
            "sub_id": result["sub_id"],
            "sub_url": sub_url,
            "expires_at": expires_at,
            "qr_base64": _make_qr_base64(sub_url),
        }
    except XuiError as e:
        logger.error("Rekey failed for order %s: %s", order_id, e)
        raise HTTPException(502, f"Ошибка перевыпуска ключа: {e}")


@account_router.post("/subscription/{order_id}/rename")
async def rename_subscription(
    order_id: str,
    req: RenameSubscriptionRequest,
    user: dict = Depends(require_verified_email),
):
    """Переименование подписки (пользовательское имя). Требует авторизации."""
    name = req.name.strip()
    if not name:
        raise HTTPException(400, "Имя подписки не может быть пустым")
    if len(name) > 100:
        raise HTTPException(400, "Имя подписки слишком длинное (максимум 100 символов)")

    order = await get_user_order(user["id"], order_id)
    if not order:
        raise HTTPException(404, "Подписка не найдена")

    await set_order_custom_name(order_id, name)
    logger.info("Order %s renamed to %r by user %s", order_id, name, user["id"])
    return {"ok": True, "order_id": order_id, "custom_name": name}


@account_router.get("/subscription/{order_id}/stats")
async def get_subscription_stats(order_id: str, user: dict = Depends(get_optional_user)):
    """Статистика ключа: трафик, даты, статус. Требует авторизации."""
    if not user:
        raise HTTPException(401, "Требуется авторизация")
    order = await get_user_order(user["id"], order_id)
    if not order:
        raise HTTPException(404, "Подписка не найдена")
    if not order.get("xui_email"):
        raise HTTPException(400, "Нет привязки к 3x-UI клиенту")

    try:
        status = await check_client_status(order["xui_email"])
    except XuiError as e:
        logger.error("Stats failed for order %s: %s", order_id, e)
        raise HTTPException(502, f"Ошибка получения статистики: {e}")

    used_bytes = int(status.get("up", 0) or 0) + int(status.get("down", 0) or 0)
    total_bytes = int(status.get("total_gb", 0) or 0)
    expiry_ms = int(status.get("expiry_ms", 0) or 0)
    now_ms = int(datetime.utcnow().timestamp() * 1000)

    if status.get("enable") is False:
        client_state = "disabled"
    elif expiry_ms and expiry_ms < now_ms:
        client_state = "expired"
    else:
        client_state = "active"

    return {
        "ok": True,
        "order_id": order_id,
        "email": status["email"],
        "state": client_state,
        "enable": status.get("enable"),
        "used_bytes": used_bytes,
        "used_gb": round(used_bytes / 1073741824, 2),
        "total_bytes": total_bytes,
        "total_gb": round(total_bytes / 1073741824, 2) if total_bytes else None,
        "created_at": order.get("created_at"),
        "expires_at": order.get("expires_at"),
        "expiry_ms": expiry_ms,
        "status": order.get("status"),
    }


@account_router.delete("/subscription/{order_id}")
async def delete_subscription(order_id: str, user: dict = Depends(require_verified_email)):
    """Удаление подписки: удаляет клиента из 3x-UI, помечает заказ deleted."""
    order = await get_user_order(user["id"], order_id)
    if not order:
        raise HTTPException(404, "Подписка не найдена")

    if order.get("xui_email"):
        try:
            await delete_client(order["xui_email"])
        except XuiError as e:
            msg = str(e).lower()
            if "not found" in msg or "not exist" in msg:
                logger.info("Delete client for order %s already gone: %s", order_id, e)
            else:
                logger.error("Delete client failed for order %s: %s", order_id, e)
                raise HTTPException(502, f"Ошибка удаления ключа из панели: {e}")

    await mark_order_deleted(order_id)
    logger.info("Subscription %s deleted by user %s", order_id, user["id"])
    return {"ok": True, "order_id": order_id}


@account_router.get("/trial")
async def get_trial_status(user: dict = Depends(get_optional_user)):
    """Статус пробного периода для текущего пользователя."""
    if not user:
        return {"status": "unavailable", "message": "Требуется авторизация"}
    if not settings.trial_enabled:
        return {"status": "unavailable", "message": "Пробный период отключён"}

    trial_started = user.get("trial_started_at")
    trial_expires = user.get("trial_expires_at")

    if not trial_started:
        return {
            "status": "available",
            "trial_days": settings.trial_days,
            "trial_gb": settings.trial_gb,
        }

    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    is_active = trial_expires and trial_expires > now_str

    if is_active:
        # Найти order_id триала для кнопки "Показать ключ"
        orders = await get_user_subscriptions(user["id"])
        trial_order = next((o for o in orders if o["tariff"] == "trial"), None)
        return {
            "status": "active",
            "trial_days": settings.trial_days,
            "trial_gb": settings.trial_gb,
            "expires_at": trial_expires,
            "order_id": trial_order["id"] if trial_order else None,
        }
    else:
        return {
            "status": "expired",
            "trial_days": settings.trial_days,
            "trial_gb": settings.trial_gb,
            "expires_at": trial_expires,
        }


@account_router.post("/trial/activate")
async def activate_trial(request: Request, user: dict = Depends(require_verified_email)):
    """Активировать пробный период: 3 дня, 25 ГБ, 1 устройство."""
    if not settings.trial_enabled:
        raise HTTPException(403, "Пробный период отключён")
    # IP rate limiting: 1 триал на IP в 24 часа
    client_ip = get_real_ip(request)
    if not check_rate_limit(client_ip, max_requests=1, window_minutes=1440):
        logger.warning("Trial rate limit exceeded for IP: %s", client_ip)
        raise HTTPException(429, "Пробный период уже был активирован. Попробуйте позже.")

    expires_at = (datetime.utcnow() + timedelta(days=settings.trial_days)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    if not await claim_trial(user["id"], expires_at):
        raise HTTPException(400, "Пробный период уже использован")

    order_id = uuid.uuid4().hex[:12]
    await create_order(order_id, "trial", 0.0)
    await set_order_user(order_id, user["id"])
    await mark_paid(order_id)

    logger.info("Trial activated: user=%s email=%s ip=%s", user["id"], user["email"], client_ip)

    try:
        client_data = await create_client(
            email=f"trial-{order_id}@vpn.local",
            duration_days=settings.trial_days,
            limit_ip=settings.trial_devices,
            total_gb=settings.trial_gb,
        )
        sub_url = await get_subscription_url(client_data["sub_id"])
        await save_subscription(
            order_id, f"trial-{order_id}@vpn.local",
            client_data["sub_id"], sub_url, expires_at=expires_at,
        )
        return {
            "ok": True,
            "order_id": order_id,
            "expires_at": expires_at,
            "trial_gb": settings.trial_gb,
            "sub_url": sub_url,
        }
    except XuiError as e:
        logger.error("Trial provisioning failed: %s", e)
        await mark_order_error(order_id, str(e))
        raise HTTPException(502, f"Ошибка создания пробного ключа: {e}")


# ————————————————— DEVICE ADD-ONS (доп. устройства) —————————————————

class AddonRequest(BaseModel):
    addon_type: str  # "devices_5" | "devices_10"


@account_router.get("/subscription/{order_id}/addon-price")
async def get_addon_price(order_id: str, addon_type: str, user: dict = Depends(get_optional_user)):
    """Расчёт proration: P = ceil(B * (1 - D) / T * R)"""
    if not user:
        raise HTTPException(401, "Требуется авторизация")
    order = await get_user_order(user["id"], order_id)
    if not order:
        raise HTTPException(404, "Подписка не найдена")
    if order.get("status") == "deleted":
        raise HTTPException(400, "Подписка удалена")

    addon_cfg = settings.device_addons.get(addon_type)
    if not addon_cfg:
        raise HTTPException(400, "Неизвестный тип add-on")
    tariff = settings.tariffs.get(order["tariff"])
    if not tariff:
        raise HTTPException(400, "Неизвестный тариф")

    now = datetime.utcnow()
    remaining = 0
    if order.get("expires_at"):
        try:
            expires_at = datetime.strptime(order["expires_at"], "%Y-%m-%d %H:%M:%S")
            remaining = max(0, (expires_at - now).total_seconds() / 86400)
        except ValueError:
            pass

    base_price = addon_cfg["base_price"]
    total_days = tariff["days"]
    price_now = compute_addon_proration(
        base_price, tariff.get("discount", 0), total_days, remaining
    )["price_now"]

    return {
        "addon_type": addon_type, "extra_devices": addon_cfg["extra_devices"],
        "title": addon_cfg["title"], "base_monthly": base_price,
        "discount_pct": tariff.get("discount", 0),
        "remaining_days": round(remaining, 1), "total_days": total_days, "price_now": price_now,
    }


@account_router.post("/subscription/{order_id}/addon")
async def purchase_addon(order_id: str, req: AddonRequest, request: Request,
                         user: dict = Depends(get_optional_user)):
    """Покупка add-on: платёж Platega на пропорциональную сумму."""
    if not user:
        raise HTTPException(401, "Требуется авторизация")
    order = await get_user_order(user["id"], order_id)
    if not order:
        raise HTTPException(404, "Подписка не найдена")
    if order.get("status") in ("deleted", "pending"):
        raise HTTPException(400, "Нельзя купить add-on для этой подписки")
    if not order.get("xui_email"):
        raise HTTPException(400, "Нет привязки к 3x-UI")

    addon_cfg = settings.device_addons.get(req.addon_type)
    if not addon_cfg:
        raise HTTPException(400, "Неизвестный тип add-on")

    # Баг5: проверяем НЕ только active/cancel_pending, но и pending (защита от двойного клика)
    existing_addons = await get_device_addons_for_order(order_id)
    pending_or_active = [a for a in existing_addons
                         if a["addon_type"] == req.addon_type
                         and a["status"] in ("active", "pending", "cancel_pending")]
    if pending_or_active:
        raise HTTPException(400, "Уже есть активный или ожидающий add-on этого типа")

    tariff = settings.tariffs.get(order["tariff"])
    now = datetime.utcnow()
    remaining = 0
    if order.get("expires_at"):
        try:
            expires_at = datetime.strptime(order["expires_at"], "%Y-%m-%d %H:%M:%S")
            remaining = max(0, (expires_at - now).total_seconds() / 86400)
        except ValueError:
            pass

    base_price = addon_cfg["base_price"]
    total_days = tariff["days"]
    price_now = compute_addon_proration(
        base_price, tariff.get("discount", 0), total_days, remaining
    )["price_now"]

    addon_id = uuid.uuid4().hex[:12]

    if price_now <= 0:
        await create_device_addon(addon_id, user["id"], order_id, req.addon_type,
                                  addon_cfg["extra_devices"], 0, order.get("expires_at", ""))
        await activate_addon(addon_id)
        base_devices = tariff.get("devices", 5)
        extra = await get_total_extra_devices(order_id)
        try:
            await update_client_limit(order["xui_email"], base_devices + extra)
        except XuiError as e:
            logger.error("Failed to update limit: %s", e)
        return {"ok": True, "addon_id": addon_id, "price_now": 0}

    try:
        payment = await create_payment(amount=price_now, order_id=addon_id,
                                       description=f"Доп. устройства {addon_cfg['title']} ({round(remaining)} дн.)",
                                       capability_token=uuid.uuid4().hex)
    except PlategaError as e:
        raise HTTPException(502, str(e))

    # Баг1: НЕ вызываем save_platega_tx — addon_id не существует в orders,
    # иначе webhook подхватит фантомный заказ и вызовет fulfill_order.
    # tx_id хранится в device_addons.platega_tx_id (через create_device_addon).
    await create_device_addon(addon_id, user["id"], order_id, req.addon_type,
                              addon_cfg["extra_devices"], price_now,
                              order.get("expires_at", ""), payment["transaction_id"])

    return {"ok": True, "addon_id": addon_id, "payment_url": payment["payment_url"], "amount": price_now}


@account_router.post("/subscription/{order_id}/addon/cancel")
async def cancel_addon(order_id: str, user: dict = Depends(get_optional_user)):
    """Отмена add-on: cancel_pending, лимит уменьшится при следующем продлении."""
    if not user:
        raise HTTPException(401, "Требуется авторизация")
    order = await get_user_order(user["id"], order_id)
    if not order:
        raise HTTPException(404, "Подписка не найдена")
    addon = await get_active_addon_for_order(order_id)
    if not addon:
        raise HTTPException(404, "Нет активного add-on")
    await cancel_pending_addon(addon["id"])
    return {"ok": True, "message": "Доп. устройства будут отменены при следующем продлении"}


@account_router.get("/subscription/{order_id}/addons")
async def list_addons(order_id: str, user: dict = Depends(get_optional_user)):
    """Список add-on'ов для подписки."""
    if not user:
        raise HTTPException(401, "Требуется авторизация")
    order = await get_user_order(user["id"], order_id)
    if not order:
        raise HTTPException(404, "Подписка не найдена")
    addons = await get_device_addons_for_order(order_id)
    total_extra = await get_total_extra_devices(order_id)
    return {"addons": addons, "total_extra_devices": total_extra}


@account_router.get("/addon/{addon_id}/status")
async def addon_status(addon_id: str, user: dict = Depends(get_optional_user)):
    """Polling-эндпоинт: проверка статуса add-on после оплаты (аналог /api/order/{id}/status)."""
    if not user:
        raise HTTPException(401, "Требуется авторизация")
    addon = await get_addon_by_id(addon_id)
    if not addon:
        raise HTTPException(404, "Add-on не найден")
    if addon["user_id"] != user["id"]:
        raise HTTPException(403, "Доступ запрещён")

    # Если pending — проверяем статус оплаты, и только тогда активируем
    if addon["status"] == "pending" and addon.get("platega_tx_id"):
        try:
            real_status = await check_status(addon["platega_tx_id"])
            if real_status == "succeeded":
                await fulfill_addon(addon_id)
                addon = await get_addon_by_id(addon_id)
            # Если не succeeded — оставляем pending, webhook/polling попробует снова
        except PlategaError as e:
            logger.error("Addon status polling: check_status failed for %s: %s", addon_id, e)

    return {
        "addon_id": addon["id"],
        "status": addon["status"],
        "addon_type": addon["addon_type"],
        "extra_devices": addon["extra_devices"],
    }


app.include_router(account_router)


# ————————————————— РЕФЕРАЛЬНАЯ ПРОГРАММА —————————————————
referral_router = APIRouter(prefix="/api/referral", tags=["referral"])


@referral_router.get("/code")
async def api_referral_code(user: dict = Depends(get_current_user)):
    """Свой реферальный код и ссылка."""
    code = await ensure_referral_code(user["id"])
    if not code:
        raise HTTPException(404, "Пользователь не найден")
    enabled = (await get_setting("referral_enabled", "1")) == "1"
    bonus_percent = int(await get_setting("bonus_percent", "10") or 0)
    base = settings.site_base_url.rstrip("/")
    return {
        "code": code,
        "link": f"{base}/?ref={code}",
        "enabled": enabled,
        "bonus_percent": bonus_percent,
    }


@referral_router.get("/stats")
async def api_referral_stats(user: dict = Depends(get_current_user)):
    """Статистика: сколько приглашено, сколько бонусных дней начислено."""
    invited_count = await count_referrals(user["id"])
    total_reward_days = await sum_reward_days(user["id"])
    referrals = await get_referral_list(user["id"])
    return {
        "invited_count": invited_count,
        "total_reward_days": total_reward_days,
        "referrals": referrals,
        "bonus_percent": int(await get_setting("bonus_percent", "10") or 0),
        "enabled": (await get_setting("referral_enabled", "1")) == "1",
    }


@referral_router.post("/apply")
async def api_referral_apply(code: str, user: dict = Depends(get_current_user)):
    """Применить реферальный код (например, сразу после регистрации)."""
    code = (code or "").strip().upper()
    if not code:
        raise HTTPException(400, "Укажите реферальный код")
    if (await get_setting("referral_enabled", "1")) != "1":
        raise HTTPException(400, "Реферальная программа отключена")
    referrer = await get_user_by_referral_code(code)
    if not referrer:
        raise HTTPException(404, "Реферальный код не найден")
    if referrer["id"] == user["id"]:
        raise HTTPException(400, "Нельзя применить собственный реферальный код")
    ok = await apply_referral_code(user["id"], referrer["id"])
    return {
        "ok": True,
        "already_applied": not ok,
        "referrer_id": referrer["id"],
    }


app.include_router(referral_router)


async def process_referral_rewards(order_id: str):
    """
    Начисляет бонусные дни реферерам после успешной оплаты заказа.
    Проходит до 3 уровней вверх по цепочке приглашений.
    """
    order = await get_order(order_id)
    if not order:
        return
    payer_id = order.get("user_id")
    if not payer_id:
        return

    tariff = settings.tariffs.get(order["tariff"])
    days = tariff["days"] if tariff else 30

    levels = await get_referral_levels()
    if not levels:
        return

    current_id = payer_id
    for level_num, percent in levels:
        referrer_id = await get_user_referrer(current_id)
        if not referrer_id:
            break
        reward_days = math.ceil(days * percent / 100)
        if reward_days > 0:
            await _grant_referral_days(referrer_id, payer_id, order_id, reward_days)
        current_id = referrer_id


async def _grant_referral_days(
    referrer_id: str, referred_id: str, source_order_id: str, reward_days: int
):
    """
    Начисляет referrer'у reward_days бонусных дней:
    1) продлевает последнюю активную подписку в 3x-UI (если есть);
    2) фиксирует начисление в таблице referrals (для статистики).
    """
    granted = False
    sub = await get_active_subscription(referrer_id)
    if sub and sub.get("xui_email"):
        try:
            result = await renew_client(sub["xui_email"], reward_days)
            new_expires = datetime.utcfromtimestamp(
                result["new_expiry_ms"] / 1000
            ).strftime("%Y-%m-%d %H:%M:%S")
            async with _db() as db:
                await db.execute(
                    "UPDATE orders SET expires_at = ? WHERE id = ?",
                    (new_expires, sub["id"]),
                )
                await db.commit()
            granted = True
        except XuiError as e:
            logger.error(
                "Referral reward renew failed: referrer=%s order=%s: %s",
                referrer_id, source_order_id, e,
            )
        except Exception as e:
            logger.error(
                "Referral reward error: referrer=%s: %s", referrer_id, e,
            )

    await add_referral_reward(referrer_id, referred_id, reward_days)
    logger.info(
        "Referral reward: referrer=%s referred=%s days=%s order=%s granted=%s",
        referrer_id, referred_id, reward_days, source_order_id, granted,
    )


# ————————————————— МОДЕЛИ —————————————————

class CreateOrderRequest(BaseModel):
    tariff: str  # "quantum_month" | "quantum_quarter" | "quantum_halfyear" | "quantum_year"


class DemoOrderRequest(BaseModel):
    tariff: str = "quantum_month"
    password: str  # Пароль для демо-оплаты


# ————————————————— ЭНДПОИНТЫ API —————————————————

@app.get("/api/tariffs")
async def list_tariffs():
    """Список доступных тарифов — для витрины."""
    return [
        {"slug": slug, "days": t["days"], "price": t["price"], "title": t["title"]}
        for slug, t in settings.tariffs.items()
    ]


@app.get("/api/config")
async def api_config():
    """Публичная конфигурация витрины. demo_mode=true → показать кнопку «Демо подписка»."""
    return {
        "demo_mode": settings.demo_mode,
    }


@app.post("/api/order/create")
async def api_create_order(req: CreateOrderRequest, request: Request, user: dict = Depends(get_optional_user)):
    """
    Шаг 1: пользователь выбирает тариф.
    Создаёт заказ в БД и платёжную ссылку в Platega.
    Если пользователь авторизован — привязывает заказ к аккаунту.
    """
    # Rate limiting: 10 заказов в час с одного IP
    client_ip = get_real_ip(request)
    if not check_rate_limit(client_ip, max_requests=10, window_minutes=60):
        logger.warning("Rate limit exceeded for IP: %s", client_ip)
        raise HTTPException(429, "Слишком много запросов. Попробуйте позже.")

    tariff = settings.tariffs.get(req.tariff)
    if not tariff:
        raise HTTPException(400, "Неизвестный тариф")

    order_id = uuid.uuid4().hex[:12]
    # Capability-токен: случайный токен для доступа к статусу заказа без авторизации
    capability_token = uuid.uuid4().hex
    await create_order(order_id, req.tariff, tariff["price"], capability_token)

    try:
        payment = await create_payment(
            amount=tariff["price"],
            order_id=order_id,
            description=f"VPN подписка {tariff['title']} ({tariff['days']} дней)",
            capability_token=capability_token,
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
        "capability_token": capability_token,  # Для доступа к статусу без авторизации
    }


@app.post("/api/order/demo")
async def api_demo_order(req: DemoOrderRequest, request: Request, user: dict = Depends(get_optional_user)):
    """
    ДЕМО-оплата — создает заказ и сразу выдаёт ключ без реальной оплаты.
    Используйте только для тестирования!
    В продакшене отключается через DEMO_MODE=false в .env.
    """
    if not settings.demo_mode:
        raise HTTPException(404, "Демо-режим отключён")

    # Проверка пароля демо-оплаты
    if req.password != settings.demo_password:
        raise HTTPException(403, "Неверный пароль демо-оплаты")

    # Rate-limit: 3 demo-заказа в час с одного IP (защита от абьюза)
    client_ip = get_real_ip(request)
    if not check_rate_limit(client_ip, max_requests=3, window_minutes=60):
        logger.warning("Demo rate limit exceeded for IP: %s", client_ip)
        raise HTTPException(429, "Слишком много демо-запросов. Попробуйте позже.")

    tariff = settings.tariffs.get(req.tariff)
    if not tariff:
        raise HTTPException(400, "Неизвестный тариф")

    order_id = uuid.uuid4().hex[:12]
    # Capability-токен для демо-заказа
    capability_token = uuid.uuid4().hex
    await create_order(order_id, req.tariff, tariff["price"], capability_token)

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
        "payment_url": f"{settings.site_base_url}/payment/success?order_id={order_id}&token={capability_token}",
        "amount": 0,
        "demo": True,
        "capability_token": capability_token,
    }


@app.get("/api/order/{order_id}/status")
async def api_order_status(order_id: str, request: Request, token: str = ""):
    """
    Витрина опрашивает этот эндпоинт после редиректа с оплаты.
    Возвращает sub-ссылку, если ключ уже создан.

    Авторизация: capability-токен (из query ?token=...) ИЛИ авторизованный владелец заказа.
    Если токен не передан и пользователь не авторизован — отдаём только статус без sub_url.
    """
    # Rate limiting: 30 запросов в минуту с одного IP
    client_ip = get_real_ip(request)
    if not check_rate_limit(client_ip, max_requests=30, window_minutes=1):
        logger.warning("Rate limit exceeded for status endpoint, IP: %s", client_ip)
        raise HTTPException(429, "Слишком много запросов. Попробуйте позже.")

    order = await get_order(order_id)
    if not order:
        raise HTTPException(404, "Заказ не найден")

    # Проверка авторизации: capability-токен ИЛИ владелец заказа
    # Авторизация: capability-токен ИЛИ владелец ИЛИ заказ без токена (backward compat)
    is_authorized = False
    if token and order.get("capability_token") and hmac.compare_digest(token, order["capability_token"]):
        is_authorized = True
    elif not order.get("capability_token"):
        # Старый заказ без capability_token — разрешаем доступ (backward compatibility)
        is_authorized = True

    logger.info("Status check for order %s: status=%s, has_sub_url=%s, has_tx=%s",
                order_id, order["status"], bool(order.get("sub_url")), bool(order.get("platega_tx_id")))

    # Case 1: paid but no key created → retry fulfill_order
    if order["status"] == "paid" and not order.get("sub_url"):
        logger.info("Order %s is paid but has no sub_url, retrying fulfill_order", order_id)
        try:
            await fulfill_order(order_id)
            order = await get_order(order_id)
            logger.info("After retry: order %s sub_url=%s", order_id, bool(order.get("sub_url")))
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

    # Case 3: not paid and no tx_id yet → wait for webhook
    elif order["status"] != "paid" and not order.get("platega_tx_id"):
        logger.info("Order %s: no tx_id yet, waiting for webhook or Platega redirect", order_id)

    response = {
        "order_id": order["id"],
        "status": order["status"],
        "tariff": order["tariff"],
        "has_capability_token": bool(order.get("capability_token")),
    }

    # sub_url и QR возвращаются только авторизованным (токен или владелец)
    if is_authorized and order.get("sub_url"):
        response["sub_url"] = order["sub_url"]
        response["qr_base64"] = _make_qr_base64(order["sub_url"])
        logger.info("Returning sub_url for order %s (authorized)", order_id)
    elif order.get("sub_url"):
        # Заказ выполнен, но нет авторизации — сообщаем что ключ готов
        response["ready"] = True

    return response


@app.post("/webhook/platega")
async def platega_webhook(request: Request):
    """
    Webhook от Platega об изменении статуса транзакции.
    Platega шлёт JSON с полями: id, status, payload (наш order_id).
    """
    body = await request.body()

    # Проверяем webhook через X-MerchantId + X-Secret
    if not verify_platega_webhook(request.headers):
        logger.warning("Invalid webhook credentials")
        raise HTTPException(403, "Invalid credentials")

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

    # 1. Реальный статус транзакции — вычисляем ОДИН раз, до любых веток
    try:
        real_status = await check_status(tx_id)
    except PlategaError as e:
        logger.error("Webhook check_status failed for tx=%s: %s", tx_id, e)
        return {"ok": True, "msg": "check failed, polling fallback"}

    if real_status != "succeeded":
        logger.info("Payment not confirmed: tx=%s status=%s", tx_id, real_status)
        if real_status in ("cancelled", "expired"):
            # Проверяем — это add-on или обычный заказ?
            addon = await get_addon_by_tx(tx_id)
            if not addon and order_id:
                addon_by_id = await get_addon_by_id(order_id)
                if addon_by_id and addon_by_id.get("platega_tx_id") == tx_id:
                    addon = addon_by_id
            if addon:
                logger.info("Addon payment cancelled/expired: id=%s", addon["id"])
            else:
                await mark_order_error(order_id, f"Payment {real_status}")
        return {"ok": True, "msg": "not confirmed yet"}

    # 2. Статус succeeded — ищем add-on или обычный заказ
    addon = await get_addon_by_tx(tx_id)
    if not addon and order_id:
        addon_by_id = await get_addon_by_id(order_id)
        if addon_by_id and addon_by_id.get("platega_tx_id") == tx_id:
            addon = addon_by_id

    if addon and addon["status"] == "pending":
        await fulfill_addon(addon["id"])
        return {"ok": True}

    # Обычный заказ
    order = await get_order(order_id) if order_id else None
    if not order:
        order = await get_order_by_tx(tx_id)
    if not order:
        logger.warning("Order not found for tx=%s", tx_id)
        return {"ok": False, "error": "order not found"}

    await fulfill_order(order["id"])
    return {"ok": True}


# ————————————————— FULFILL ADD-ON (с Lock) —————————————————

_fulfill_addon_locks: dict[str, asyncio.Lock] = {}

async def fulfill_addon(addon_id: str):
    """Активация add-on после оплаты. Защищена Lock от параллельных вызовов."""
    async with _fulfill_locks_lock:
        lock_key = f"addon:{addon_id}"
        if lock_key not in _fulfill_locks:
            _fulfill_locks[lock_key] = asyncio.Lock()

    async with _fulfill_locks[lock_key]:
        addon = await get_addon_by_id(addon_id)
        if not addon:
            logger.warning("fulfill_addon: addon %s not found", addon_id)
            return
        if addon["status"] != "pending":
            logger.info("fulfill_addon: addon %s already %s", addon_id, addon["status"])
            return

        await activate_addon(addon_id)

        order = await get_order(addon["order_id"])
        if order and order.get("xui_email"):
            tariff = settings.tariffs.get(order["tariff"])
            base_devices = tariff.get("devices", 5) if tariff else 5
            extra = await get_total_extra_devices(addon["order_id"])
            try:
                await update_client_limit(order["xui_email"], base_devices + extra)
                logger.info("Addon activated: id=%s type=%s limit=%d",
                            addon_id, addon["addon_type"], base_devices + extra)
            except XuiError as e:
                logger.error("Addon activate: failed to update limit: %s", e)

    return {"ok": True}


@app.get("/payment/success", response_class=HTMLResponse)
async def payment_success(order_id: str, token: str = ""):
    """Страница после успешной оплаты — показывает sub-ссылку и QR."""
    # Передаём capability-токен в шаблон для polling
    return HTML_TEMPLATE.render(order_id=order_id, capability_token=token)


@app.get("/payment/failed", response_class=HTMLResponse)
async def payment_failed(order_id: str):
    """Страница после неудачной оплаты."""
    return "<html><body><h2>Оплата не удалась</h2><p>Попробуйте снова.</p></body></html>"


# ————————————————— УТИЛИТЫ —————————————————

async def fulfill_order(order_id: str):
    """
    Создаёт клиента в 3x-UI после успешной оплаты.
    КЛЮЧЕВОЙ МОМЕНТ — создаёт клиента сразу во ВСЕХ видимых инбаундах.
    Идемпотентно: если sub_url уже есть — ничего не делает.
    Защищено от гонки: asyncio.Lock на уровне order_id.
    """
    # Получаем или создаём лок для этого заказа
    async with _fulfill_locks_lock:
        if order_id not in _fulfill_locks:
            _fulfill_locks[order_id] = asyncio.Lock()

    async with _fulfill_locks[order_id]:
        order = await get_order(order_id)

        if order.get("sub_url"):
            logger.info("Order %s already fulfilled", order_id)
            return

        if order.get("status") != "paid":
            await mark_paid(order_id)

        tariff = settings.tariffs.get(order["tariff"])
        days = tariff["days"] if tariff else 30
        devices = tariff.get("devices", 1) if tariff else 1

        # Уникальный email на основе тарифа и номера заказа
        email = f"{order['tariff']}-{order_id}@vpn.local"

        logger.info("Fulfilling order %s: tariff=%s, days=%s, devices=%s, email=%s",
                    order_id, order["tariff"], days, devices, email)

        try:
            # КЛЮЧЕВОЙ МОМЕНТ —
            # Создаём клиента ВО ВСЕХ видимых инбаундах
            # limit_ip = кол-во устройств, разрешённых тарифом
            client_data = await create_client(
                email=email,
                duration_days=days,
                limit_ip=devices,
            )

            logger.info("Client created for order %s: sub_id=%s", order_id, client_data.get("sub_id"))

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
                "Order %s fulfilled successfully: email=%s sub_url=%s",
                order_id, email, sub_url[:80],
            )

            # Начисляем бонусные дни реферерам
            try:
                await process_referral_rewards(order_id)
            except Exception as e:
                logger.error(
                    "Referral rewards failed for order %s: %s", order_id, e
                )

        except XuiError as e:
            # Логируем ошибку, заказ остаётся в статусе 'paid'
            # но без выданного ключа — нужна ручная проверка
            logger.error("XuiError for order %s: %s", order_id, e)
            await mark_order_error(order_id, str(e))
            return
        except Exception as e:
            logger.error("Unexpected error for order %s: %s", order_id, e)
            await mark_order_error(order_id, str(e))
            return

    # НЕ удаляем лок — он остаётся как защита от повторных вызовов
    # Словарь _fulfill_locks растёт медленно (только для заказов с fulfill)


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
        <p class="text-gray-700 mb-2">Оплата получена. Ваш ключ готовится...</p>
        <p class="text-gray-500 text-sm mb-6">Заказ: <strong>{{ order_id }}</strong></p>

        <!-- Кнопка в ЛК — доступна СРАЗУ, не ждём загрузки ключа -->
        <a href="/account.html" class="block w-full text-center bg-green-600 text-white font-semibold py-3 px-6 rounded-lg hover:bg-green-700 transition mb-6 text-lg">
            🔑 Перейти в личный кабинет
        </a>

        <div id="status" class="mb-6">
            <div class="flex items-center gap-3 text-gray-500 text-sm">
                <svg class="animate-spin h-5 w-5 text-green-600" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path></svg>
                <span id="status-text">Проверяем статус ключа...</span>
            </div>
        </div>

        <div id="result" class="hidden">
            <div class="bg-green-50 border-l-4 border-green-500 p-4 mb-4">
                <p class="text-sm text-green-700 font-semibold">🎉 Ключ уже готов! Вот ваша подписка:</p>
            </div>

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

            <div class="bg-green-50 border-l-4 border-green-500 p-4 mb-4">
                <p class="text-sm text-green-700">
                    <strong>📱 Инструкция:</strong> Скопируйте ссылку и вставьте в приложение для VPN.
                </p>
            </div>

            <a href="/account.html" class="block w-full text-center bg-gray-600 text-white font-medium py-2 px-4 rounded hover:bg-gray-700 transition">
                Перейти в личный кабинет
            </a>
        </div>

        <!-- Если ключ НЕ загрузился — показываем ссылку на ЛК -->
        <div id="timeout-hint" class="hidden">
            <div class="bg-amber-50 border-l-4 border-amber-400 p-4 mb-4">
                <p class="text-sm text-amber-700">
                    <strong>Ключ ещё готовится.</strong> Не переживайте — он уже в процессе.
                    Вы можете забрать его в личном кабинете.
                </p>
            </div>
            <a href="/account.html" class="block w-full text-center bg-green-600 text-white font-semibold py-3 px-6 rounded-lg hover:bg-green-700 transition">
                🔑 Забрать ключ в личном кабинете
            </a>
        </div>
    </div>

    <script>
        const orderId = "{{ order_id }}";
        const capabilityToken = "{{ capability_token }}";
        let attempts = 0;
        const maxAttempts = 15; // 30 секунд — потом предлагаем ЛК

        function showTimeoutHint() {
            const s = document.getElementById("status");
            const h = document.getElementById("timeout-hint");
            if (s) s.classList.add("hidden");
            if (h) h.classList.remove("hidden");
        }

        async function checkStatus() {
            attempts++;
            try {
                const resp = await fetch(`/api/order/${orderId}/status?token=${capabilityToken}`);
                const data = await resp.json();

                if (data.sub_url) {
                    // Ключ готов — показываем сразу
                    document.getElementById("sub_url").textContent = data.sub_url;
                    document.getElementById("qr_code").src = "data:image/png;base64," + data.qr_base64;
                    document.getElementById("status").classList.add("hidden");
                    document.getElementById("result").classList.remove("hidden");
                } else if (data.ready) {
                    // Ключ есть, но нет авторизации — тоже предлагаем ЛК
                    showTimeoutHint();
                } else if (attempts < maxAttempts) {
                    // Ещё ждём — обновляем текст
                    const dots = '.'.repeat((attempts % 3) + 1);
                    document.getElementById("status-text").textContent = `Проверяем статус ключа${dots}`;
                    setTimeout(checkStatus, 2000);
                } else {
                    // 30 секунд прошли — предлагаем ЛК
                    showTimeoutHint();
                }
            } catch (e) {
                console.error(e);
                if (attempts < maxAttempts) {
                    setTimeout(checkStatus, 2000);
                } else {
                    showTimeoutHint();
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
