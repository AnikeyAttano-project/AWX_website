"""
Full admin panel API for AWX-WEB-lite.

Every route lives under the ``/admin`` prefix and requires the ``X-Admin-Key``
request header to match the ``ADMIN_API_KEY`` environment variable.

Endpoints:
    GET    /admin/dashboard           — summary statistics
    GET    /admin/users               — paginated user list with search
    GET    /admin/users/{id}          — user profile + subscriptions
    POST   /admin/users/{id}/block    — block a user
    POST   /admin/users/{id}/unblock  — unblock a user
    GET    /admin/keys                — paginated key/subscription list
    POST   /admin/keys/{id}/extend    — extend a key on the panel
    POST   /admin/keys/{id}/delete    — delete a key from panel + DB
    GET    /admin/stats               — order statistics + charts data
    GET    /admin/settings            — current tariffs + trial + referral settings
    POST   /admin/settings/tariffs    — update and persist tariffs
    POST   /admin/settings/trial      — update trial settings
    POST   /admin/settings/referral   — update referral settings
    POST   /admin/settings/inbounds  — update available inbound IDs
    POST   /admin/tariffs             — (legacy) hot-replace tariffs + persist
"""

import hmac
import logging
import json
from typing import Optional
from datetime import datetime, timedelta

import aiosqlite
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from pydantic import BaseModel, Field

from config import settings
from database import (
    _db,
    get_order, save_settings_value,
    set_user_blocked, count_users, list_users_page, get_user_by_id,
    get_user_subscriptions, mark_order_deleted, set_devices_admin_addon,
    add_site_log, get_site_logs,
    create_promo_code, list_promo_codes, delete_promo_code, toggle_promo_code,
    get_analytics_funnel, get_analytics_by_tariff, get_analytics_anomalies,
)
from xui_client import _parse_inbound_ids

logger = logging.getLogger(__name__)


# -- Authentication --

async def require_admin(x_admin_key: str = Header(default="", alias="X-Admin-Key")):
    if not settings.admin_api_key:
        raise HTTPException(503, "ADMIN_API_KEY is not configured")
    if not x_admin_key or not hmac.compare_digest(x_admin_key, settings.admin_api_key):
        raise HTTPException(401, "Invalid or missing X-Admin-Key header")


admin_router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)


# -- Request models --

class ExtendKeyRequest(BaseModel):
    days: int = Field(ge=1, le=3650, description="Days to add")


class SetKeyDevicesRequest(BaseModel):
    total_devices: int = Field(5, ge=1, le=100, description="Итоговый лимит устройств (базовый + доп.)")


class TariffItem(BaseModel):
    days: int = Field(ge=1)
    price: float = Field(gt=0)
    title: str = Field(min_length=1)
    devices: int = Field(ge=1, default=5)
    discount: int = Field(ge=0, le=100, default=0)
    inbounds: list[int] = Field(default_factory=list)


class TariffUpdateRequest(BaseModel):
    tariffs: dict[str, TariffItem]


class TariffGroupItem(BaseModel):
    id: str = Field(min_length=1, max_length=50)
    title: str = Field(min_length=1, max_length=80)
    description: str = ""
    inbounds: list[int] = Field(default_factory=list)
    tariffs: list[str] = Field(default_factory=list)


class TariffGroupsRequest(BaseModel):
    groups: dict[str, TariffGroupItem]


class PromoCodeCreateRequest(BaseModel):
    code: str = Field(min_length=2, max_length=32)
    kind: str = Field(default="percent")  # 'percent' | 'fixed'
    value: float = Field(gt=0)
    max_uses: int = Field(ge=0, default=0)   # 0 = безлимит
    expires_at: str = ""                      # "YYYY-MM-DD HH:MM:SS" или пусто
    tariff_group: str = ""                    # id группы тарифов или пусто (все)


class BrandingRequest(BaseModel):
    site_name: str = Field(min_length=1, max_length=60)
    accent_color: str = Field(default="#1F5F52")
    support_contact: str = Field(default="", max_length=120)


class InboundsRequest(BaseModel):
    inbounds: list[int] = Field(default_factory=list)


class PaymentSettingsRequest(BaseModel):
    provider: str = Field(pattern="^(platega|yookassa)$")
    yookassa_shop_id: str = Field(default="", max_length=64)
    yookassa_secret_key: str = Field(default="", max_length=200)  # пусто = не менять


_ACCENT_RE = r"^#[0-9a-fA-F]{6}$"


def _validate_accent_color(color: str) -> None:
    import re
    if color and not re.match(_ACCENT_RE, color.strip()):
        raise HTTPException(400, "accent_color должен быть HEX-цветом вида #RRGGBB")


