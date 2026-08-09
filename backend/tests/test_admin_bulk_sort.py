"""
Ит.4 «Таблицы и действия»: сортировки и массовые действия в админке.

- GET /admin/users?sort_by=&sort_dir= — сортировка пользователей.
- POST /admin/users/bulk-block, /admin/users/bulk-unblock — блок пачкой.
- GET /admin/keys?sort_by=&sort_dir= — сортировка ключей (id/tariff/email/expires_at).
- POST /admin/keys/bulk-delete — удаление пачкой (клиент из 3x-UI + заказ в deleted),
  аудит admin_keys_bulk_delete.

Бэкенд уже имел эти эндпоинты (Ит.4 делался на фронте) — тесты фиксируют контракт.
"""
import uuid

from database import _db, create_user, get_order, get_site_logs

ADMIN_HEADERS = {"X-Admin-Key": "test-admin-key-0123456789abcdefghijklmnopqrstuvwxyz"}


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


def _make_user(email_suffix=""):
    uid = uuid.uuid4().hex[:12]
    email = f"{email_suffix or 'bulk'}-{uid}@vpn.local"
    asyncio_run(create_user(uid, email, "hash", verified=1))
    return uid, email


async def _seed_order(order_id, tariff, amount, status, user_id, xui_email=None,
                      sub_url=None, expires_at=None):
    async with _db() as db:
        await db.execute(
            "INSERT INTO orders (id, tariff, amount, status, user_id, xui_email, sub_url, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (order_id, tariff, amount, status, user_id, xui_email, sub_url, expires_at),
        )
        await db.commit()


# ——— Сортировка пользователей ———

def test_admin_users_sort_email(client):
    u1, e1 = _make_user("aa")
    u2, e2 = _make_user("bb")
    asyncio_run(create_user(f"{u2}x", f"cc-{uuid.uuid4().hex[:8]}@vpn.local", "hash", verified=1))

    r = client.get("/admin/users?sort_by=email&sort_dir=asc&page_size=50", headers=ADMIN_HEADERS)
    assert r.status_code == 200, r.text
    emails = [u["email"] for u in r.json()["items"]]
    assert emails == sorted(emails), "sort_by=email&asc должен дать восходящий порядок"

    # Грубая, но надёжная проверка направления: aa-… идёт раньше bb-…
    idx1, idx2 = emails.index(e1), emails.index(e2)
    assert idx1 < idx2


def test_admin_users_sort_desc_is_reverse(client):
    r = client.get("/admin/users?sort_by=email&sort_dir=desc&page_size=50", headers=ADMIN_HEADERS)
    assert r.status_code == 200, r.text
    emails = [u["email"] for u in r.json()["items"]]
    assert emails == sorted(emails, reverse=True)


# ——— Массовая блокировка/разблокировка ———

def test_admin_users_bulk_block_and_unblock(client):
    ids = [_make_user()[0] for _ in range(3)]

    r = client.post("/admin/users/bulk-block", json={"ids": ids}, headers=ADMIN_HEADERS)
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 3

    d = client.get("/admin/users?page_size=100", headers=ADMIN_HEADERS).json()
    for u in d["items"]:
        if u["id"] in ids:
            assert u["blocked"] == 1, f"пользователь {u['id']} должен быть заблокирован"

    audit = asyncio_run(get_site_logs(100))
    assert any(l["action"] == "admin_users_bulk_block" and "count=3" in l["details"]
               for l in audit), "аудит bulk-block отсутствует"

    r = client.post("/admin/users/bulk-unblock", json={"ids": ids}, headers=ADMIN_HEADERS)
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 3

    d = client.get("/admin/users?page_size=100", headers=ADMIN_HEADERS).json()
    for u in d["items"]:
        if u["id"] in ids:
            assert u["blocked"] == 0, f"пользователь {u['id']} должен быть разблокирован"


# ——— Сортировка ключей ———

def test_admin_keys_sort_by_expires_at(client):
    u1, _ = _make_user()
    u2, _ = _make_user()
    asyncio_run(_seed_order("k-early", "quantum_month", 300, "paid", u1,
                            xui_email=f"early-{u1}@vpn.local",
                            sub_url=f"https://sub.local/{u1}-early",
                            expires_at="2025-01-01 00:00:00"))
    asyncio_run(_seed_order("k-late", "quantum_year", 1200, "paid", u2,
                            xui_email=f"late-{u2}@vpn.local",
                            sub_url=f"https://sub.local/{u2}-late",
                            expires_at="2026-06-01 00:00:00"))

    r = client.get("/admin/keys?sort_by=expires_at&sort_dir=asc&page_size=50", headers=ADMIN_HEADERS)
    assert r.status_code == 200, r.text
    ids = [k["id"] for k in r.json()["items"] if k["id"] in ("k-early", "k-late")]
    assert ids == ["k-early", "k-late"], "по expires_at asc ранний должен идти первым"

    r = client.get("/admin/keys?sort_by=expires_at&sort_dir=desc&page_size=50", headers=ADMIN_HEADERS)
    ids = [k["id"] for k in r.json()["items"] if k["id"] in ("k-early", "k-late")]
    assert ids == ["k-late", "k-early"]


def test_admin_keys_sort_invalid_field_rejected(client):
    r = client.get("/admin/keys?sort_by=DROP", headers=ADMIN_HEADERS)
    assert r.status_code in (400, 422), r.text


# ——— Массовое удаление ключей ———

def test_admin_keys_bulk_delete(client):
    uid, _ = _make_user()
    order_ids = []
    for i in range(3):
        oid = f"bd-{uuid.uuid4().hex[:8]}-{i}"
        order_ids.append(oid)
        asyncio_run(_seed_order(oid, "quantum_month", 300, "paid", uid,
                                xui_email=f"bd-{uid}-{i}@vpn.local",
                                sub_url=f"https://sub.local/{uid}-{i}"))

    r = client.post("/admin/keys/bulk-delete", json={"ids": order_ids}, headers=ADMIN_HEADERS)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["deleted"] == 3
    assert d["failed"] == []

    # Все заказы переведены в deleted
    for oid in order_ids:
        order = asyncio_run(get_order(oid))
        assert order and order["status"] == "deleted", f"{oid} должен быть deleted"

    audit = asyncio_run(get_site_logs(100))
    assert any(l["action"] == "admin_keys_bulk_delete" and "deleted=3" in l["details"]
               for l in audit), "аудит bulk-delete отсутствует"
