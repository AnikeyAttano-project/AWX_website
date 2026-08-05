"""
Общие хелперы и глобальные объекты (вынесены из main.py в рефакторинге Части 4).

Здесь: rate-limit, get_real_ip, автоочистка, провижининг заказов/add-ons/продлений,
инстансы PaymentLifecycle (order/addon/renewal), реферальные и тарифные хелперы, QR.
Роутеры и webhook импортируют отсюда — без циклических импортов на main.py.
"""
import asyncio
import base64
import io
import logging
import math
import qrcode
from collections import defaultdict
from datetime import datetime, timedelta

from fastapi import HTTPException, Request

from config import settings
from database import (
    _db,
    init_db, load_runtime_settings, create_order, get_order, save_platega_tx,
    mark_paid, save_subscription, get_order_by_tx, mark_order_error,
    begin_fulfillment, complete_fulfillment, fail_fulfillment,
    get_user_subscriptions, get_user_order, set_order_user,
    claim_trial, set_order_custom_name, mark_order_deleted,
    get_user_by_referral_code, apply_referral_code, ensure_referral_code,
    count_referrals, sum_reward_days, get_referral_list,
    get_referral_levels, get_user_referrer, get_active_subscription,
    add_referral_reward, get_setting, get_user_by_id,
    cleanup_expired_orders, cleanup_expired_trials, delete_order,
    create_device_addon, get_device_addons_for_order, get_active_addon_for_order,
    activate_addon, activate_pending_addons_for_order,
    cancel_pending_addon, finalize_addon_cancellation,
    get_addon_by_tx, get_addon_by_id, get_total_extra_devices,
    add_site_log, prune_site_log,
    validate_promo_code, compute_promo_discount, use_promo_code,
    get_promo_code,
    create_renewal, get_renewal_by_id, get_renewal_by_tx,
    get_pending_renewal_for_order, activate_renewal, set_renewal_status,
)
from payment_providers import get_provider, get_active_provider, PaymentError
from pricing import compute_addon_proration
from xui_client import (
    create_client, get_subscription_url, _parse_inbound_ids,
    update_client_limit, get_sub_links, renew_client, check_client_status,
    delete_client, rekey_client, effective_inbounds, XuiError,
)
from payment_lifecycle import PaymentLifecycle

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Rate limiting — хранилище запросов по IP
rate_limit_storage = defaultdict(list)


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


async def _renew_subscription_core(order_id: str, order: dict) -> dict:
    """
    Внутренняя логика продления подписки (БЕЗ проверок доступа/rate-limit).

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
        await add_site_log("renew", actor=order.get("user_id"),
                           details=f"order={order_id} days={days} new_expires={new_expires}")
        return {"ok": True, "new_expires_at": new_expires}
    except XuiError as e:
        logger.error("Renew failed for order %s: %s", order_id, e)
        error_msg = str(e)
        if "not found" in error_msg.lower() or "not exist" in error_msg.lower():
            raise HTTPException(404, "Клиент не найден в 3x-UI. Возможно, подписка была деактивирована. Попробуйте перевыпустить ключ.")
        raise HTTPException(500, f"Ошибка продления: {e}")


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


def _tariff_group_of(tariff_slug: str) -> str | None:
    """id группы, которой принадлежит тариф (None — без группы)."""
    for gid, g in settings.tariff_groups.items():
        if tariff_slug in (g.get("tariffs") or []):
            return gid
    return None


def _compute_order_total(tariff_slug: str, addon_type: str) -> tuple[float, float]:
    """(total, addon_price) для тарифа (+ опциональный add-on). Скидка тарифа
    применяется и к доп. устройствам — полный период (proration)."""
    tariff = settings.tariffs[tariff_slug]
    total = tariff["price"]
    addon_price = 0.0
    if addon_type:
        cfg = settings.device_addons.get(addon_type)
        if cfg:
            addon_price = compute_addon_proration(
                cfg["base_price"], tariff.get("discount", 0),
                tariff["days"], tariff["days"],
            )["price_now"]
            total += addon_price
    return total, addon_price


def _tariff_payload(slug: str, t: dict) -> dict:
    """Полный объект тарифа для витрины: цены, add-on'ы, эффективные инбаунды, группа."""
    days = t["days"]
    addons = []
    for atype, cfg in settings.device_addons.items():
        addons.append({
            "type": atype,
            "extra_devices": cfg["extra_devices"],
            "title": cfg["title"],
            "price": compute_addon_proration(
                cfg["base_price"], t.get("discount", 0), days, days
            )["price_now"],
        })
    # Группа, которой принадлежит тариф
    group_id = None
    for gid, g in settings.tariff_groups.items():
        if slug in (g.get("tariffs") or []):
            group_id = gid
            break
    return {
        "slug": slug,
        "days": days,
        "price": t["price"],
        "title": t["title"],
        "devices": t.get("devices", 1),
        "discount": t.get("discount", 0),
        "inbounds": effective_inbounds(slug),
        "group": group_id,
        "addons": addons,
        # Часть 6: контент витрины
        "description": t.get("description", ""),
        "features": t.get("features", []) or [],
        "badge": t.get("badge", ""),
    }


