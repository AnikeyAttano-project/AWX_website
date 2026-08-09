"""
Ит.7 «Дебаг-песочница»: пометка «тестовый аккаунт» из админки.

- POST /admin/users/{id}/test-account — установить/снять флаг is_test_account,
  аудит admin_test_account в site_log с именем админа (X-Admin-Name).
- Интеграция: тестовый аккаунт пропускает confirm_email в дебаг-песочнице
  (require_test_account_or_confirmation) — это и есть смысл пометки.
"""
import urllib.parse
import uuid

from database import _db, create_user, get_site_logs
from admin_debug import require_test_account_or_confirmation

ADMIN_HEADERS = {
    "X-Admin-Key": "test-admin-key-0123456789abcdefghijklmnopqrstuvwxyz",
    "X-Admin-Name": urllib.parse.quote("Тест Админ"),
}


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


def _make_user():
    uid = uuid.uuid4().hex[:12]
    asyncio_run(create_user(uid, f"testacc-{uid}@vpn.local", "hash", verified=1))
    return uid


async def _get_flag(user_id):
    async with _db() as db:
        cur = await db.execute("SELECT is_test_account FROM users WHERE id = ?", (user_id,))
        row = await cur.fetchone()
        return row[0] if row else None


# ——— Пометка / снятие ———

def test_admin_mark_test_account(client):
    uid = _make_user()
    r = client.post(f"/admin/users/{uid}/test-account", json={"is_test": True},
                    headers=ADMIN_HEADERS)
    assert r.status_code == 200, r.text
    assert r.json()["is_test_account"] is True
    assert asyncio_run(_get_flag(uid)) == 1

    # Флаг виден в профиле пользователя
    d = client.get(f"/admin/users/{uid}", headers=ADMIN_HEADERS).json()
    assert d["is_test_account"] == 1

    audit = asyncio_run(get_site_logs(100))
    assert any(l["action"] == "admin_test_account" and l["actor"] == "Тест Админ"
               and f"is_test=1" in l["details"] for l in audit), "аудит пометки отсутствует"


def test_admin_unmark_test_account(client):
    uid = _make_user()
    client.post(f"/admin/users/{uid}/test-account", json={"is_test": True}, headers=ADMIN_HEADERS)
    r = client.post(f"/admin/users/{uid}/test-account", json={"is_test": False},
                    headers=ADMIN_HEADERS)
    assert r.status_code == 200, r.text
    assert r.json()["is_test_account"] is False
    assert asyncio_run(_get_flag(uid)) == 0


def test_admin_test_account_missing_user(client):
    r = client.post("/admin/users/nope/test-account", json={"is_test": True},
                    headers=ADMIN_HEADERS)
    assert r.status_code == 404, r.text


# ——— Интеграция с дебаг-песочницей ———

def test_test_account_skips_confirm_email(client):
    """Тестовый аккаунт проходит require_test_account_or_confirmation без confirm_email."""
    uid = _make_user()
    client.post(f"/admin/users/{uid}/test-account", json={"is_test": True}, headers=ADMIN_HEADERS)

    user = asyncio_run(require_test_account_or_confirmation(uid, confirm_email=""))
    assert user["id"] == uid


def test_real_account_needs_confirm_email(client):
    """Обычный аккаунт без confirm_email — 400 (пометки нет)."""
    uid = _make_user()
    try:
        asyncio_run(require_test_account_or_confirmation(uid, confirm_email=""))
        assert False, "должно быть исключение для реального аккаунта"
    except Exception as e:
        assert getattr(e, "status_code", None) == 400
