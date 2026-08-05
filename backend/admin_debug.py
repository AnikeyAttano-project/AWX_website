"""
Debug sandbox (Billing & Upsell Sandbox) для админ-панели AWX-WEB-lite.

Полностью отделён от ``admin.py`` (не раздуваем god-module). Живёт под
префиксом ``/admin/debug``, защищён тем же ``X-Admin-Key`` через
``require_admin`` плюс флагом полной изоляции ``settings.debug_sandbox_enabled``
(по умолчанию False — весь раздел отвечает 404).

ВАЖНО ПРО БЕЗОПАСНОСТЬ (см. раздел «НЕЛЬЗЯ» в задании):
- Никаких импортов из ``platega_client`` — симулятор работает только через
  прямые вызовы ``fulfill_order``/``fulfill_addon`` и прямые операции с БД,
  без единого реального похода в Platega API.
- Никакое разрушительное действие нельзя выполнить на реальном (не тестовом)
  пользователе без явного ``confirm_email``, совпадающего с его настоящим email.
- Force Sync строго двухшаговый: preview (read-only) → apply (после явного
  действия оператора).
"""

import hmac
import logging
import math
import uuid
from typing import Optional

import aiosqlite
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel

from admin import require_admin
from config import settings
from database import (
    _db,
    get_user_by_id,
    get_order,
    get_user_subscriptions,
    get_device_addons_for_order,
    get_active_addon_for_order,
    get_total_extra_devices,
    create_order,
    set_order_user,
    mark_paid,
    mark_order_error,
    get_addon_by_id,
    create_device_addon,
    activate_addon,
    cancel_pending_addon,
    finalize_addon_cancellation,
    log_debug_action,
)
from pricing import compute_addon_proration
from xui_client import update_client_limit, get_client_info, XuiError

logger = logging.getLogger(__name__)


def require_sandbox():
    """Флаг полной изоляции песочницы. 404, а не 403 — не подтверждать
    посторонним, что эндпоинт вообще существует."""
    if not settings.debug_sandbox_enabled:
        raise HTTPException(404, "Debug sandbox disabled")


admin_debug_router = APIRouter(
    prefix="/admin/debug",
    tags=["admin-debug"],
    # Порядок важен: require_admin (X-Admin-Key) → require_sandbox (флаг 404) —
    # оба router-уровня, поэтому срабатывают ДО per-endpoint require_admin_name.
    # Иначе при выключенной песочнице POST с валидным телом упал бы в 400 (имя)
    # вместо 404 (флаг) — чек-лист приёмки требует 404 для ВСЕГО раздела (п.1).
    dependencies=[Depends(require_admin), Depends(require_sandbox)],
)


# ————————————————— Зависимости и guard-ы —————————————————

async def require_admin_name(x_admin_name: str = Header(default="", alias="X-Admin-Name")):
    """X-Admin-Name обязателен на всех разрушающих эндпоинтах.

    Это НЕ настоящая аутентификация по ролям (в проекте единственный общий
    X-Admin-Key, отдельных админ-учёток нет) — это лишь указание «кто отвечает
    за это действие», значение не проверяется ни на что кроме «не пусто».
    Если в будущем появятся несколько реальных людей с раздельным доступом —
    это место нужно будет заменить на настоящие именные учётки.
    """
    name = (x_admin_name or "").strip()
    if not name:
        raise HTTPException(
            400,
            "X-Admin-Name обязателен — укажите, кто выполняет действие",
        )
    return name


class ConfirmedAction(BaseModel):
    confirm_email: str = ""  # обязателен, если аккаунт НЕ тестовый


async def require_test_account_or_confirmation(user_id: str, confirm_email: str) -> dict:
    """Разрешает разрушительное действие только если:
    - у пользователя is_test_account=1, ЛИБО
    - вызывающий явно подтвердил, набрав ПОЛНЫЙ email пользователя в confirm_email
      (сверка через hmac.compare_digest, регистронезависимо через .lower()).
    Иначе — 400 с понятным сообщением.
    Возвращает профиль пользователя (dict) при успехе.
    """
    user = await get_user_by_id(user_id)
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    if user.get("is_test_account"):
        return user
    if confirm_email and hmac.compare_digest(
        confirm_email.strip().lower(), user["email"].strip().lower()
    ):
        return user
    raise HTTPException(
        400,
        f"Это НЕ тестовый аккаунт. Чтобы выполнить действие на реальном пользователе, "
        f"повторите запрос с полем confirm_email, равным его email в точности.",
    )


