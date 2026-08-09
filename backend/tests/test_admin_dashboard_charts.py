"""
Ит.5 «Сводка и графики»: контракты данных для dashboard/stats/analytics.

- GET /admin/dashboard — orders_week (серия для компактного графика на Сводке).
- GET /admin/stats — orders_by_day / revenue_by_day / users_by_day / top_tariffs /
  key_distribution (данные для читаемых графиков).
- GET /admin/analytics/funnel — rows/totals для воронки.

Фронт (admin.html) читает эти поля: пустые состояния и оси строятся на их основе.
"""
import uuid

from database import _db, create_user

ADMIN_HEADERS = {"X-Admin-Key": "test-admin-key-0123456789abcdefghijklmnopqrstuvwxyz"}


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


async def _seed_order(oid, status, user_id=None, paid=True, days_ago=0):
    paid_at = f"datetime('now', '-{days_ago} days')" if paid and status == "paid" else "NULL"
    async with _db() as db:
        await db.execute(
            f"INSERT INTO orders (id, tariff, amount, status, user_id, paid_at, xui_email, sub_url) "
            f"VALUES (?, 'quantum_month', 300, ?, ?, {paid_at}, ?, ?)",
            (oid, status, user_id,
             f"{oid}@vpn.local" if status == "paid" else None,
             f"https://sub.local/{oid}" if status == "paid" else None),
        )
        await db.commit()


def test_dashboard_orders_week(client):
    uid = uuid.uuid4().hex[:12]
    asyncio_run(create_user(uid, f"dash-{uid}@vpn.local", "hash", verified=1))
    asyncio_run(_seed_order("dw-1", "paid", uid))
    asyncio_run(_seed_order("dw-2", "pending", uid))
    asyncio_run(_seed_order("dw-del", "deleted", uid))  # deleted не считается

    r = client.get("/admin/dashboard", headers=ADMIN_HEADERS)
    assert r.status_code == 200, r.text
    d = r.json()
    assert isinstance(d["orders_week"], list)
    assert d["orders_week"], "orders_week не должен быть пустым после сидирования заказов"
    row = d["orders_week"][0]
    assert row["orders"] == 2, "deleted-заказ не должен попадать в orders_week"
    assert row["revenue"] == 300, "доход считается только по paid-заказам"


def test_stats_series_contracts(client):
    uid = uuid.uuid4().hex[:12]
    asyncio_run(create_user(uid, f"stats-{uid}@vpn.local", "hash", verified=1))
    asyncio_run(_seed_order("st-1", "paid", uid))
    asyncio_run(_seed_order("st-2", "paid", uid, days_ago=1))

    r = client.get("/admin/stats", headers=ADMIN_HEADERS)
    assert r.status_code == 200, r.text
    d = r.json()

    # Все серии — списки записей с ключами, которые читает фронт
    for key, valkey in [("orders_by_day", "orders"), ("revenue_by_day", "revenue"),
                        ("users_by_day", "users")]:
        assert isinstance(d.get(key), list), f"{key} должен быть списком"
        assert d[key], f"{key} не должен быть пустым при наличии заказов"
        assert valkey in d[key][0], f"запись {key} должна содержать '{valkey}'"

    assert d["top_tariffs"], "top_tariffs не должен быть пустым"
    assert "key_distribution" in d and isinstance(d["key_distribution"], dict)
    assert d["basic"]["paid"] == 2
    assert d["conversion"] == 100.0


def test_dashboard_and_funnel_empty_ok(client):
    """Пустая БД: эндпоинты отдают пустые списки (фронт рисует пустые состояния)."""
    r = client.get("/admin/dashboard", headers=ADMIN_HEADERS)
    assert r.status_code == 200, r.text
    assert r.json()["orders_week"] == []

    r = client.get("/admin/analytics/funnel?days=7", headers=ADMIN_HEADERS)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["rows"] == []
    assert d["totals"]["order_create"] == 0