class TrialSettingsRequest(BaseModel):
    enabled: bool = True
    days: int = Field(ge=1, le=365, default=3)
    gb: int = Field(ge=0, default=25)
    devices: int = Field(ge=1, le=20, default=1)


class ReferralSettingsRequest(BaseModel):
    enabled: bool = True
    bonus_percent: int = Field(ge=0, le=100, default=10)


# -- DB helpers --

async def _scalar(query, params=()):
    async with _db() as db:
        cur = await db.execute(query, params)
        row = await cur.fetchone()
    return row[0] if row else 0


async def _fetchall(query, params=()):
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(query, params)
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


def _key_status(order: dict, now_str: str) -> str:
    """Classify a key/order into active/expired/error/pending."""
    if order.get("status") == "error":
        return "error"
    if order.get("status") == "deleted":
        return "deleted"
    if order.get("status") != "paid":
        return "pending"
    if not order.get("sub_url"):
        return "error"
    if order.get("expires_at") and order["expires_at"] <= now_str:
        return "expired"
    return "active"


# =====================================================================
#  DASHBOARD
# =====================================================================

@admin_router.get("/dashboard")
async def get_dashboard():
    now = datetime.utcnow()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    today_start = now.strftime("%Y-%m-%d 00:00:00")
    week_start = (now - timedelta(days=6)).strftime("%Y-%m-%d 00:00:00")
    month_start = (now - timedelta(days=29)).strftime("%Y-%m-%d 00:00:00")

    async with _db() as db:
        db.row_factory = aiosqlite.Row

        # Users
        row = (await (await db.execute("SELECT COUNT(*) FROM users")).fetchone())
        users_total = row[0]

        row = (await (await db.execute("SELECT COUNT(*) FROM users WHERE blocked=1")).fetchone())
        users_blocked = row[0]

        row = (await (await db.execute("SELECT COUNT(*) FROM users WHERE created_at >= ?", (today_start,))).fetchone())
        users_today = row[0]

        row = (await (await db.execute(
            "SELECT COUNT(*) FROM users WHERE created_at >= ?", (week_start,)
        )).fetchone())
        users_week = row[0]

        row = (await (await db.execute(
            "SELECT COUNT(*) FROM users WHERE trial_started_at IS NOT NULL"
        )).fetchone())
        trial_users = row[0]

        # Orders / keys summary
        row = (await (await db.execute("SELECT COUNT(*) FROM orders WHERE status != 'deleted'")).fetchone())
        orders_total = row[0]

        row = (await (await db.execute(
            "SELECT COUNT(*) FROM orders WHERE status='paid' AND status != 'deleted'"
        )).fetchone())
        orders_paid = row[0]

        row = (await (await db.execute(
            "SELECT COUNT(*) FROM orders WHERE status='pending'"
        )).fetchone())
        orders_pending = row[0]

        row = (await (await db.execute(
            "SELECT COUNT(*) FROM orders WHERE status='error'"
        )).fetchone())
        orders_error = row[0]

        # Revenue
        for label, start in [("today", today_start), ("week", week_start), ("month", month_start)]:
            row = (await (await db.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM orders WHERE status='paid' AND paid_at >= ?",
                (start,),
            )).fetchone())
            if label == "today":
                revenue_today = row[0]
            elif label == "week":
                revenue_week = row[0]
            else:
                revenue_month = row[0]

        row = (await (await db.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM orders WHERE status='paid'"
        )).fetchone())
        revenue_total = row[0]

        # Key status counts
        row = (await (await db.execute(
            "SELECT COUNT(*) FROM orders WHERE status='paid' AND sub_url IS NOT NULL "
            "AND (expires_at IS NULL OR expires_at > ?)",
            (now_str,),
        )).fetchone())
        keys_active = row[0]

        row = (await (await db.execute(
            "SELECT COUNT(*) FROM orders WHERE status='paid' AND sub_url IS NOT NULL "
            "AND expires_at IS NOT NULL AND expires_at <= ?",
            (now_str,),
        )).fetchone())
        keys_expired = row[0]

        # Recent orders (last 6)
        cur = await db.execute(
            """SELECT o.*, u.email AS user_email
               FROM orders o LEFT JOIN users u ON u.id = o.user_id
               WHERE o.status != 'deleted'
               ORDER BY o.created_at DESC LIMIT 6"""
        )
        recent = [dict(r) for r in await cur.fetchall()]

    return {
        "users": {"total": users_total, "blocked": users_blocked, "today": users_today, "week": users_week, "trial": trial_users},
        "orders": {"total": orders_total, "paid": orders_paid, "pending": orders_pending, "error": orders_error},
        "revenue": {"today": revenue_today, "week": revenue_week, "month": revenue_month, "total": revenue_total},
        "keys": {"active": keys_active, "expired": keys_expired},
        "trial_enabled": settings.trial_enabled,
        "recent_orders": recent,
    }


