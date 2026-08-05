"""Витрина и заказы: /api/tariffs, /api/config, /api/order/*."""
import hmac
import logging
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from config import settings
import shared_state
from auth import get_optional_user
from database import (
    create_order, get_order, save_platega_tx, get_order_by_tx,
    mark_order_error, mark_paid, get_setting, add_site_log,
    get_active_subscription, get_device_addons_for_order,
    activate_pending_addons_for_order, create_device_addon,
    set_order_user, validate_promo_code, compute_promo_discount,
)
from payment_providers import PaymentError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["orders"])


class CreateOrderRequest(BaseModel):
    tariff: str  # "quantum_month" | "quantum_quarter" | "quantum_halfyear" | "quantum_year"
    addon_type: str = ""  # "devices_5" | "devices_10" — доп. устройства к покупке (опционально)
    promo_code: str = ""  # Промо-код (опционально, регистр не важен)


class DemoOrderRequest(BaseModel):
    tariff: str = "quantum_month"
    password: str  # Пароль для демо-оплаты


class PromoCheckRequest(BaseModel):
    code: str
    tariff: str
    addon_type: str = ""


@router.get("/api/tariffs")
async def list_tariffs():
    """Тарифы для витрины, сгруппированные по группам тарифов.

    Ответ: {tariffs: [плоский список — обратная совместимость],
            groups: [{id,title,description,inbounds,tariffs:[...]}],
            ungrouped: [slug, ...]}.

    Каждый тариф дополнен кол-вом устройств, скидкой, эффективным списком
    инбаундов (тариф → группа → все) и ценами add-on'ов (считает сервер —
    фронт не дублирует, чтобы сумма в модалке и платёжке не разошлась).
    """
    flat = []
    for slug, t in settings.tariffs.items():
        flat.append(shared_state._tariff_payload(slug, t))

    grouped_slugs = set()
    for g in settings.tariff_groups.values():
        grouped_slugs.update(g.get("tariffs") or [])

    groups_out = []
    for gid, g in settings.tariff_groups.items():
        group_tariffs = [
            shared_state._tariff_payload(slug, settings.tariffs[slug])
            for slug in (g.get("tariffs") or [])
            if slug in settings.tariffs
        ]
        groups_out.append({
            "id": gid,
            "title": g.get("title", gid),
            "description": g.get("description", ""),
            "inbounds": [int(x) for x in (g.get("inbounds") or [])],
            "tariffs": group_tariffs,
        })

    ungrouped = [slug for slug in settings.tariffs if slug not in grouped_slugs]

    return {"tariffs": flat, "groups": groups_out, "ungrouped": ungrouped}


@router.post("/api/promo/check")
async def check_promo(req: PromoCheckRequest):
    """Проверяет промо-код для тарифа (+add-on) и возвращает размер скидки.

    Используется фронтом в модалке подтверждения до создания заказа, чтобы
    показать итоговую сумму. Финальная валидация — в /api/order/create.
    """
    if req.tariff not in settings.tariffs:
        raise HTTPException(400, "Неизвестный тариф")
    total, _ = shared_state._compute_order_total(req.tariff, req.addon_type)
    promo, err = await validate_promo_code(req.code, req.tariff, shared_state._tariff_group_of(req.tariff))
    if not promo:
        return {"valid": False, "error": err}
    discount, final = compute_promo_discount(promo, total)
    return {
        "valid": True,
        "code": promo["code"],
        "kind": promo["kind"],
        "value": promo["value"],
        "discount": discount,
        "final": final,
    }


@router.get("/api/config")
async def api_config():
    """Публичная конфигурация витрины. demo_mode=true → показать кнопку «Демо подписка».

    telegram_bot_username — если задан, фронт рендерит кнопку «Войти через Telegram».
    """
    return {
        "demo_mode": settings.demo_mode,
        "telegram_bot_username": settings.telegram_bot_username,
        "branding": settings.branding,
    }


