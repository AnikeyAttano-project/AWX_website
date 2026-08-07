"""Личный кабинет и реферальная программа: /api/account/*, /api/referral/*."""
import logging
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from config import settings
import shared_state
from shared_state import effective_inbounds, get_real_ip
from database import (
    get_user_subscriptions, get_user_order, get_user_by_referral_code,
    apply_referral_code, ensure_referral_code, count_referrals,
    sum_reward_days, get_referral_list, get_setting,
    get_order, get_device_addons_for_order, get_active_addon_for_order,
    activate_pending_addons_for_order, cancel_pending_addon,
    finalize_addon_cancellation, finalize_pending_addon,
    get_addon_by_id, get_total_extra_devices,
    claim_trial, set_order_custom_name, mark_order_deleted,
    create_device_addon, create_renewal, get_renewal_by_id,
    set_renewal_status, get_pending_renewal_for_order, get_user_by_id,
    get_active_subscription, get_user_referrer, get_referral_levels,
    add_site_log, activate_addon,
    create_order, set_order_user, mark_paid, save_subscription,
    mark_order_error,
)
from payment_providers import PaymentError
from xui_client import XuiError
from pricing import compute_addon_proration
from auth import get_optional_user, require_verified_email, get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/account", tags=["account"])
referral_router = APIRouter(prefix="/api/referral", tags=["referral"])


class RenameSubscriptionRequest(BaseModel):
    name: str


class AddonRequest(BaseModel):
    addon_type: str  # "devices_5" | "devices_10"

@router.get("/subscriptions")
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


@router.get("/subscription/{order_id}")
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
        result["qr_base64"] = shared_state._make_qr_base64(order["sub_url"])
    return result


