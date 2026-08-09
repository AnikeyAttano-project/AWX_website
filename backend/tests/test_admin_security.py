"""
Ит.2 Backend-безопасность и аудит.

1. Локаут на подбор X-Admin-Key: после N неверных попыток с одного IP даже
   верный ключ блокируется (429), другой IP не задет.
2. Все смены настроек пишутся в site_log (settings_*).
3. Валидация /admin/settings/import: триал-диапазоны, промо с явными ошибками.
4. /admin/logs — фильтры action/actor + пагинация.
"""
import uuid

from config import settings
from database import _db, add_site_log, get_site_logs, get_site_logs_filtered
import admin as admin_module

ADMIN_HEADERS = {"X-Admin-Key": "test-admin-key-0123456789abcdefghijklmnopqrstuvwxyz"}


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


# ——— 1. Локаут на подбор X-Admin-Key ———

def test_admin_lockout_blocks_bruteforce(client, monkeypatch):
    """N неверных попыток с одного IP → даже верный ключ даёт 429."""
    ip = ["1.2.3.4"]
    monkeypatch.setattr(admin_module, "get_real_ip", lambda request: ip[0])

    wrong = {"X-Admin-Key": "not-the-key"}
    for _ in range(settings.admin_lockout_after):
        r = client.get("/admin/dashboard", headers=wrong)
        assert r.status_code == 401, r.text

    # Верный ключ с того же IP теперь блокируется
    r = client.get("/admin/dashboard", headers=ADMIN_HEADERS)
    assert r.status_code == 429, r.text

    # Другой IP не задет локаутом
    ip[0] = "5.6.7.8"
    r = client.get("/admin/dashboard", headers=ADMIN_HEADERS)
    assert r.status_code == 200, r.text


def test_admin_lockout_resets_on_success(client, monkeypatch):
    """Успешная авторизация снимает локаут: неудача → успех → снова 401, не 429."""
    ip = ["9.9.9.9"]
    monkeypatch.setattr(admin_module, "get_real_ip", lambda request: ip[0])

    assert client.get("/admin/dashboard", headers={"X-Admin-Key": "bad"}).status_code == 401
    # Успешная авторизация сбрасывает счётчик
    assert client.get("/admin/dashboard", headers=ADMIN_HEADERS).status_code == 200
    # Снова неверный ключ — 401, а не 429 (локаут снят)
    assert client.get("/admin/dashboard", headers={"X-Admin-Key": "bad"}).status_code == 401


def test_admin_lockout_applies_to_debug_router(client, monkeypatch):
    """Тот же require_admin используется дебаг-роутером → локаут действует и там."""
    ip = ["8.8.8.8"]
    monkeypatch.setattr(admin_module, "get_real_ip", lambda request: ip[0])
    wrong = {"X-Admin-Key": "bad-key"}
    for _ in range(settings.admin_lockout_after):
        r = client.get("/admin/debug/timeline/whatever", headers=wrong)
        assert r.status_code in (401, 404), r.text  # 404 = флаг sandbox, 401 = неверный ключ
    r = client.get("/admin/debug/timeline/whatever", headers=ADMIN_HEADERS)
    assert r.status_code == 429, r.text


# ——— 2. Аудит смен настроек ———

def test_settings_changes_audited_to_site_log(client):
    r = client.post("/admin/settings/trial", json={
        "enabled": True, "days": 7, "gb": 10, "devices": 2,
    }, headers=ADMIN_HEADERS)
    assert r.status_code == 200, r.text

    logs = asyncio_run(get_site_logs(50))
    entry = next((l for l in logs if l["action"] == "settings_trial"), None)
    assert entry, "смена триала должна попасть в site_log"
    assert "days=7" in entry["details"]
    assert entry["actor"] == "admin"


def test_all_settings_endpoints_audited(client):
    """Каждый settings-эндпоинт пишет свой action в site_log."""
    calls = [
        ("/admin/settings/trial", "settings_trial",
         {"enabled": False, "days": 3, "gb": 5, "devices": 1}),
        ("/admin/settings/branding", "settings_branding",
         {"site_name": "Test", "accent_color": "#123456", "support_contact": "a@b.c",
          "logo_data_url": "", "hero_title": "", "hero_subtitle": "",
          "site_description": "", "footer_text": ""}),
        ("/admin/settings/demo?enabled=false", "settings_demo", None),
        ("/admin/settings/inbounds", "settings_inbounds", {"inbounds": [5]}),
    ]
    for path, action, body in calls:
        r = client.post(path, json=body, headers=ADMIN_HEADERS)
        assert r.status_code == 200, f"{path}: {r.text}"

    logs = asyncio_run(get_site_logs(50))
    actions = {l["action"] for l in logs}
    for _, action, _ in calls:
        assert action in actions, f"нет аудита для {action}"