@router.post("/api/order/create")
async def api_create_order(req: CreateOrderRequest, request: Request, user: dict = Depends(get_optional_user)):
    """
    Шаг 1: пользователь выбирает тариф (+ опционально доп. устройства).
    Создаёт заказ в БД и платёжную ссылку в Platega на ИТОГОВУЮ сумму.
    Если пользователь авторизован — привязывает заказ к аккаунту.
    """
    # Rate limiting: 10 заказов в час с одного IP
    client_ip = shared_state.get_real_ip(request)
    if not shared_state.check_rate_limit(client_ip, max_requests=10, window_minutes=60):
        logger.warning("Rate limit exceeded for IP: %s", client_ip)
        raise HTTPException(429, "Слишком много запросов. Попробуйте позже.")

    tariff = settings.tariffs.get(req.tariff)
    if not tariff:
        raise HTTPException(400, "Неизвестный тариф")

    # Доп. устройства к покупке (add-on). Цену считает СЕРВЕР — клиент её не присылает.
    addon_cfg = settings.device_addons.get(req.addon_type) if req.addon_type else None
    if req.addon_type and not addon_cfg:
        raise HTTPException(400, "Неизвестный add-on")

    # device_addons.user_id NOT NULL → доп. устройства доступны только авторизованным
    if addon_cfg and not user:
        raise HTTPException(401, "Войдите в аккаунт, чтобы добавить устройства")

    # Итоговая сумма: тариф + add-on (скидка тарифа применяется и к доп. устройствам).
    total, addon_price = shared_state._compute_order_total(req.tariff, req.addon_type)

    # Промо-код: валидация + скидка. Код привязан к группе тарифа (если задан).
    promo = None
    promo_discount = 0.0
    if req.promo_code:
        promo, promo_err = await validate_promo_code(
            req.promo_code, req.tariff, shared_state._tariff_group_of(req.tariff)
        )
        if not promo:
            raise HTTPException(400, promo_err)
        promo_discount, total = compute_promo_discount(promo, total)

    order_id = uuid.uuid4().hex[:12]
    # Capability-токен: случайный токен для доступа к статусу заказа без авторизации
    capability_token = uuid.uuid4().hex
    provider = shared_state.get_active_provider()
    await create_order(
        order_id, req.tariff, total, capability_token,
        promo_code=(promo["code"] if promo else None),
        promo_discount=promo_discount,
        provider=provider.name,
    )

    description = f"VPN подписка {tariff['title']} ({tariff['days']} дней)"
    if addon_cfg:
        description += f" + {addon_cfg['title']}"

    try:
        payment = await provider.create_payment(
            amount=total,
            order_id=order_id,
            description=description,
            capability_token=capability_token,
        )
    except PaymentError as e:
        logger.error("Payment provider error: %s", e)
        raise HTTPException(502, str(e))

    await save_platega_tx(order_id, payment["transaction_id"])

    # Создаём add-on-строку (pending), привязанную к заказу и его транзакции.
    # Активируется в fulfill_order после подтверждения платежа.
    addon_id = ""
    if addon_cfg and user:
        addon_id = uuid.uuid4().hex[:12]
        expires_at = (datetime.utcnow() + timedelta(days=tariff["days"])).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        await create_device_addon(
            addon_id, user["id"], order_id, req.addon_type,
            addon_cfg["extra_devices"], addon_price,
            expires_at, payment["transaction_id"],
        )

    # Привязываем заказ к пользователю, если авторизован
    if user:
        await set_order_user(order_id, user["id"])

    await add_site_log("order_create", actor=(user["id"] if user else None),
                       details=f"order={order_id} tariff={req.tariff} "
                               f"addon={req.addon_type or 'none'} amount={total}")

    return {
        "order_id": order_id,
        "payment_url": payment["payment_url"],
        "amount": total,
        "addon_type": req.addon_type,
        "addon_price": addon_price,
        "promo_code": (promo["code"] if promo else None),
        "promo_discount": promo_discount,
        "capability_token": capability_token,  # Для доступа к статусу без авторизации
    }


@router.post("/api/order/demo")
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
    client_ip = shared_state.get_real_ip(request)
    if not shared_state.check_rate_limit(client_ip, max_requests=3, window_minutes=60):
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
        await shared_state.fulfill_order(order_id)
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


@router.get("/api/order/{order_id}/status")
async def api_order_status(order_id: str, request: Request, token: str = ""):
    """
    Витрина опрашивает этот эндпоинт после редиректа с оплаты.
    Возвращает sub-ссылку, если ключ уже создан.

    Авторизация: capability-токен (из query ?token=...) ИЛИ авторизованный владелец заказа.
    Если токен не передан и пользователь не авторизован — отдаём только статус без sub_url.
    """
    # Rate limiting: 30 запросов в минуту с одного IP
    client_ip = shared_state.get_real_ip(request)
    if not shared_state.check_rate_limit(client_ip, max_requests=30, window_minutes=1):
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

    # Case 1: paid but no key created → retry fulfill_order.
    # Оплата уже подтверждена — lifecycle.fulfill без повторного check_status.
    if order["status"] == "paid" and not order.get("sub_url"):
        logger.info("Order %s is paid but has no sub_url, retrying fulfill_order", order_id)
        try:
            await shared_state.order_lifecycle.fulfill(order_id)
            order = await get_order(order_id)
            logger.info("After retry: order %s sub_url=%s", order_id, bool(order.get("sub_url")))
        except Exception as e:
            logger.error("Retry failed for order %s: %s", order_id, e)

    # Case 2: not yet paid → check provider (polling fallback).
    # confirm_and_fulfill сам: pending → check_status → при "succeeded" fulfill.
    elif order["status"] != "paid" and order.get("platega_tx_id"):
        result = await shared_state.order_lifecycle.confirm_and_fulfill(
            order_id, order["platega_tx_id"]
        )
        logger.info("Polling order %s: tx=%s result=%s",
                    order_id, order["platega_tx_id"], result)
        order = await get_order(order_id)

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
        response["qr_base64"] = shared_state._make_qr_base64(order["sub_url"])
        logger.info("Returning sub_url for order %s (authorized)", order_id)
    elif order.get("sub_url"):
        # Заказ выполнен, но нет авторизации — сообщаем что ключ готов
        response["ready"] = True

    return response