def _make_qr_base64(data: str) -> str:
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(data)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return base64.b64encode(buf.read()).decode()


async def _provision_addon(addon: dict):
    """Провижининг add-on после подтверждения оплаты.

    Перевод pending → active выполняет lifecycle (activate=activate_addon);
    здесь — только обновление лимита устройств в 3x-UI. Идемпотентность и
    защита от гонки — на lifecycle (per-entity asyncio.Lock).
    """
    order = await get_order(addon["order_id"])
    if order and order.get("xui_email"):
        tariff = settings.tariffs.get(order["tariff"])
        base_devices = tariff.get("devices", 5) if tariff else 5
        extra = await get_total_extra_devices(addon["order_id"])
        try:
            await update_client_limit(order["xui_email"], base_devices + extra)
            logger.info("Addon activated: id=%s type=%s limit=%d",
                        addon["id"], addon["addon_type"], base_devices + extra)
        except XuiError as e:
            logger.error("Addon activate: failed to update limit: %s", e)


async def fulfill_addon(addon_id: str):
    """Активация add-on после оплаты. Тонкая обёртка над addon_lifecycle.

    Вызывать ТОЛЬКО после подтверждения платежа (webhook / mock / confirm_and_fulfill).
    """
    await addon_lifecycle.fulfill(addon_id)
    return {"ok": True}


async def _provision_renewal(renewal: dict):
    """Выполняет фактическое продление после подтверждения оплаты.

    Платёж уже подтверждён (confirm_and_fulfill) — здесь только renew_client +
    обновление expires_at + обработка cancel_pending add-ons (внутри
    _renew_subscription_core). При любой ошибке помечаем заявку 'failed',
    чтобы пользователь мог создать новое продление (дедуп видит только 'pending').
    """
    order_id = renewal["order_id"]
    try:
        order = await get_order(order_id)
        if not order or not order.get("xui_email"):
            raise HTTPException(400, "Подписка недоступна для продления")
        await _renew_subscription_core(order_id, order)
    except HTTPException as e:
        logger.error("Renew provision failed for %s: %s", renewal["id"], e.detail)
        await set_renewal_status(renewal["id"], "failed")
    except XuiError as e:
        logger.error("Renew provision failed for %s: %s", renewal["id"], e)
        await set_renewal_status(renewal["id"], "failed")
    except Exception as e:
        logger.error("Renew provision unexpected error for %s: %s", renewal["id"], e)
        await set_renewal_status(renewal["id"], "failed")