# ——— 3. Валидация import ———

def test_import_trial_validates_ranges(client):
    r = client.post("/admin/settings/import", json={
        "trial": {"enabled": True, "days": 500, "gb": 25, "devices": 1},
    }, headers=ADMIN_HEADERS)
    assert r.status_code == 400, r.text
    assert "trial.days" in r.json()["detail"]


def test_import_trial_validates_devices(client):
    r = client.post("/admin/settings/import", json={
        "trial": {"enabled": True, "days": 3, "gb": 25, "devices": 99},
    }, headers=ADMIN_HEADERS)
    assert r.status_code == 400, r.text
    assert "trial.devices" in r.json()["detail"]


def test_import_promo_invalid_is_explicit_and_atomic(client):
    """Битая пачка промо → 400 с перечнем, валидные из той же пачки не создаются."""
    r = client.post("/admin/settings/import", json={
        "promo_codes": [
            {"code": "GOOD100", "kind": "percent", "value": 10},
            {"code": "BAD", "kind": "flat", "value": 5},      # неверный kind
            {"code": "OVER100", "kind": "percent", "value": 150},  # >100%
        ],
    }, headers=ADMIN_HEADERS)
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert "BAD" in detail and "OVER100" in detail, detail

    # GOOD100 не должен был создаться (атомарность пачки)
    r = client.get("/admin/promo", headers=ADMIN_HEADERS)
    assert r.status_code == 200
    codes = [p["code"] for p in r.json()["items"]]
    assert "GOOD100" not in codes


def test_import_promo_valid_batch(client):
    r = client.post("/admin/settings/import", json={
        "promo_codes": [
            {"code": "NEWYEAR", "kind": "percent", "value": 20},
            {"code": "FIXED10", "kind": "fixed", "value": 10},
        ],
    }, headers=ADMIN_HEADERS)
    assert r.status_code == 200, r.text
    r = client.get("/admin/promo", headers=ADMIN_HEADERS)
    codes = [p["code"] for p in r.json()["items"]]
    assert "NEWYEAR" in codes and "FIXED10" in codes


# ——— 4. Логи: фильтры и пагинация ———

def _seed_logs(n: int, actions=("settings_trial", "settings_branding", "promo_create")):
    async def _seed():
        async with _db() as db:
            for i in range(n):
                a = actions[i % len(actions)]
                await db.execute(
                    "INSERT INTO site_log (level, action, actor, details) "
                    "VALUES ('info', ?, ?, ?)",
                    (a, f"actor-{i % 3}", f"row-{i}"),
                )
            await db.commit()
    asyncio_run(_seed())


def test_admin_logs_filter_action(client):
    _seed_logs(9, actions=("settings_trial", "settings_branding"))
    r = client.get("/admin/logs?action=settings_trial", headers=ADMIN_HEADERS)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["total"] == 5
    assert all(item["action"] == "settings_trial" for item in d["items"])
    assert d["page"] == 1 and d["page_size"] == 50


def test_admin_logs_filter_actor(client):
    _seed_logs(6)
    r = client.get("/admin/logs?actor=actor-0", headers=ADMIN_HEADERS)
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 2
    assert all(item["actor"] == "actor-0" for item in r.json()["items"])


def test_admin_logs_pagination(client):
    _seed_logs(12)
    r = client.get("/admin/logs?page=1&page_size=10", headers=ADMIN_HEADERS)
    d = r.json()
    assert d["total"] == 12
    assert len(d["items"]) == 10
    first_page_first = d["items"][0]["id"]

    r = client.get("/admin/logs?page=2&page_size=10", headers=ADMIN_HEADERS)
    d2 = r.json()
    assert len(d2["items"]) == 2
    assert all(item["id"] < first_page_first for item in d2["items"]), "новые записи вверху"


def test_get_site_logs_filtered_pure_function():
    """Фильтрованная функция БД возвращает (items, total) без HTTP."""
    async def _seed():
        async with _db() as db:
            for i in range(7):
                await db.execute(
                    "INSERT INTO site_log (level, action, actor, details) "
                    "VALUES ('info', 'x_action', 'x_actor', ?)", (f"r{i}",))
            await db.commit()
    asyncio_run(_seed())
    items, total = asyncio_run(get_site_logs_filtered(
        page=1, page_size=4, action="x_action", actor="x_actor"))
    assert total == 7
    assert len(items) == 4
    assert all(i["action"] == "x_action" for i in items)