# =====================================================================
#  USERS
# =====================================================================

@admin_router.get("/users")
async def list_users(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    q: Optional[str] = Query(None),
):
    search = (q or "").strip()
    total = await count_users(search)
    items = await list_users_page(search, page_size, (page - 1) * page_size)
    # Strip password_hash from response
    for item in items:
        item.pop("password_hash", None)
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": (total + page_size - 1) // page_size if total else 0,
    }


@admin_router.get("/users/{user_id}")
async def get_user_detail(user_id: str):
    user = await get_user_by_id(user_id)
    if not user:
        raise HTTPException(404, "User not found")
    user.pop("password_hash", None)

    subs = await get_user_subscriptions(user_id)
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    for s in subs:
        s["key_status"] = _key_status(s, now_str)

    user["subscriptions"] = subs

    # Referral info
    from database import count_referrals, ensure_referral_code
    user["referral_code"] = await ensure_referral_code(user_id)
    user["referral_count"] = await count_referrals(user_id)
    user.pop("password_hash", None)
    return user


@admin_router.post("/users/{user_id}/block")
async def block_user(user_id: str):
    user = await get_user_by_id(user_id)
    if not user:
        raise HTTPException(404, "User not found")
    await set_user_blocked(user_id, True)
    return {"ok": True, "blocked": True}


@admin_router.post("/users/{user_id}/unblock")
async def unblock_user(user_id: str):
    user = await get_user_by_id(user_id)
    if not user:
        raise HTTPException(404, "User not found")
    await set_user_blocked(user_id, False)
    return {"ok": True, "blocked": False}


# =====================================================================
#  KEYS (subscriptions / orders with VPN clients)
# =====================================================================

@admin_router.get("/keys")
async def list_keys(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: Optional[str] = Query(None, description="active|expired|error|pending|deleted"),
    q: Optional[str] = Query(None, description="search by id, email, tariff, sub_url"),
):
    now = datetime.utcnow()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    search = (q or "").strip()

    # Build base query with JOIN for user email
    where_parts = ["o.status != 'deleted'"]
    params: list = []

    if status:
        if status == "active":
            where_parts.append("o.status = 'paid' AND o.sub_url IS NOT NULL AND (o.expires_at IS NULL OR o.expires_at > ?)")
            params.append(now_str)
        elif status == "expired":
            where_parts.append("o.status = 'paid' AND o.sub_url IS NOT NULL AND o.expires_at IS NOT NULL AND o.expires_at <= ?")
            params.append(now_str)
        elif status == "error":
            where_parts.append("(o.status = 'error' OR (o.status = 'paid' AND o.sub_url IS NULL))")
        elif status == "pending":
            where_parts.append("o.status = 'pending'")
        elif status == "deleted":
            where_parts = ["o.status = 'deleted'"]

    if search:
        where_parts.append(
            "(o.id LIKE ? OR o.xui_email LIKE ? OR o.tariff LIKE ? OR o.sub_url LIKE ? OR u.email LIKE ?)"
        )
        like = f"%{search}%"
        params.extend([like, like, like, like, like])

    where_sql = " AND ".join(where_parts)
    count_sql = f"SELECT COUNT(*) FROM orders o LEFT JOIN users u ON u.id = o.user_id WHERE {where_sql}"
    data_sql = f"""SELECT o.*, u.email AS user_email
                   FROM orders o LEFT JOIN users u ON u.id = o.user_id
                   WHERE {where_sql}
                   ORDER BY o.created_at DESC LIMIT ? OFFSET ?"""

    total = await _scalar(count_sql, params)
    items = await _fetchall(data_sql, params + [page_size, (page - 1) * page_size])

    for item in items:
        item["key_status"] = _key_status(item, now_str)

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": (total + page_size - 1) // page_size if total else 0,
    }