# ————————————————— Модуль 3 — Инспектор Proration —————————————————

class ProrationCalcRequest(BaseModel):
    addon_type: str  # "devices_5" | "devices_10"
    tariff_slug: str = ""  # существующий slug из settings.tariffs, ИЛИ...
    total_days: Optional[int] = None      # ...либо вручную: срок тарифа
    discount_pct: Optional[float] = None  # ...и скидка, если tariff_slug не передан
    remaining_days: float = 0.0  # R — сколько дней осталось до конца периода


@admin_debug_router.post("/proration-calc")
async def debug_proration_calc(
    req: ProrationCalcRequest,
    x_admin_name: str = Depends(require_admin_name),
):
    """Read-only калькулятор proration.

    Численно повторяет реальную формулу из pricing.compute_addon_proration —
    единственный источник правды, чтобы дебаг и реальный purchase не могли
    разъехаться между копипастами. X-Admin-Name обязателен на всех POST раздела
    (п.2 чек-листа приёмки), хотя audit-лог тут не пишется — математика ничего не трогает.
    """
    # В задании (Модуль 3) написано, что этому read-only эндпоинту проверка не нужна,
    # но Чек-лист приёмки (п.1) требует 404 для ВСЕГО /admin/debug/* при выключенном
    # флаге — поэтому require_sandbox висит на роутере целиком (консервативно).
    addon_cfg = settings.device_addons.get(req.addon_type)
    if not addon_cfg:
        raise HTTPException(400, "Неизвестный тип add-on")

    if req.tariff_slug:
        tariff = settings.tariffs.get(req.tariff_slug)
        if not tariff:
            raise HTTPException(400, "Неизвестный тариф")
        total_days = tariff["days"]
        discount_pct = tariff.get("discount", 0)
    else:
        if req.total_days is None or req.discount_pct is None:
            raise HTTPException(400, "Укажите tariff_slug либо total_days+discount_pct")
        total_days = req.total_days
        discount_pct = req.discount_pct

    base_price = addon_cfg["base_price"]
    result = compute_addon_proration(base_price, discount_pct, total_days, req.remaining_days)
    remaining = max(0, req.remaining_days)
    next_recurring_price = math.ceil(base_price * (1 - discount_pct / 100))

    return {
        "addon_type": req.addon_type,
        "base_price": base_price,
        "discount_pct": discount_pct,
        "total_days": total_days,
        "remaining_days": remaining,
        "raw_before_ceil": round(result["raw"], 4),
        "price_now": result["price_now"],
        "rounding_delta": round(result["price_now"] - result["raw"], 4),
        "next_recurring_price": next_recurring_price,
    }


# ————————————————— Модуль 5 — Финансовый таймлайн пользователя —————————————————

@admin_debug_router.get("/timeline/{user_id}")
async def debug_timeline(user_id: str, limit: int = Query(100, le=500)):
    """Read-only проекция существующих данных (orders + device_addons +
    debug_audit_log) в единую хронологию для пользователя."""
    user = await get_user_by_id(user_id)
    if not user:
        raise HTTPException(404, "Пользователь не найден")

    events = []
    orders = await get_user_subscriptions(user_id)
    for o in orders:
        events.append({"ts": o["created_at"], "type": "order_created",
                        "detail": f"Заказ {o['id']} ({o['tariff']}), {o['amount']}₽"})
        if o.get("paid_at"):
            events.append({"ts": o["paid_at"], "type": "order_paid",
                            "detail": f"Заказ {o['id']} оплачен (tx={o.get('platega_tx_id')})"})
        if o.get("sub_url"):
            events.append({"ts": o.get("paid_at", o["created_at"]), "type": "key_issued",
                            "detail": f"Ключ выдан для заказа {o['id']}"})

        addons = await get_device_addons_for_order(o["id"])
        for a in addons:
            events.append({"ts": a["created_at"], "type": "addon_created",
                            "detail": f"Add-on {a['id']} ({a['addon_type']}, {a['amount_paid']}₽), "
                                      f"статус={a['status']}"})

    # + записи из debug_audit_log за этого пользователя
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM debug_audit_log WHERE target_user_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        )
        for row in await cur.fetchall():
            d = dict(row)
            events.append({"ts": d["created_at"], "type": f"debug:{d['action']}",
                            "detail": f"[{d['admin_name']}] {d['details_json']}"})

    events.sort(key=lambda e: e["ts"], reverse=True)
    return {"user_id": user_id, "email": user["email"], "events": events[:limit]}


