"""
Админ-список пользователей: orders_count (колонка «Подписки») и безопасность.

Ит.1: фронт таблицы «Пользователи» читал u.orders_count, а list_users_page
делал SELECT * без этого поля → колонка всегда показывала «—».
Проверяем, что orders_count теперь считается (без удалённых заказов),
и что password_hash не утекает в ответ.
"""
import uuid

from database import _db, create_user

ADMIN_HEADERS = {"X-Admin-Key": "test-admin-key-0123456789abcdefghijklmnopqrstuvwxyz"}


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


def test_admin_users_orders_count(client):
    uid = uuid.uuid4().hex[:12]
    asyncio_run(create_user(uid, f"orders-{uid}@vpn.local", "hash", verified=1))

    async def _seed():
        async with _db() as db:
            # 2 не-удалённых заказа (подписки) + 1 удалённый — удалённый не считается
            for i, status in enumerate(("paid", "pending", "deleted")):
                await db.execute(
                    "INSERT INTO orders (id, tariff, amount, status, user_id) "
                    "VALUES (?, 'quantum_month', 300, ?, ?)",
                    (f"o-{uid}-{i}", status, uid),
                )
            await db.commit()
    asyncio_run(_seed())

    r = client.get("/admin/users?page_size=50", headers=ADMIN_HEADERS)
    assert r.status_code == 200, r.text
    d = r.json()
    me = next(u for u in d["items"] if u["id"] == uid)
    assert me["orders_count"] == 2, "deleted-заказ не должен считаться в подписках"
    assert "password_hash" not in me, "password_hash не должен утекать в админ-список"


def test_admin_users_orders_count_search(client):
    uid = uuid.uuid4().hex[:12]
    email = f"search-{uid}@vpn.local"
    asyncio_run(create_user(uid, email, "hash", verified=1))

    r = client.get(f"/admin/users?q={email}", headers=ADMIN_HEADERS)
    assert r.status_code == 200, r.text
    me = r.json()["items"][0]
    assert me["id"] == uid
    assert me["orders_count"] == 0  # заказов ещё нет