@admin_router.post("/keys/{order_id}/extend")
async def extend_key(order_id: str, req: ExtendKeyRequest):
    order = await get_order(order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    if not order.get("xui_email"):
        raise HTTPException(400, "No 3x-UI client linked to this order")
    if order["status"] not in ("paid", "error"):
        raise HTTPException(400, f"Cannot extend key with status '{order['status']}'")

    from xui_client import renew_client, XuiError

    try:
        result = await renew_client(order["xui_email"], req.days)
        new_expires = datetime.utcfromtimestamp(
            result["new_expiry_ms"] / 1000
        ).strftime("%Y-%m-%d %H:%M:%S")
    except XuiError as e:
        logger.error("Extend key failed for %s: %s", order_id, e)
        raise HTTPException(502, f"Panel error: {e}")

    async with _db() as db:
        await db.execute(
            "UPDATE orders SET expires_at = ?, status = 'paid', error_msg = NULL WHERE id = ?",
            (new_expires, order_id),
        )
        await db.commit()

    await add_site_log("admin_extend", actor="admin",
                       details=f"order={order_id} days={req.days} new_expires={new_expires}")
    return {"ok": True, "new_expires_at": new_expires}


@admin_router.post("/keys/{order_id}/devices")
async def set_key_devices(order_id: str, req: SetKeyDevicesRequest):
    """Тестовый инструмент: задаёт итоговый лимит устройств ключа в 3x-UI.

    Синхронизирует admin-аддон (devices_admin), чтобы личный кабинет показывал
    тот же итоговый лимит, что и панель. Реальные купленные addon'ы не трогает.
    """
    order = await get_order(order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    if not order.get("xui_email"):
        raise HTTPException(400, "No 3x-UI client linked to this order")

    tariff = settings.tariffs.get(order["tariff"])
    base_devices = tariff.get("devices", 5) if tariff else 5
    if req.total_devices < base_devices:
        raise HTTPException(
            400,
            f"Нельзя меньше базового лимита тарифа ({base_devices} устройств)",
        )

    from xui_client import update_client_limit, XuiError

    try:
        await update_client_limit(order["xui_email"], req.total_devices)
    except XuiError as e:
        logger.error("Set devices failed for %s: %s", order_id, e)
        raise HTTPException(502, f"Panel error: {e}")

    await set_devices_admin_addon(
        order_id,
        order.get("user_id") or "",
        req.total_devices - base_devices,
        order.get("expires_at"),
    )

    await add_site_log("admin_set_devices", actor="admin",
                       details=f"order={order_id} total_devices={req.total_devices} "
                               f"(base={base_devices} extra={req.total_devices - base_devices})")

    return {
        "ok": True,
        "total_devices": req.total_devices,
        "base_devices": base_devices,
        "extra_devices": req.total_devices - base_devices,
    }


@admin_router.post("/keys/{order_id}/delete")
async def delete_key(order_id: str, force: bool = Query(False)):
    order = await get_order(order_id)
    if not order:
        raise HTTPException(404, "Order not found")

    # Attempt to delete from 3x-UI panel
    if order.get("xui_email"):
        from xui_client import delete_client, XuiError
        try:
            await delete_client(order["xui_email"])
        except XuiError as e:
            if not force:
                raise HTTPException(
                    409, detail=f"Panel unavailable: {e}. Re-check with ?force=true to delete from DB only."
                )
            logger.warning("Force delete: panel error for %s: %s", order_id, e)

    await mark_order_deleted(order_id)
    return {"ok": True, "force": force}


# =====================================================================
#  STATISTICS
# =====================================================================

@admin_router.get("/stats")
async def get_stats():
    now = datetime.utcnow()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    month_start = (now - timedelta(days=29)).strftime("%Y-%m-%d 00:00:00")
    week_start = (now - timedelta(days=6)).strftime("%Y-%m-%d 00:00:00")

    async with _db() as db:
        db.row_factory = aiosqlite.Row

        # Basic counts
        row = (await (await db.execute(
            """SELECT
                COUNT(*) AS total_orders,
                COALESCE(SUM(CASE WHEN status='paid' THEN 1 ELSE 0 END), 0) AS paid,
                COALESCE(SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END), 0) AS pending,
                COALESCE(SUM(CASE WHEN status='error' THEN 1 ELSE 0 END), 0) AS errors,
                COALESCE(SUM(CASE WHEN status='paid' THEN amount ELSE 0 END), 0) AS revenue
            FROM orders WHERE status != 'deleted'"""
        )).fetchone())
        basic = dict(row)

        # Conversion
        total_created = basic["total_orders"]
        total_paid = basic["paid"]
        conversion = round(total_paid / total_created * 100, 1) if total_created > 0 else 0.0

        # Orders by day (last 30 days)
        cur = await db.execute(
            """SELECT date(created_at) AS day,
                      COUNT(*) AS orders,
                      COALESCE(SUM(CASE WHEN status='paid' THEN amount ELSE 0 END), 0) AS revenue
               FROM orders
               WHERE created_at >= ? AND status != 'deleted'
               GROUP BY day ORDER BY day""",
            (month_start,),
        )
        orders_by_day = [dict(r) for r in await cur.fetchall()]

        # New users by day (last 14 days)
        users_start = (now - timedelta(days=13)).strftime("%Y-%m-%d 00:00:00")
        cur = await db.execute(
            """SELECT date(created_at) AS day, COUNT(*) AS users
               FROM users WHERE created_at >= ?
               GROUP BY day ORDER BY day""",
            (users_start,),
        )
        users_by_day = [dict(r) for r in await cur.fetchall()]

        # Top tariffs
        cur = await db.execute(
            """SELECT tariff,
                      COUNT(*) AS orders,
                      COALESCE(SUM(CASE WHEN status='paid' THEN amount ELSE 0 END), 0) AS revenue
               FROM orders WHERE status != 'deleted'
               GROUP BY tariff ORDER BY orders DESC"""
        )
        top_tariffs = [dict(r) for r in await cur.fetchall()]

        # Key status distribution
        cur = await db.execute(
            """SELECT
                SUM(CASE WHEN status='paid' AND sub_url IS NOT NULL AND (expires_at IS NULL OR expires_at > ?) THEN 1 ELSE 0 END) AS active,
                SUM(CASE WHEN status='paid' AND sub_url IS NOT NULL AND expires_at IS NOT NULL AND expires_at <= ? THEN 1 ELSE 0 END) AS expired,
                SUM(CASE WHEN status='error' OR (status='paid' AND sub_url IS NULL) THEN 1 ELSE 0 END) AS error,
                SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending
            FROM orders""",
            (now_str, now_str),
        )
        key_dist_row = (await cur.fetchone())
        key_distribution = dict(key_dist_row) if key_dist_row else {}

        # Revenue by day (last 30 days)
        cur = await db.execute(
            """SELECT date(paid_at) AS day, SUM(amount) AS revenue
               FROM orders
               WHERE status='paid' AND paid_at IS NOT NULL AND paid_at >= ?
               GROUP BY day ORDER BY day""",
            (month_start,),
        )
        revenue_by_day = [dict(r) for r in await cur.fetchall()]

    return {
        "basic": basic,
        "conversion": conversion,
        "orders_by_day": orders_by_day,
        "revenue_by_day": revenue_by_day,
        "users_by_day": users_by_day,
        "top_tariffs": top_tariffs,
        "key_distribution": key_distribution,
    }


# =====================================================================
#  SETTINGS
# =====================================================================

def _validate_inbounds(values: list[int]) -> None:
    """Инбаунды тарифа/группы должны быть подмножеством доступных (XUI_INBOUND_IDS)."""
    from xui_client import _parse_inbound_ids
    available = set(_parse_inbound_ids())
    bad = [int(x) for x in values if int(x) not in available]
    if bad:
        raise HTTPException(
            400,
            f"Инбаунды {bad} недоступны. Доступные: {sorted(available)}",
        )


@admin_router.get("/settings")
async def get_settings():
    return {
        "tariffs": settings.tariffs,
        "tariff_groups": settings.tariff_groups,
        "branding": settings.branding,
        "payment": {
            "provider": settings.payment_provider,
            "yookassa_shop_id": settings.yookassa_shop_id,
            "has_yookassa_secret": bool(settings.yookassa_secret_key),
        },
        "available_inbounds": _parse_inbound_ids(),
        "trial": {
            "enabled": settings.trial_enabled,
            "days": settings.trial_days,
            "gb": settings.trial_gb,
            "devices": settings.trial_devices,
        },
        "referral": await _referral_settings_dict(),
        "demo_mode": settings.demo_mode,
    }


@admin_router.post("/settings/tariffs")
async def update_tariffs(req: TariffUpdateRequest):
    if not req.tariffs:
        raise HTTPException(400, "Tariffs cannot be empty")

    for slug, t in req.tariffs.items():
        _validate_inbounds(t.inbounds)

    tariffs_dict = {slug: tariff.model_dump() for slug, tariff in req.tariffs.items()}
    settings.tariffs = tariffs_dict
    await save_settings_value("tariffs", tariffs_dict)
    logger.info("Admin updated tariffs: %s", list(tariffs_dict.keys()))
    return {"ok": True, "tariffs": tariffs_dict}


@admin_router.post("/settings/tariff_groups")
async def update_tariff_groups(req: TariffGroupsRequest):
    """Сохранить группы тарифов. Валидация: тарифы существуют, инбаунды доступны."""
    for gid, g in req.groups.items():
        if g.id != gid:
            raise HTTPException(400, f"Ключ группы '{gid}' не совпадает с id '{g.id}'")
        unknown = [s for s in g.tariffs if s not in settings.tariffs]
        if unknown:
            raise HTTPException(400, f"Группа '{gid}': тарифы {unknown} не существуют")
        _validate_inbounds(g.inbounds)

    groups_dict = {gid: g.model_dump() for gid, g in req.groups.items()}
    settings.tariff_groups = groups_dict
    await save_settings_value("tariff_groups", groups_dict)
    logger.info("Admin updated tariff groups: %s", list(groups_dict.keys()))
    return {"ok": True, "tariff_groups": groups_dict}


@admin_router.post("/settings/branding")
async def update_branding(req: BrandingRequest):
    if not req.site_name.strip():
        raise HTTPException(400, "site_name не может быть пустым")
    _validate_accent_color(req.accent_color)
    branding = {
        "site_name": req.site_name.strip(),
        "accent_color": req.accent_color.strip(),
        "support_contact": req.support_contact.strip(),
    }
    settings.branding = branding
    await save_settings_value("branding", branding)
    logger.info("Admin updated branding: site_name=%s", branding["site_name"])
    return {"ok": True, "branding": branding}


@admin_router.post("/settings/inbounds")
async def update_inbounds(req: InboundsRequest):
    """Обновить пул доступных инбаундов. Сохраняется в БД."""
    settings.available_inbounds = req.inbounds
    await save_settings_value("available_inbounds", req.inbounds)
    logger.info("Admin updated inbounds: %s", req.inbounds)
    return {"ok": True, "available_inbounds": req.inbounds}


@admin_router.post("/settings/payment")
async def update_payment(req: PaymentSettingsRequest):
    """Выбор активного провайдера + настройки ЮKassa. Секрет не отдаётся в GET."""
    settings.payment_provider = req.provider
    if req.yookassa_shop_id.strip():
        settings.yookassa_shop_id = req.yookassa_shop_id.strip()
    if req.yookassa_secret_key.strip():
        settings.yookassa_secret_key = req.yookassa_secret_key.strip()
    if settings.payment_provider == "yookassa" and (
        not settings.yookassa_shop_id or not settings.yookassa_secret_key
    ):
        raise HTTPException(400, "Для ЮKassa нужны shop_id и secret_key")
    payment_cfg = {
        "provider": settings.payment_provider,
        "yookassa_shop_id": settings.yookassa_shop_id,
        "yookassa_secret_key": settings.yookassa_secret_key,
    }
    await save_settings_value("payment", payment_cfg)
    logger.info("Admin updated payment provider: %s", settings.payment_provider)
    return {"ok": True, "payment": {"provider": settings.payment_provider}}


# -- Экспорт / импорт настроек (#7) --

async def _referral_settings_dict() -> dict:
    return {
        "enabled": (await _scalar("SELECT value FROM referral_settings WHERE key='referral_enabled'")) == "1",
        "bonus_percent": int(await _scalar("SELECT value FROM referral_settings WHERE key='bonus_percent'") or 10),
        "level2_percent": int(await _scalar("SELECT value FROM referral_settings WHERE key='level2_percent'") or 0),
        "level3_percent": int(await _scalar("SELECT value FROM referral_settings WHERE key='level3_percent'") or 0),
    }


@admin_router.get("/settings/export")
async def export_settings():
    """Полный бэкап настроек: тарифы, группы, брендинг, триал, рефералка, промо."""
    data = {
        "version": 1,
        "exported_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "tariffs": settings.tariffs,
        "tariff_groups": settings.tariff_groups,
        "branding": settings.branding,
        "trial": {
            "enabled": settings.trial_enabled,
            "days": settings.trial_days,
            "gb": settings.trial_gb,
            "devices": settings.trial_devices,
        },
        "referral": await _referral_settings_dict(),
        "promo_codes": await list_promo_codes(),
    }
    return data


class ImportSettingsRequest(BaseModel):
    tariffs: dict[str, TariffItem] | None = None
    tariff_groups: dict[str, TariffGroupItem] | None = None
    branding: BrandingRequest | None = None
    trial: dict | None = None
    referral: dict | None = None
    promo_codes: list[dict] | None = None


@admin_router.post("/settings/import")
async def import_settings(req: ImportSettingsRequest):
    imported = []

    if req.tariffs is not None:
        if not req.tariffs:
            raise HTTPException(400, "tariffs не может быть пустым")
        for t in req.tariffs.values():
            _validate_inbounds(t.inbounds)
        settings.tariffs = {slug: t.model_dump() for slug, t in req.tariffs.items()}
        await save_settings_value("tariffs", settings.tariffs)
        imported.append("tariffs")

    if req.tariff_groups is not None:
        for gid, g in req.tariff_groups.items():
            if g.id != gid:
                raise HTTPException(400, f"Ключ группы '{gid}' не совпадает с id")
            unknown = [s for s in g.tariffs if s not in settings.tariffs]
            if unknown:
                raise HTTPException(400, f"Группа '{gid}': тарифы {unknown} не существуют")
            _validate_inbounds(g.inbounds)
        settings.tariff_groups = {gid: g.model_dump() for gid, g in req.tariff_groups.items()}
        await save_settings_value("tariff_groups", settings.tariff_groups)
        imported.append("tariff_groups")

    if req.branding is not None:
        if not req.branding.site_name.strip():
            raise HTTPException(400, "site_name не может быть пустым")
        _validate_accent_color(req.branding.accent_color)
        settings.branding = req.branding.model_dump()
        await save_settings_value("branding", settings.branding)
        imported.append("branding")

    if req.trial is not None:
        t = req.trial
        trial_val = {
            "enabled": bool(t.get("enabled", settings.trial_enabled)),
            "days": int(t.get("days", settings.trial_days)),
            "gb": int(t.get("gb", settings.trial_gb)),
            "devices": int(t.get("devices", settings.trial_devices)),
        }
        settings.trial_enabled = trial_val["enabled"]
        settings.trial_days = trial_val["days"]
        settings.trial_gb = trial_val["gb"]
        settings.trial_devices = trial_val["devices"]
        await save_settings_value("trial", trial_val)
        imported.append("trial")

    if req.referral is not None:
        r = req.referral
        vals = {
            "referral_enabled": "1" if bool(r.get("enabled")) else "0",
            "bonus_percent": str(int(r.get("bonus_percent", 10) or 10)),
            "level2_percent": str(int(r.get("level2_percent", 0) or 0)),
            "level3_percent": str(int(r.get("level3_percent", 0) or 0)),
        }
        async with _db() as db:
            for k, v in vals.items():
                await db.execute(
                    "INSERT INTO referral_settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (k, v)
                )
            await db.commit()
        imported.append("referral")

    if req.promo_codes is not None:
        added = 0
        for p in req.promo_codes:
            code = str(p.get("code", "")).strip().upper()
            if not code:
                continue
            kind = p.get("kind", "percent")
            value = float(p.get("value", 0))
            if kind not in ("percent", "fixed") or value <= 0:
                continue
            try:
                await create_promo_code(
                    code=code, kind=kind, value=value,
                    max_uses=int(p.get("max_uses", 0)),
                    expires_at=p.get("expires_at") or None,
                    tariff_group=p.get("tariff_group") or None,
                )
                added += 1
            except ValueError:
                pass  # уже существует
        imported.append(f"promo_codes(+{added})")

    if not imported:
        raise HTTPException(400, "Пустой файл импорта")
    await add_site_log("settings_import", actor="admin", details=", ".join(imported))
    return {"ok": True, "imported": imported}


# -- Промо-коды --

def _validate_promo_expiry(expires_at: str) -> str | None:
    """Принимает пустую строку или 'YYYY-MM-DD HH:MM:SS'; иначе HTTPException 400."""
    if not expires_at.strip():
        return None
    try:
        datetime.strptime(expires_at.strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        raise HTTPException(400, "expires_at должен быть в формате 'YYYY-MM-DD HH:MM:SS'")
    return expires_at.strip()


@admin_router.get("/promo")
async def admin_list_promo():
    return {"items": await list_promo_codes()}


@admin_router.post("/promo")
async def admin_create_promo(req: PromoCodeCreateRequest):
    if req.kind not in ("percent", "fixed"):
        raise HTTPException(400, "kind должен быть 'percent' или 'fixed'")
    if req.kind == "percent" and (req.value <= 0 or req.value > 100):
        raise HTTPException(400, "Процент скидки — от 0 до 100")
    if req.tariff_group and req.tariff_group not in settings.tariff_groups:
        raise HTTPException(400, f"Группа тарифов '{req.tariff_group}' не существует")
    try:
        promo = await create_promo_code(
            code=req.code, kind=req.kind, value=req.value,
            max_uses=req.max_uses,
            expires_at=_validate_promo_expiry(req.expires_at),
            tariff_group=req.tariff_group or None,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    await add_site_log("promo_create", actor="admin", details=promo["code"])
    return {"ok": True, "item": promo}


@admin_router.post("/promo/{promo_id}/toggle")
async def admin_toggle_promo(promo_id: int):
    item = await toggle_promo_code(promo_id)
    if not item:
        raise HTTPException(404, "Промокод не найден")
    await add_site_log("promo_toggle", actor="admin",
                       details=f"{item['code']} active={item['is_active']}")
    return {"ok": True, "item": item}


@admin_router.post("/promo/{promo_id}/delete")
async def admin_delete_promo(promo_id: int):
    if not await delete_promo_code(promo_id):
        raise HTTPException(404, "Промокод не найден")
    await add_site_log("promo_delete", actor="admin", details=str(promo_id))
    return {"ok": True}


@admin_router.post("/settings/trial")
async def update_trial(req: TrialSettingsRequest):
    settings.trial_enabled = req.enabled
    settings.trial_days = req.days
    settings.trial_gb = req.gb
    settings.trial_devices = req.devices

    await save_settings_value("trial", {
        "enabled": req.enabled,
        "days": req.days,
        "gb": req.gb,
        "devices": req.devices,
    })
    logger.info("Admin updated trial: enabled=%s", req.enabled)
    return {"ok": True}


@admin_router.post("/settings/referral")
async def update_referral(req: ReferralSettingsRequest):
    enabled_val = "1" if req.enabled else "0"

    async with _db() as db:
        await db.execute(
            "INSERT INTO referral_settings (key, value) VALUES ('referral_enabled', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (enabled_val,),
        )
        await db.execute(
            "INSERT INTO referral_settings (key, value) VALUES ('bonus_percent', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(req.bonus_percent),),
        )
        await db.commit()

    logger.info("Admin updated referral: enabled=%s, percent=%s", req.enabled, req.bonus_percent)
    return {"ok": True}


@admin_router.post("/settings/demo")
async def update_demo(enabled: bool = Query(...)):
    settings.demo_mode = enabled
    await save_settings_value("demo_mode", {"enabled": enabled})
    logger.info("Admin updated demo_mode: %s", enabled)
    return {"ok": True, "demo_mode": enabled}


@admin_router.post("/cleanup")
async def admin_cleanup():
    """Ручной запуск очистки устаревших записей (>14 дней после окончания)."""
    from shared_state import cleanup_expired_subscriptions
    await cleanup_expired_subscriptions()
    return {"ok": True, "message": "Cleanup completed. Check server logs for details."}


# -- Legacy endpoint (kept for backwards compat, now also persists) --

@admin_router.post("/tariffs")
async def legacy_update_tariffs(req: TariffUpdateRequest):
    return await update_tariffs(req)


# -- Общий лог действий сайта --

@admin_router.get("/logs")
async def admin_logs(limit: int = Query(200, ge=10, le=5000)):
    """Последние N записей общего лога действий сайта (свежие сверху)."""
    items = await get_site_logs(limit)
    return {"items": items, "total": len(items), "limit": limit}


@admin_router.get("/logs/download")
async def admin_logs_download(limit: int = Query(200, ge=10, le=5000)):
    """Скачать лог действий сайта как текстовый файл."""
    items = await get_site_logs(limit)
    lines = [
        f"{r['ts']} [{r['level'].upper()}] {r['action']} | actor={r['actor'] or '-'} | {r['details'] or ''}"
        for r in items
    ]
    text = "Общий лог действий AWX-WEB-lite (последние %d записей)\n%s\n%s\n" % (
        len(items), "-" * 70, "\n".join(lines),
    )
    return Response(
        content=text,
        media_type="text/plain",
        headers={"Content-Disposition": 'attachment; filename="site_logs.txt"'},
    )


# ————————————————— АНАЛИТИКА (витрина на основе site_log) —————————————————

@admin_router.get("/analytics/funnel")
async def analytics_funnel(days: int = Query(7, ge=1, le=90)):
    """
    Воронка: order_create → order_paid → fulfill, по дням, за последние `days`
    дней. Конверсия в процентах на каждом переходе.
    """
    return await get_analytics_funnel(days)


@admin_router.get("/analytics/by-tariff")
async def analytics_by_tariff(days: int = Query(30, ge=1, le=365)):
    """Разбивка оплаченных заказов по тарифам — количество и выручка."""
    return await get_analytics_by_tariff(days)


@admin_router.get("/analytics/anomalies")
async def analytics_anomalies():
    """
    Простая эвристика: пользователи с аномально частым 'renew' (>3 за 7 дней)
    или 'addon_purchase' (>5 за день) — потенциальные баги или злоупотребления.
    """
    return await get_analytics_anomalies()