# ————————————————— Модуль 2 — Управление жизненным циклом аддонов —————————————————

class AddonForceRequest(ConfirmedAction):
    addon_type: str


class AddonCancelForceRequest(ConfirmedAction):
    pass


def _order_base_devices(order: dict) -> int:
    tariff = settings.tariffs.get(order["tariff"])
    return tariff.get("devices", 5) if tariff else 5


@admin_debug_router.get("/user/{user_id}/devices")
async def debug_user_devices(user_id: str, order_id: str):
    """Текущее состояние: тарифный лимит, активные add-on'ы, лимит в БД vs реальный в 3x-UI."""
    order = await get_order(order_id)
    if not order or order["user_id"] != user_id:
        raise HTTPException(404, "Заказ не найден для этого пользователя")
    base_devices = _order_base_devices(order)
    extra = await get_total_extra_devices(order_id)
    addons = await get_device_addons_for_order(order_id)

    real_limit = None
    xui_error = None
    if order.get("xui_email"):
        try:
            info = await get_client_info(order["xui_email"])
            real_limit = info.get("limitIp")
        except XuiError as e:
            xui_error = str(e)

    return {
        "base_devices": base_devices,
        "extra_devices_db": extra,
        "total_expected": base_devices + extra,
        "real_limit_ip_in_3xui": real_limit,
        "in_sync": real_limit == base_devices + extra if real_limit is not None else None,
        "xui_error": xui_error,
        "addons": addons,
    }


@admin_debug_router.post("/user/{user_id}/devices/force-grant")
async def debug_force_grant_addon(user_id: str, order_id: str, req: AddonForceRequest,
                                   x_admin_name: str = Depends(require_admin_name)):
    """Принудительно выдаёт add-on, минуя pending/оплату.
    Только is_test_account или явный confirm_email."""
    user = await require_test_account_or_confirmation(user_id, req.confirm_email)
    order = await get_order(order_id)
    if not order or order["user_id"] != user_id:
        raise HTTPException(404, "Заказ не найден для этого пользователя")
    addon_cfg = settings.device_addons.get(req.addon_type)
    if not addon_cfg:
        raise HTTPException(400, "Неизвестный тип add-on")

    addon_id = uuid.uuid4().hex[:12]
    await create_device_addon(addon_id, user_id, order_id, req.addon_type,
                              addon_cfg["extra_devices"], 0, order.get("expires_at", ""),
                              platega_tx_id="")
    await activate_addon(addon_id)
    base_devices = _order_base_devices(order)
    extra = await get_total_extra_devices(order_id)
    new_limit = base_devices + extra
    if order.get("xui_email"):
        try:
            await update_client_limit(order["xui_email"], new_limit)
        except XuiError as e:
            raise HTTPException(502, f"3x-UI update failed: {e}")

    await log_debug_action(x_admin_name, "force_grant_addon", user_id, {
        "order_id": order_id, "addon_id": addon_id, "addon_type": req.addon_type,
        "new_limit": new_limit, "is_mock": True,
    })
    return {"ok": True, "addon_id": addon_id, "new_limit": new_limit}


@admin_debug_router.post("/user/{user_id}/devices/force-cancel-pending")
async def debug_force_cancel_pending(user_id: str, order_id: str, req: AddonCancelForceRequest,
                                      x_admin_name: str = Depends(require_admin_name)):
    """Имитирует нажатие «Отказаться» — переводит активный add-on в cancel_pending."""
    await require_test_account_or_confirmation(user_id, req.confirm_email)
    addon = await get_active_addon_for_order(order_id)
    if not addon or addon["status"] != "active":
        raise HTTPException(404, "Нет активного add-on для отмены")
    await cancel_pending_addon(addon["id"])
    await log_debug_action(x_admin_name, "force_cancel_pending", user_id,
                            {"order_id": order_id, "addon_id": addon["id"]})
    return {"ok": True, "addon_id": addon["id"], "status": "cancel_pending"}


