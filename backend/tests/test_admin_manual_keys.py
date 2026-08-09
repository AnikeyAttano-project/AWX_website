"""
Ит.3 Ручные ключи: выдача/перевыпуск/отмена подписок админом.

- POST /admin/users/{id}/key/issue — ручная выдача (без оплаты), провижининг
  в 3x-UI, заказ сразу paid, аудит admin_key_issue с именем админа (X-Admin-Name).
- POST /admin/users/{id}/key/reissue — новый sub_id/sub_url, срок не меняется.
- POST /admin/users/{id}/key/{order_id}/cancel — удаление клиента из 3x-UI
  + заказ в 'deleted'.
"""
import urllib.parse
import uuid

from database import _db, get_order, get_site_logs, create_user

# Фронт шлёт имя через encodeURIComponent (заголовки не несут кириллицу)
ADMIN_HEADERS = {
    "X-Admin-Key": "test-admin-key-0123456789abcdefghijklmnopqrstuvwxyz",
    "X-Admin-Name": urllib.parse.quote("Тест Админ"),
}


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


def _make_user():
    uid = uuid.uuid4().hex[:12]
    asyncio_run(create_user(uid, f"manual-{uid}@vpn.local", "hash", verified=1))
    return uid


def _log_actions():
    logs = asyncio_run(get_site_logs(100))
    return [l for l in logs if l["action"].startswith("admin_key")]


# ——— Выдача ———

def test_admin_issue_key_provisions(client):
    uid = _make_user()
    r = client.post(f"/admin/users/{uid}/key/issue", json={
        "tariff": "quantum_month", "days": 0,
    }, headers=ADMIN_HEADERS)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is True
    assert d["order_id"] and d["email"] and d["sub_url"]
    assert d["email"].endswith("@vpn.local")
    assert d["sub_url"].startswith("https://sub.local/")
    assert d["days"] == 31  # по тарифу quantum_month

    order = asyncio_run(get_order(d["order_id"]))
    assert order["status"] == "paid"
    assert order["user_id"] == uid
    assert order["xui_sub_id"], "ключ должен быть создан в 3x-UI"

    audit = _log_actions()
    assert any(e["action"] == "admin_key_issue" and e["actor"] == "Тест Админ" for e in audit)


def test_admin_issue_key_days_override(client):
    uid = _make_user()
    r = client.post(f"/admin/users/{uid}/key/issue", json={
        "tariff": "quantum_month", "days": 10,
    }, headers=ADMIN_HEADERS)
    assert r.status_code == 200, r.text
    assert r.json()["days"] == 10

    audit = _log_actions()
    entry = next(e for e in audit if e["action"] == "admin_key_issue")
    assert "days=10" in entry["details"], entry["details"]


def test_admin_issue_key_invalid_tariff(client):
    uid = _make_user()
    r = client.post(f"/admin/users/{uid}/key/issue", json={"tariff": "no_such"},
                    headers=ADMIN_HEADERS)
    assert r.status_code == 400, r.text


def test_admin_issue_key_blocked_user(client):
    uid = _make_user()
    asyncio_run(_db_block(uid, 1))
    r = client.post(f"/admin/users/{uid}/key/issue", json={"tariff": "quantum_month"},
                    headers=ADMIN_HEADERS)
    assert r.status_code == 400, r.text
    assert "блокирован" in r.json()["detail"]


def test_admin_issue_key_unknown_user(client):
    r = client.post("/admin/users/nonexistent/key/issue", json={"tariff": "quantum_month"},
                    headers=ADMIN_HEADERS)
    assert r.status_code == 404, r.text


# ——— Перевыпуск ———

def test_admin_reissue_key(client):
    uid = _make_user()
    issue = client.post(f"/admin/users/{uid}/key/issue", json={"tariff": "quantum_month"},
                        headers=ADMIN_HEADERS).json()
    order_id = issue["order_id"]

    r = client.post(f"/admin/users/{uid}/key/reissue", json={"order_id": order_id},
                    headers=ADMIN_HEADERS)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is True
    assert d["sub_url"].startswith("https://sub.local/")
    assert "rekey-" in d["email"]

    audit = _log_actions()
    assert any(e["action"] == "admin_key_reissue" and e["actor"] == "Тест Админ" for e in audit)


def test_admin_reissue_key_other_user(client):
    uid1 = _make_user()
    uid2 = _make_user()
    order_id = client.post(f"/admin/users/{uid1}/key/issue", json={"tariff": "quantum_month"},
                           headers=ADMIN_HEADERS).json()["order_id"]
    # uid2 пытается перевыпустить чужой ключ
    r = client.post(f"/admin/users/{uid2}/key/reissue", json={"order_id": order_id},
                    headers=ADMIN_HEADERS)
    assert r.status_code == 404, r.text


# ——— Отмена ———

def test_admin_cancel_key(client):
    uid = _make_user()
    order_id = client.post(f"/admin/users/{uid}/key/issue", json={"tariff": "quantum_month"},
                           headers=ADMIN_HEADERS).json()["order_id"]

    r = client.post(f"/admin/users/{uid}/key/{order_id}/cancel", headers=ADMIN_HEADERS)
    assert r.status_code == 200, r.text
    order = asyncio_run(get_order(order_id))
    assert order["status"] == "deleted"

    # Повторная отмена — 400
    r = client.post(f"/admin/users/{uid}/key/{order_id}/cancel", headers=ADMIN_HEADERS)
    assert r.status_code == 400, r.text

    audit = _log_actions()
    assert any(e["action"] == "admin_key_cancel" and e["actor"] == "Тест Админ" for e in audit)


# ——— helpers ———

async def _db_block(uid, blocked):
    async with _db() as db:
        await db.execute("UPDATE users SET blocked = ? WHERE id = ?", (blocked, uid))
        await db.commit()
