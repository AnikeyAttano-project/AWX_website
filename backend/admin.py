"""
Admin panel for AWX-WEB-lite.

Minimal order-management REST API protected by an API key.

Every route lives under the ``/admin`` prefix and requires the ``X-Admin-Key``
request header to match the ``ADMIN_API_KEY`` environment variable.

Endpoints:
    GET  /admin/orders              — paginated order list with status filter
    GET  /admin/orders/{id}         — single order details
    POST /admin/orders/{id}/retry   — re-run fulfill_order for failed orders
    GET  /admin/stats               — order statistics (counts + revenue)
    POST /admin/tariffs             — hot-replace the in-memory tariff catalogue
"""

import hmac
import logging
from typing import Optional

import aiosqlite
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from config import settings
from database import get_order

logger = logging.getLogger(__name__)


# ————————————————— Аутентификация —————————————————

async def require_admin(x_admin_key: str = Header(default="", alias="X-Admin-Key")):
    """
    FastAPI dependency that guards every admin route.

    Reads the ``X-Admin-Key`` request header and compares it in constant time
    with the value of the ``ADMIN_API_KEY`` environment variable.
    """
    if not settings.admin_api_key:
        raise HTTPException(
            status_code=503,
            detail="ADMIN_API_KEY is not configured on the server",
        )
    if not x_admin_key or not hmac.compare_digest(x_admin_key, settings.admin_api_key):
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing X-Admin-Key header",
        )


admin_router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)


# ————————————————— Модели —————————————————

class TariffItem(BaseModel):
    """A single tariff definition."""

    days: int = Field(ge=1, description="Validity period in days")
    price: float = Field(gt=0, description="Price in RUB")
    title: str = Field(min_length=1, description="Human-readable tariff name")
    devices: int = Field(ge=1, default=5, description="Allowed devices per subscription")
    discount: int = Field(ge=0, le=100, default=0, description="Discount percentage")


class TariffUpdateRequest(BaseModel):
    """Request body for POST /admin/tariffs."""

    tariffs: dict[str, TariffItem]


# ————————————————— Хелперы БД —————————————————

async def _count_orders(status: Optional[str] = None) -> int:
    """Return the total number of orders, optionally filtered by status."""
    query = "SELECT COUNT(*) AS cnt FROM orders"
    params: list = []
    if status:
        query += " WHERE status = ?"
        params.append(status)
    async with aiosqlite.connect(settings.database_path) as db:
        cur = await db.execute(query, params)
        row = await cur.fetchone()
    return int(row[0]) if row else 0


async def _list_orders(status: Optional[str], limit: int, offset: int) -> list[dict]:
    """Return a page of orders (newest first), optionally filtered by status."""
    query = "SELECT * FROM orders"
    params: list = []
    if status:
        query += " WHERE status = ?"
        params.append(status)
    query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(query, params)
        rows = await cur.fetchall()
    return [dict(row) for row in rows]


# ————————————————— Эндпоинты —————————————————

@admin_router.get("/orders")
async def list_orders(
    page: int = Query(1, ge=1, description="Page number, starting from 1"),
    page_size: int = Query(20, ge=1, le=100, description="Orders per page"),
    status: Optional[str] = Query(None, description="Filter by status: pending, paid, error"),
):
    """
    List orders with pagination and an optional status filter.

    Returns ``items`` (page of orders), plus ``total``, ``page``,
    ``page_size`` and ``pages`` for building a simple pager.
    """
    total = await _count_orders(status)
    items = await _list_orders(status, page_size, (page - 1) * page_size)
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": (total + page_size - 1) // page_size if total else 0,
    }


@admin_router.get("/orders/{order_id}")
async def get_order_detail(order_id: str):
    """Return full details for a single order, or 404 if it does not exist."""
    order = await get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


@admin_router.post("/orders/{order_id}/retry")
async def retry_order(order_id: str):
    """
    Retry provisioning for a failed order.

    Re-runs ``fulfill_order`` for orders stuck in ``error`` state or in
    ``paid`` state without a generated subscription URL. Returns the updated
    order after the attempt.
    """
    order = await get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.get("sub_url"):
        raise HTTPException(
            status_code=400,
            detail="Order already fulfilled (sub_url exists)",
        )
    if order["status"] not in ("error", "paid"):
        raise HTTPException(
            status_code=400,
            detail=f"Order status '{order['status']}' is not retryable",
        )

    # Lazy import to avoid a circular dependency: admin.py <-> main.py
    # (fulfill_order lives in main.py, which imports this router).
    from main import fulfill_order

    try:
        await fulfill_order(order_id)
    except Exception as e:
        logger.exception("Retry failed for order %s", order_id)
        raise HTTPException(status_code=500, detail=f"Retry failed: {e}")

    updated = await get_order(order_id)
    return {"ok": True, "order": updated}


@admin_router.get("/stats")
async def get_stats():
    """
    Basic order statistics.

    Returns total order count, per-status counts (paid / pending / errors)
    and total revenue from paid orders.
    """
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT
                COUNT(*) AS total_orders,
                COALESCE(SUM(CASE WHEN status = 'paid'    THEN 1 ELSE 0 END), 0) AS paid,
                COALESCE(SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END), 0) AS pending,
                COALESCE(SUM(CASE WHEN status = 'error'   THEN 1 ELSE 0 END), 0) AS errors,
                COALESCE(SUM(CASE WHEN status = 'paid'    THEN amount ELSE 0 END), 0) AS revenue
            FROM orders
            """
        )
        row = await cur.fetchone()
    return dict(row)


@admin_router.post("/tariffs")
async def update_tariffs(req: TariffUpdateRequest):
    """
    Replace the in-memory tariff catalogue.

    Takes effect immediately for the running process (hot-reload), but is
    NOT persisted — changes are lost on restart unless the same values are
    also written to ``.env`` / ``ADMIN_API_KEY``-style config.
    """
    if not req.tariffs:
        raise HTTPException(status_code=400, detail="tariffs dict cannot be empty")
    settings.tariffs = {slug: tariff.model_dump() for slug, tariff in req.tariffs.items()}
    logger.info("Admin updated tariffs: %s", list(settings.tariffs.keys()))
    return {"ok": True, "tariffs": settings.tariffs}