@admin_debug_router.post("/user/{user_id}/devices/force-finalize-cancel")
async def debug_force_finalize_cancel(user_id: str, order_id: str, req: AddonCancelForceRequest,
                                       x_admin_name: str = Depends(require_admin_name)):
    """Немедленно завершает отмену: лимит -> база, все cancel_pending -> cancelled."""
    user = await require_test_account_or_confirmation(user_id, req.confirm_email)
    order = await get_order(order_id)
    if not order or order["user_id"] != user_id:
        raise HTTPException(404, "Заказ не найден")

    pending_cancels = [a for a in await get_device_addons_for_order(order_id)
                      if a["status"] == "cancel_pending"]
    base_devices = _order_base_devices(order)
    for a in pending_cancels:
        await finalize_addon_cancellation(a["id"])
    extra = await get_total_extra_devices(order_id)  # finalized cancelled уже не считает
    new_limit = base_devices + extra
    if order.get("xui_email"):
        try:
            await update_client_limit(order["xui_email"], new_limit)
        except XuiError as e:
            raise HTTPException(502, f"3x-UI update failed: {e}")

    await log_debug_action(x_admin_name, "force_finalize_cancel", user_id, {
        "order_id": order_id, "finalized": [a["id"] for a in pending_cancels],
        "new_limit": new_limit,
    })
    return {"ok": True, "new_limit": new_limit, "finalized_count": len(pending_cancels)}


@admin_debug_router.post("/user/{user_id}/devices/reset-all")
async def debug_reset_all_addons(user_id: str, order_id: str, req: AddonCancelForceRequest,
                                  x_admin_name: str = Depends(require_admin_name)):
    """Полная очистка add-on'ов заказа — ТОЛЬКО для order_id, не для всех заказов
    пользователя разом (случайный клик не должен затронуть все подписки сразу)."""
    user = await require_test_account_or_confirmation(user_id, req.confirm_email)
    order = await get_order(order_id)
    if not order or order["user_id"] != user_id:
        raise HTTPException(404, "Заказ не найден")

    async with _db() as db:
        await db.execute("DELETE FROM device_addons WHERE order_id = ?", (order_id,))
        await db.commit()

    base_devices = _order_base_devices(order)
    if order.get("xui_email"):
        try:
            await update_client_limit(order["xui_email"], base_devices)
        except XuiError as e:
            logger.warning("reset-all: failed to reset 3x-UI limit: %s", e)

    await log_debug_action(x_admin_name, "reset_all_addons", user_id, {"order_id": order_id})
    return {"ok": True, "reset_to": base_devices}


# ————————————————— Модуль 1 — Симулятор оплаты и вебхуков —————————————————

class MockPaymentRequest(ConfirmedAction):
    user_id: str
    purchase_type: str  # "subscription" | "devices_5" | "devices_10"
    order_id: Optional[str] = None  # обязателен для devices_5/devices_10
    tariff_slug: Optional[str] = None  # обязателен для purchase_type="subscription"