@router.post("/renew/{order_id}")
async def renew_subscription(order_id: str, request: Request, user: dict = Depends(get_optional_user)):
    """
    Платное продление подписки: pending-заявка + платёж.

    Сам платёж — достаточная защита от накрутки, поэтому rate-limit
    «1 продление в 24 часа» и любые временные ограничения убраны. Остаётся
    только лёгкий лимит против спама создания платёжных ссылок (10/час).
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

    # Лёгкий rate-limit ПРОТИВ СПАМА создания платёжных ссылок (не против
    # легитимного продления) — 10/час более чем достаточно реальному пользователю.
    if not shared_state.check_rate_limit(f"renew-payment:{user['id']}", max_requests=10, window_minutes=60):
        raise HTTPException(429, "Слишком много попыток. Попробуйте позже.")

    # Дедуп двойного клика: пока есть pending-заявка — новый платёж не создаём.
    existing = await get_pending_renewal_for_order(order_id)
    if existing:
        raise HTTPException(400, "Уже есть незавершённый платёж за продление этой подписки.")

    tariff = settings.tariffs.get(order["tariff"])
    if not tariff:
        raise HTTPException(400, "Тариф не найден")
    days, amount = tariff["days"], tariff["price"]  # полная цена, НЕ proration

    renewal_id = uuid.uuid4().hex[:12]
    provider = shared_state.get_active_provider()
    try:
        payment = await provider.create_payment(
            amount=amount, order_id=renewal_id,
            description=f"Продление подписки {order['tariff']} ({days} дн.)",
            capability_token=uuid.uuid4().hex,
            return_url=f"{settings.site_base_url}/account.html",
        )
    except PaymentError as e:
        raise HTTPException(502, str(e))

    await create_renewal(renewal_id, order_id, user["id"], days, amount,
                         payment["transaction_id"], provider=provider.name)
    await add_site_log("renew_payment", actor=user["id"],
                       details=f"renewal={renewal_id} order={order_id} days={days} amount={amount}")
    return {"ok": True, "renewal_id": renewal_id,
            "payment_url": payment["payment_url"], "amount": amount}


@router.post("/subscription/{order_id}/rekey")
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
        result = await shared_state.rekey_client(
            old_email=order["xui_email"],
            new_email=new_email,
            expiry_ms=expiry_ms,
            limit_ip=devices,
            inbound_ids=effective_inbounds(order["tariff"]),
        )
        sub_url = await shared_state.get_subscription_url(result["sub_id"])
        expires_at = datetime.utcfromtimestamp(expiry_ms / 1000).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        await save_subscription(
            order_id, result["email"], result["sub_id"], sub_url,
            inbound_ids=",".join(str(x) for x in effective_inbounds(order["tariff"])),
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
            "qr_base64": shared_state._make_qr_base64(sub_url),
        }
    except XuiError as e:
        logger.error("Rekey failed for order %s: %s", order_id, e)
        raise HTTPException(502, f"Ошибка перевыпуска ключа: {e}")


@router.post("/subscription/{order_id}/rename")
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


@router.get("/subscription/{order_id}/stats")
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
        status = await shared_state.check_client_status(order["xui_email"])
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


@router.delete("/subscription/{order_id}")
async def delete_subscription(order_id: str, user: dict = Depends(require_verified_email)):
    """Удаление подписки: удаляет клиента из 3x-UI, помечает заказ deleted."""
    order = await get_user_order(user["id"], order_id)
    if not order:
        raise HTTPException(404, "Подписка не найдена")

    if order.get("xui_email"):
        try:
            await shared_state.delete_client(order["xui_email"])
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


@router.get("/trial")
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


@router.post("/trial/activate")
async def activate_trial(request: Request, user: dict = Depends(require_verified_email)):
    """Активировать пробный период: 3 дня, 25 ГБ, 1 устройство."""
    if not settings.trial_enabled:
        raise HTTPException(403, "Пробный период отключён")
    # IP rate limiting: 1 триал на IP в 24 часа
    client_ip = get_real_ip(request)
    if not shared_state.check_rate_limit(client_ip, max_requests=1, window_minutes=1440):
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
        client_data = await shared_state.create_client(
            email=f"trial-{order_id}@vpn.local",
            duration_days=settings.trial_days,
            limit_ip=settings.trial_devices,
            total_gb=settings.trial_gb,
        )
        sub_url = await shared_state.get_subscription_url(client_data["sub_id"])
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


@router.get("/subscription/{order_id}/addon-price")
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


@router.post("/subscription/{order_id}/addon")
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

    # №21: истёкшая подписка НЕ получает аддон бесплатно. Раньше при remaining=0
    # proration давал цену 0 и аддон активировался без оплаты. Теперь — только
    # после продления.
    if order.get("expires_at"):
        try:
            exp = datetime.strptime(order["expires_at"], "%Y-%m-%d %H:%M:%S")
            if exp < datetime.utcnow():
                raise HTTPException(400, "Подписка истекла. Продлите её, чтобы добавить устройства")
        except ValueError:
            pass

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
            await shared_state.update_client_limit(order["xui_email"], base_devices + extra)
        except XuiError as e:
            logger.error("Failed to update limit: %s", e)
        await add_site_log("addon_purchase", actor=user["id"],
                           details=f"order={order_id} addon={addon_id} type={req.addon_type} price=0")
        return {"ok": True, "addon_id": addon_id, "price_now": 0}

    provider = shared_state.get_active_provider()
    try:
        payment = await provider.create_payment(amount=price_now, order_id=addon_id,
                                                description=f"Доп. устройства {addon_cfg['title']} ({round(remaining)} дн.)",
                                                capability_token=uuid.uuid4().hex,
                                                return_url=f"{settings.site_base_url}/account.html")
    except PaymentError as e:
        raise HTTPException(502, str(e))

    # tx_id хранится в device_addons.platega_tx_id (через create_device_addon),
    # чтобы webhook не подхватил фантомный заказ.
    await create_device_addon(addon_id, user["id"], order_id, req.addon_type,
                              addon_cfg["extra_devices"], price_now,
                              order.get("expires_at", ""), payment["transaction_id"],
                              provider=provider.name)

    await add_site_log("addon_purchase", actor=user["id"],
                       details=f"order={order_id} addon={addon_id} type={req.addon_type} price={price_now}")
    return {"ok": True, "addon_id": addon_id, "payment_url": payment["payment_url"], "amount": price_now}


@router.post("/subscription/{order_id}/addon/cancel")
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
    await add_site_log("addon_cancel", actor=user["id"],
                       details=f"order={order_id} addon={addon['id']} type={addon['addon_type']}")
    return {"ok": True, "message": "Доп. устройства будут отменены при следующем продлении"}


@router.get("/subscription/{order_id}/addons")
async def list_addons(order_id: str, user: dict = Depends(get_optional_user)):
    """Список add-on'ов для подписки + каталог пакетов (для цен, №45 из правки.txt).

    Раньше фронтенд показывал захардкоженные «100 ₽/мес»/«170 ₽/мес». Теперь цены
    и названия пакетов приходят с сервера (settings.device_addons) — изменение
    тарифов в админке сразу отражается в ЛК.
    """
    if not user:
        raise HTTPException(401, "Требуется авторизация")
    order = await get_user_order(user["id"], order_id)
    if not order:
        raise HTTPException(404, "Подписка не найдена")
    addons = await get_device_addons_for_order(order_id)
    total_extra = await get_total_extra_devices(order_id)
    catalog = [
        {
            "type": atype,
            "extra_devices": cfg["extra_devices"],
            "title": cfg["title"],
            "base_price": cfg["base_price"],
        }
        for atype, cfg in settings.device_addons.items()
    ]
    return {"addons": addons, "total_extra_devices": total_extra, "catalog": catalog}


@router.get("/addon/{addon_id}/status")
async def addon_status(addon_id: str, user: dict = Depends(get_optional_user)):
    """Polling-эндпоинт: проверка статуса add-on после оплаты (аналог /api/order/{id}/status)."""
    if not user:
        raise HTTPException(401, "Требуется авторизация")
    addon = await get_addon_by_id(addon_id)
    if not addon:
        raise HTTPException(404, "Add-on не найден")
    if addon["user_id"] != user["id"]:
        raise HTTPException(403, "Доступ запрещён")

    # Если pending — проверяем статус оплаты, и только тогда активируем.
    # confirm_and_fulfill сам: 1) проверит pending, 2) сходит в check_status
    # активного провайдера, 3) активирует ТОЛЬКО при "succeeded". Если статус
    # не succeeded — addon остаётся pending, webhook/polling попробует снова.
    if addon.get("platega_tx_id"):
        result = await shared_state.addon_lifecycle.confirm_and_fulfill(
            addon_id, addon["platega_tx_id"]
        )
        # Отменённый/протухший платёж — финализируем аддон (pending → cancelled),
        # иначе дедуп в purchase_addon заблокирует повторную покупку (№16).
        if result.get("final") in ("cancelled", "expired"):
            await finalize_pending_addon(addon_id)
        addon = await get_addon_by_id(addon_id)

    return {
        "addon_id": addon["id"],
        "status": addon["status"],
        "addon_type": addon["addon_type"],
        "extra_devices": addon["extra_devices"],
    }


@router.get("/renewal/{renewal_id}/status")
async def renewal_status(renewal_id: str, user: dict = Depends(get_optional_user)):
    """Polling-эндпоинт: статус платного продления после оплаты (аналог addon_status)."""
    if not user:
        raise HTTPException(401, "Требуется авторизация")
    renewal = await get_renewal_by_id(renewal_id)
    if not renewal:
        raise HTTPException(404, "Заявка не найдена")
    if renewal["user_id"] != user["id"]:
        raise HTTPException(403, "Доступ запрещён")

    # confirm_and_fulfill сам: pending → check_status → при "succeeded" продлевает.
    result = await shared_state.renewal_lifecycle.confirm_and_fulfill(
        renewal_id, renewal.get("platega_tx_id", "")
    )
    # Отменённый/протухший платёж — финализируем заявку (cancelled), иначе дедуп
    # get_pending_renewal_for_order заблокирует создание нового продления.
    if result.get("final") in ("cancelled", "expired"):
        await set_renewal_status(renewal_id, "cancelled")
        result["status"] = "cancelled"

    return {"renewal_id": renewal_id, **result}


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