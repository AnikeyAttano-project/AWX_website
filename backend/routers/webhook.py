"""Webhook-эндпоинты платёжных систем: /webhook/platega, /webhook/yookassa."""
import logging

from fastapi import APIRouter, HTTPException, Request

import shared_state
from database import (
    get_addon_by_tx, get_addon_by_id, get_renewal_by_tx, get_renewal_by_id,
    get_order, get_order_by_tx, set_renewal_status, mark_order_error,
)
from payment_providers import PaymentError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webhook"])

async def _handle_payment_webhook(request: Request, provider: "PaymentProvider"):
    """Общая обработка вебхука платёжного провайдера.

    Провайдер проверяет подлинность (verify_webhook), парсит тело (parse_webhook),
    а реальный статус всегда перепроверяется через его API (check_status) —
    поддельный вебхук не пройдёт.
    """
    body = await request.body()

    if not provider.verify_webhook(request.headers, body):
        logger.warning("Invalid webhook credentials (%s)", provider.name)
        raise HTTPException(403, "Invalid credentials")

    parsed = provider.parse_webhook(body)
    logger.info("%s webhook received: %s", provider.name, parsed)

    tx_id = parsed.get("transaction_id")
    order_id = parsed.get("order_id") or ""
    if not tx_id:
        return {"ok": False, "error": "no transaction id"}

    # 1. Реальный статус транзакции — вычисляем ОДИН раз, до любых веток
    try:
        real_status = await provider.check_status(tx_id)
    except PaymentError as e:
        logger.error("Webhook check_status failed for tx=%s: %s", tx_id, e)
        return {"ok": True, "msg": "check failed, polling fallback"}

    if real_status != "succeeded":
        logger.info("Payment not confirmed: tx=%s status=%s", tx_id, real_status)
        if real_status in ("cancelled", "expired"):
            # Проверяем — это add-on, продление или обычный заказ?
            addon = await get_addon_by_tx(tx_id)
            if not addon and order_id:
                addon_by_id = await get_addon_by_id(order_id)
                if addon_by_id and addon_by_id.get("platega_tx_id") == tx_id:
                    addon = addon_by_id
            if addon:
                logger.info("Addon payment cancelled/expired: id=%s", addon["id"])
            else:
                renewal = await get_renewal_by_tx(tx_id)
                if not renewal and order_id:
                    renewal_by_id = await get_renewal_by_id(order_id)
                    if renewal_by_id and renewal_by_id.get("platega_tx_id") == tx_id:
                        renewal = renewal_by_id
                if renewal:
                    # Финализируем заявку, иначе дедуп pending заблокирует новое продление
                    await set_renewal_status(renewal["id"], "cancelled")
                    logger.info("Renewal payment cancelled/expired: id=%s", renewal["id"])
                else:
                    await mark_order_error(order_id, f"Payment {real_status}")
        return {"ok": True, "msg": "not confirmed yet"}

    # 2. Статус succeeded — ищем add-on, затем renewal (Часть 2), затем обычный заказ.
    #    Реальный статус уже вычислен (real_status) — передаём его как known_status,
    #    чтобы confirm_and_fulfill не ходил в провайдер повторно. Сам lifecycle
    #    гарантирует порядок: только при "succeeded" → fulfill.
    addon = await get_addon_by_tx(tx_id)
    if not addon and order_id:
        addon_by_id = await get_addon_by_id(order_id)
        if addon_by_id and addon_by_id.get("platega_tx_id") == tx_id:
            addon = addon_by_id

    if addon:
        await shared_state.addon_lifecycle.confirm_and_fulfill(
            addon["id"], tx_id, known_status=real_status
        )
        return {"ok": True}

    # Платное продление
    renewal = await get_renewal_by_tx(tx_id)
    if not renewal and order_id:
        renewal_by_id = await get_renewal_by_id(order_id)
        if renewal_by_id and renewal_by_id.get("platega_tx_id") == tx_id:
            renewal = renewal_by_id
    if renewal:
        await shared_state.renewal_lifecycle.confirm_and_fulfill(
            renewal["id"], tx_id, known_status=real_status
        )
        return {"ok": True}

    # Обычный заказ
    order = await get_order(order_id) if order_id else None
    if not order:
        order = await get_order_by_tx(tx_id)
    if not order:
        logger.warning("Order not found for tx=%s", tx_id)
        return {"ok": False, "error": "order not found"}

    await shared_state.order_lifecycle.confirm_and_fulfill(
        order["id"], tx_id, known_status=real_status
    )
    return {"ok": True}


@router.post("/webhook/platega")
async def platega_webhook(request: Request):
    return await _handle_payment_webhook(request, shared_state.get_provider("platega"))


@router.post("/webhook/yookassa")
async def yookassa_webhook(request: Request):
    return await _handle_payment_webhook(request, shared_state.get_provider("yookassa"))