@admin_debug_router.post("/payment/mock-success")
async def debug_mock_success(req: MockPaymentRequest,
                             x_admin_name: str = Depends(require_admin_name)):
    """Симуляция успешного вебхука. Реальный провижининг в 3x-UI (НЕ мок),
    но без единого похода в Platega."""
    user = await require_test_account_or_confirmation(req.user_id, req.confirm_email)

    if req.purchase_type == "subscription":
        if not req.tariff_slug or req.tariff_slug not in settings.tariffs:
            raise HTTPException(400, "Укажите корректный tariff_slug")
        tariff = settings.tariffs[req.tariff_slug]
        new_order_id = uuid.uuid4().hex[:12]
        await create_order(new_order_id, req.tariff_slug, tariff["price"])
        await set_order_user(new_order_id, req.user_id)
        await mark_paid(new_order_id)  # is_mock не персистится отдельным полем — фиксируется в audit-логе
        from shared_state import fulfill_order  # лениво — избегаем циклического импорта
        await fulfill_order(new_order_id)  # реальный вызов провижининга в 3x-UI — НЕ мок
        result = {"order_id": new_order_id}
    else:
        if not req.order_id:
            raise HTTPException(400, "Для покупки add-on укажите order_id")
        order = await get_order(req.order_id)
        if not order or order["user_id"] != req.user_id:
            raise HTTPException(404, "Заказ не найден для этого пользователя")
        addon_cfg = settings.device_addons.get(req.purchase_type)
        if not addon_cfg:
            raise HTTPException(400, "purchase_type должен быть subscription/devices_5/devices_10")
        addon_id = uuid.uuid4().hex[:12]
        await create_device_addon(addon_id, req.user_id, req.order_id, req.purchase_type,
                                  addon_cfg["extra_devices"], addon_cfg["base_price"],
                                  order.get("expires_at", ""), platega_tx_id="mock")
        from shared_state import fulfill_addon  # лениво
        await fulfill_addon(addon_id)  # реальная активация + реальный update_client_limit
        result = {"addon_id": addon_id}

    await log_debug_action(x_admin_name, "mock_payment_success", req.user_id,
                            {**result, "purchase_type": req.purchase_type, "is_mock": True})
    return {"ok": True, **result}


class MockFailRequest(ConfirmedAction):
    user_id: str
    order_id: Optional[str] = None
    addon_id: Optional[str] = None


@admin_debug_router.post("/payment/mock-fail")
async def debug_mock_fail(req: MockFailRequest,
                          x_admin_name: str = Depends(require_admin_name)):
    """Переводит заказ/add-on в failed/cancelled и ПРОВЕРЯЕТ, что 3x-UI лимит не менялся."""
    user = await require_test_account_or_confirmation(req.user_id, req.confirm_email)

    before_limit = None
    if req.order_id:
        order = await get_order(req.order_id)
        if not order or order["user_id"] != req.user_id:
            raise HTTPException(404, "Заказ не найден")
        if order.get("xui_email"):
            try:
                before_limit = (await get_client_info(order["xui_email"])).get("limitIp")
            except XuiError:
                pass
        await mark_order_error(req.order_id, "Mock: payment failed (debug sandbox)")
    elif req.addon_id:
        addon = await get_addon_by_id(req.addon_id)
        if not addon or addon["user_id"] != req.user_id:
            raise HTTPException(404, "Add-on не найден")
        async with _db() as db:
            await db.execute("UPDATE device_addons SET status='cancelled' WHERE id=?", (req.addon_id,))
            await db.commit()
        order = await get_order(addon["order_id"])
        if order and order.get("xui_email"):
            try:
                before_limit = (await get_client_info(order["xui_email"])).get("limitIp")
            except XuiError:
                pass
    else:
        raise HTTPException(400, "Укажите order_id или addon_id")

    await log_debug_action(x_admin_name, "mock_payment_fail", req.user_id,
                            {"order_id": req.order_id, "addon_id": req.addon_id,
                             "limit_unchanged_check": before_limit, "is_mock": True})
    return {"ok": True, "limit_ip_at_check_time": before_limit,
            "note": "Сравните это значение с состоянием ДО вызова — лимит не должен был измениться"}


class MockPendingRequest(ConfirmedAction):
    user_id: str
    purchase_type: str
    order_id: Optional[str] = None


