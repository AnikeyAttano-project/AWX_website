"""
Часть 5: дашборд аналитики на основе site_log.

Проверяем, что эндпоинты отдают корректный JSON на тестовой БД с синтетическими
записями site_log, и что вкладка «Анализ аномалий» реально ловит баг из Части 2
(5 записей action='renew' одного actor за 7 дней → аномалия).
"""
import uuid

from database import (
    add_site_log, _db, create_order, set_order_user,
)

ADMIN_HEADERS = {"X-Admin-Key": "test-admin-key-0123456789abcdefghijklmnopqrstuvwxyz"}


async def _seed_paid_order(oid, tariff, amount, days_ago):
    async with _db() as db:
        await db.execute(
            "INSERT INTO orders (id, tariff, amount, status, paid_at) "
            "VALUES (?, ?, ?, 'paid', datetime('now', ?))",
            (oid, tariff, amount, f"-{days_ago} days"),
        )
        await db.commit()


def test_analytics_funnel(client):
    # 3 заказа создано, 2 оплачено, 2 выдано
    for i in range(3):
        uid = uuid.uuid4().hex[:8]
        oid = f"f-{i}-{uuid.uuid4().hex[:4]}"
        create = create_order(oid, "quantum_month", 300)
        asyncio_run(create)
        asyncio_run(set_order_user(oid, uid))
        asyncio_run(add_site_log("order_create", actor=uid, details=oid))
    asyncio_run(_seed_paid_order("f-paid-1", "quantum_month", 300, 0))
    asyncio_run(_seed_paid_order("f-paid-2", "quantum_year", 2900, 1))
    asyncio_run(add_site_log("fulfill", actor="u1", details="f-paid-1"))
    asyncio_run(add_site_log("fulfill", actor="u2", details="f-paid-2"))

    r = client.get("/admin/analytics/funnel?days=7", headers=ADMIN_HEADERS)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["totals"]["order_create"] == 3
    assert d["totals"]["order_paid"] == 2
    assert d["totals"]["fulfill"] == 2
    # 2/3 = 66.7%, 2/2 = 100%
    assert d["totals"]["conv_create_to_paid"] == 66.7
    assert d["totals"]["conv_paid_to_fulfill"] == 100.0
    assert d["rows"], "должны быть строки по дням"
    assert d["days"] == 7


def test_analytics_by_tariff(client):
    asyncio_run(_seed_paid_order("bt-1", "quantum_month", 300, 0))
    asyncio_run(_seed_paid_order("bt-2", "quantum_year", 2900, 0))
    asyncio_run(_seed_paid_order("bt-3", "quantum_year", 2900, 0))
    asyncio_run(_seed_paid_order("bt-4", "quantum_month", 300, 100))  # вне окна 30 дней

    r = client.get("/admin/analytics/by-tariff?days=30", headers=ADMIN_HEADERS)
    assert r.status_code == 200, r.text
    rows = r.json()
    by = {row["tariff"]: row for row in rows}
    assert by["quantum_year"]["cnt"] == 2
    assert by["quantum_year"]["revenue"] == 5800.0
    assert by["quantum_month"]["cnt"] == 1
    assert "quantum_month" in by and by["quantum_month"]["revenue"] == 300.0
    # заказ вне окна не учитывается
    assert by["quantum_month"]["cnt"] == 1


def test_analytics_anomalies_catches_renew_burst(client):
    """5 renew одного actor за 7 дней → аномалия (ловит баг Части 2)."""
    uid = uuid.uuid4().hex[:8]
    for i in range(5):
        asyncio_run(add_site_log("renew", actor=uid, details=f"r{i}"))
    asyncio_run(add_site_log("renew", actor="normal-user", details="r"))

    r = client.get("/admin/analytics/anomalies", headers=ADMIN_HEADERS)
    assert r.status_code == 200, r.text
    d = r.json()
    assert any(x["actor"] == uid and x["cnt"] == 5 for x in d["renew_heavy"])
    assert all(x["cnt"] <= 5 for x in d["renew_heavy"])
    assert "normal-user" not in [x["actor"] for x in d["renew_heavy"]]


def test_analytics_anomalies_addon_burst(client):
    """6 addon_purchase одного actor за день → аномалия."""
    uid = uuid.uuid4().hex[:8]
    for i in range(6):
        asyncio_run(add_site_log("addon_purchase", actor=uid, details=f"a{i}"))

    r = client.get("/admin/analytics/anomalies", headers=ADMIN_HEADERS)
    assert r.status_code == 200, r.text
    d = r.json()
    assert any(x["actor"] == uid and x["cnt"] == 6 for x in d["addon_burst"])


def test_analytics_requires_admin_key(client):
    assert client.get("/admin/analytics/funnel").status_code in (401, 503)
    assert client.get("/admin/analytics/by-tariff").status_code in (401, 503)


# ————————————————— helpers —————————————————

def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)