async def _provision_order(order: dict):
    """Выдача ключа в 3x-UI после подтверждения оплаты.

    Вся логика старого fulfill_order: идемпотентность (sub_url), атомарный
    DB-claim (begin_fulfillment/complete_fulfillment/fail_fulfillment), создание
    клиента в эффективных инбаундах тарифа, промо-код, add-ons, рефералка,
    site_log. Защиту от гонки (per-entity asyncio.Lock) даёт lifecycle.
    """
    order_id = order["id"]

    # Идемпотентность: ключ уже выдан
    if order.get("sub_url"):
        logger.info("Order %s already fulfilled", order_id)
        return

    # State machine: атомарно занимаем заказ на выдачу ключа.
    # pending/failed → processing; stale processing (сбойный процесс)
    # пере-claim'ится. Если claim не удался — другой запрос уже выдаёт.
    if not await begin_fulfillment(order_id):
        logger.info("Order %s already being fulfilled by another request", order_id)
        return

    if order.get("status") != "paid":
        await mark_paid(order_id)

    tariff = settings.tariffs.get(order["tariff"])
    days = tariff["days"] if tariff else 30
    devices = tariff.get("devices", 1) if tariff else 1

    # Доп. устройства, купленные вместе с подпиской (add-on, pending/active).
    # Пока платёж не подтверждён addon'ы 'pending' — они уже оплачены тем же
    # транзакционным платежом, что и заказ, поэтому учитываем их сразу.
    extra_devices = sum(
        a["extra_devices"] for a in await get_device_addons_for_order(order_id)
        if a["status"] in ("pending", "active")
    )
    if extra_devices:
        devices += extra_devices
        logger.info(
            "Order %s includes add-on: +%s devices → limit_ip=%s",
            order_id, extra_devices, devices,
        )

    # Уникальный email на основе тарифа и номера заказа
    email = f"{order['tariff']}-{order_id}@vpn.local"

    logger.info("Fulfilling order %s: tariff=%s, days=%s, devices=%s, email=%s",
                order_id, order["tariff"], days, devices, email)

    # Инбаунды для тарифа: свой список тарифа → группы → все из env.
    # Клиент создаётся ТОЛЬКО в этих инбаундах.
    inbound_ids = effective_inbounds(order["tariff"])

    try:
        # КЛЮЧЕВОЙ МОМЕНТ —
        # Создаём клиента в инбаундах тарифа (limit_ip = кол-во устройств)
        client_data = await create_client(
            email=email,
            duration_days=days,
            limit_ip=devices,
            inbound_ids=inbound_ids,
        )

        logger.info("Client created for order %s: sub_id=%s inbounds=%s",
                    order_id, client_data.get("sub_id"), inbound_ids)

        # Получаем URL подписки
        sub_url = await get_subscription_url(client_data["sub_id"])

        # Сохраняем в БД
        expires_at = (datetime.utcnow() + timedelta(days=days)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        await save_subscription(
            order_id, email, client_data["sub_id"], sub_url,
            inbound_ids=",".join(str(x) for x in inbound_ids),
            expires_at=expires_at,
        )
        # State machine: processing → completed (ключ выдан)
        await complete_fulfillment(order_id)

        # Промо-код использован — инкрементируем счётчик (только при успешной выдаче)
        if order.get("promo_code"):
            promo_used = await get_promo_code(order["promo_code"])
            if promo_used:
                await use_promo_code(promo_used["id"])
                logger.info("Order %s: promo %s applied (+1 use)", order_id, order["promo_code"])

        logger.info(
            "Order %s fulfilled successfully: email=%s sub_url=%s",
            order_id, email, sub_url[:80],
        )

        # Активируем add-ons, купленные вместе с заказом (pending → active),
        # чтобы ЛК/продление/отмена видели итоговый лимит устройств.
        # Если create_client упал — addon'ы остаются pending, ретрай пересчитает лимит.
        activated = await activate_pending_addons_for_order(order_id)
        if activated:
            logger.info("Order %s: %s add-on(s) activated", order_id, activated)

        # Начисляем бонусные дни реферерам
        try:
            await process_referral_rewards(order_id)
        except Exception as e:
            logger.error(
                "Referral rewards failed for order %s: %s", order_id, e
            )

        await add_site_log("fulfill", actor=order.get("user_id"),
                           details=f"order={order_id} email={email} devices={devices}")

    except XuiError as e:
        # Логируем ошибку, заказ переводим в 'error' (status) + 'failed'
        # (fulfillment_status) — выдача не удалась, но ретрай возможен.
        logger.error("XuiError for order %s: %s", order_id, e)
        await mark_order_error(order_id, str(e))
        await fail_fulfillment(order_id, str(e))
        await add_site_log("fulfill_failed", actor=order.get("user_id"),
                           level="error", details=f"order={order_id} {e}")
    except Exception as e:
        logger.error("Unexpected error for order %s: %s", order_id, e)
        await mark_order_error(order_id, str(e))
        await fail_fulfillment(order_id, str(e))


async def fulfill_order(order_id: str):
    """Выдача ключа после оплаты. Тонкая обёртка над order_lifecycle.

    Вызывать ТОЛЬКО после подтверждения платежа (webhook / mock / confirm_and_fulfill).
    """
    await order_lifecycle.fulfill(order_id)


addon_lifecycle = PaymentLifecycle(
    get_by_id=get_addon_by_id,
    get_by_tx=get_addon_by_tx,
    activate=activate_addon,
    provision=_provision_addon,
    lock_prefix="addon",
)


renewal_lifecycle = PaymentLifecycle(
    get_by_id=get_renewal_by_id,
    get_by_tx=get_renewal_by_tx,
    activate=activate_renewal,          # pending -> active, идемпотентно
    provision=_provision_renewal,       # реальное продление после подтверждения
    lock_prefix="renewal",
)


order_lifecycle = PaymentLifecycle(
    get_by_id=get_order,
    get_by_tx=get_order_by_tx,
    activate=None,
    provision=_provision_order,
    lock_prefix="order",
    fulfill_statuses=("pending", "paid", "error"),
)