@admin_debug_router.post("/payment/mock-pending")
async def debug_mock_pending(req: MockPendingRequest,
                             x_admin_name: str = Depends(require_admin_name)):
    """Создаёт add-on заявку в pending — для проверки поведения фронта в состоянии ожидания."""
    user = await require_test_account_or_confirmation(req.user_id, req.confirm_email)

    if req.purchase_type == "subscription":
        raise HTTPException(400, "Для 'pending' сценария используйте обычный /api/order/create "
                                  "и просто не платите — заказ и так будет pending. "
                                  "Этот эндпоинт — только для add-on (у него нет отдельного "
                                  "публичного 'создать без оплаты' пути).")
    if not req.order_id:
        raise HTTPException(400, "Укажите order_id")
    order = await get_order(req.order_id)
    if not order or order["user_id"] != req.user_id:
        raise HTTPException(404, "Заказ не найден")
    addon_cfg = settings.device_addons.get(req.purchase_type)
    if not addon_cfg:
        raise HTTPException(400, "Неизвестный тип add-on")

    addon_id = uuid.uuid4().hex[:12]
    await create_device_addon(addon_id, req.user_id, req.order_id, req.purchase_type,
                              addon_cfg["extra_devices"], addon_cfg["base_price"],
                              order.get("expires_at", ""), platega_tx_id=f"mock_pending_{addon_id}")
    # НЕ вызываем fulfill_addon — остаётся pending, как настоящий неоплаченный заказ.
    # Проверить дальше вручную: GET /api/account/addon/{addon_id}/status должен
    # вернуть pending (т.к. check_status на фейковый tx_id из Platega вернёт ошибку —
    # это ОЖИДАЕМО и является частью теста: убедиться, что ошибка не приводит к
    # ложной активации, см. предыдущий раунд фиксов).

    await log_debug_action(x_admin_name, "mock_payment_pending", req.user_id,
                            {"addon_id": addon_id, "purchase_type": req.purchase_type, "is_mock": True})
    return {"ok": True, "addon_id": addon_id, "status": "pending",
            "note": "tx_id фейковый — polling-эндпоинт получит ошибку от Platega API "
                    "при попытке check_status, что ожидаемо и безопасно (см. try/except "
                    "в addon_status в main.py)"}


# ————————————————— Модуль 4 — Симулятор автопродления и Force Sync —————————————————

class RenewSimRequest(ConfirmedAction):
    order_id: str


@admin_debug_router.post("/billing/simulate-renew")
async def debug_simulate_renew(req: RenewSimRequest,
                               x_admin_name: str = Depends(require_admin_name)):
    """Вызывает РЕАЛЬНУЮ логику продления (ту же, что дергает пользовательский
    эндпоинт), но в обход rate-limit — это тестовый инструмент,
    не платёжный обход для реальных пользователей."""
    order = await get_order(req.order_id)
    if not order:
        raise HTTPException(404, "Заказ не найден")
    user = await require_test_account_or_confirmation(order["user_id"], req.confirm_email)

    from shared_state import _renew_subscription_core  # лениво
    result = await _renew_subscription_core(req.order_id, order)

    await log_debug_action(x_admin_name, "simulate_renew", order["user_id"],
                           {"order_id": req.order_id, "result": result, "is_mock": True})
    return {"ok": True, **result}


class ForceSyncRequest(ConfirmedAction):
    order_id: str


@admin_debug_router.get("/billing/force-sync-preview")
async def debug_force_sync_preview(order_id: str):
    """Read-only: показывает diff между ожидаемым и реальным лимитом, БЕЗ применения."""
    order = await get_order(order_id)
    if not order or not order.get("xui_email"):
        raise HTTPException(404, "Заказ не найден или нет привязки к 3x-UI")
    base_devices = _order_base_devices(order)
    extra = await get_total_extra_devices(order_id)
    expected = base_devices + extra
    try:
        real = (await get_client_info(order["xui_email"])).get("limitIp")
    except XuiError as e:
        raise HTTPException(502, f"3x-UI error: {e}")
    return {"expected_limit": expected, "real_limit": real, "in_sync": expected == real,
            "diff": expected - real if real is not None else None}


@admin_debug_router.post("/billing/force-sync-apply")
async def debug_force_sync_apply(req: ForceSyncRequest,
                                 x_admin_name: str = Depends(require_admin_name)):
    """Применяет синхронизацию ПОСЛЕ того, как preview был показан оператору."""
    order = await get_order(req.order_id)
    if not order:
        raise HTTPException(404, "Заказ не найден")
    user = await require_test_account_or_confirmation(order["user_id"], req.confirm_email)

    base_devices = _order_base_devices(order)
    extra = await get_total_extra_devices(req.order_id)
    expected = base_devices + extra
    if not order.get("xui_email"):
        raise HTTPException(400, "Нет привязки к 3x-UI клиенту")
    try:
        await update_client_limit(order["xui_email"], expected)
    except XuiError as e:
        raise HTTPException(502, f"3x-UI error: {e}")

    await log_debug_action(x_admin_name, "force_sync_apply", order["user_id"],
                           {"order_id": req.order_id, "new_limit": expected})
    return {"ok": True, "new_limit": expected}
