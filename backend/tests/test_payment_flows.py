"""
Ключевые сценарии биллинга (Часть 1–2):

1. Add-on не активируется без подтверждения оплаты (bypass-тест).
2. Продление не активируется без оплаты.
3. Продление активируется с мок-провайдером + идемпотентность.
4. Webhook для продления работает и не роняет обработчик.
5. Управляемые email'ы: managed_email_guard.
6. Гонка: два параллельных webhook → ключ выдан ровно 1 раз.
"""
import asyncio
import uuid

import pytest
from xui_client import is_managed_panel_email, _require_managed_email, XuiError

from database import (
    create_order, set_order_user, save_subscription, save_platega_tx,
    create_device_addon, create_renewal, get_order, get_renewal_by_id,
    get_device_addons_for_order,
)


def run(coro):
    """Синхронная обёртка для async-функций БД (в синхронных тестах)."""
    return asyncio.run(coro)


async def _count_orders():
    from database import _db
    async with _db() as db:
        cur = await db.execute("SELECT COUNT(*) FROM orders")
        row = await cur.fetchone()
        return row[0]

EMAIL = "user@test.local"
PASSWORD = "secret12345"  # >= password_min_length (10)


# ————————————————— helpers —————————————————

def register(client):
    r = client.post("/api/auth/register", json={"email": EMAIL, "password": PASSWORD})
    assert r.status_code == 200, r.text
    data = r.json()
    return data["token"], data["user_id"]


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def make_user_and_sub(client, tariff="quantum_month"):
    """Регистрирует пользователя и создаёт ему активную подписку с xui_email."""
    token, uid = register(client)
    oid = "o" + uuid.uuid4().hex[:6]
    run(create_order(oid, tariff, 300))
    run(set_order_user(oid, uid))
    run(save_subscription(oid, "q-user@vpn.local", "sub-1", "https://sub.local/sub-1",
                          expires_at="2030-01-01 00:00:00"))
    return token, uid, oid


# ————————————————— Тесты —————————————————

def test_addon_not_activated_without_payment(client, mocks):
    """Add-on не активируется, пока платёж не подтверждён."""
    provider, calls = mocks
    provider.status = "pending"  # оплаты нет

    token, uid, oid = make_user_and_sub(client)
    addon_id = "a" + uuid.uuid4().hex[:6]
    run(create_device_addon(addon_id, uid, oid, "devices_5", 5, 100,
                            "2030-01-01 00:00:00", platega_tx_id=f"tx-{addon_id}"))

    r = client.get(f"/api/account/addon/{addon_id}/status", headers=auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending", r.json()
    assert calls["update_client_limit"] == 0

    addons = run(get_device_addons_for_order(oid))
    assert addons[0]["status"] == "pending"


def test_renewal_not_activated_without_payment(client, mocks):
    """Продление не выполняется, пока платёж не подтверждён."""
    provider, calls = mocks
    provider.status = "pending"

    token, uid, oid = make_user_and_sub(client)
    renewal_id = "r" + uuid.uuid4().hex[:6]
    run(create_renewal(renewal_id, oid, uid, 31, 300, f"tx-{renewal_id}"))

    r = client.get(f"/api/account/renewal/{renewal_id}/status", headers=auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending", r.json()
    assert calls["renew_client"] == 0


def test_renewal_activation_with_mock_provider_idempotent(client, mocks):
    """Оплата подтверждена → продление выполняется ровно 1 раз (идемпотентно)."""
    provider, calls = mocks
    provider.status = "pending"

    token, uid, oid = make_user_and_sub(client)
    renewal_id = "r" + uuid.uuid4().hex[:6]
    run(create_renewal(renewal_id, oid, uid, 31, 300, f"tx-{renewal_id}"))

    # Сначала — без оплаты (ничего не происходит)
    r = client.get(f"/api/account/renewal/{renewal_id}/status", headers=auth(token))
    assert r.json()["status"] == "pending"

    # Платёж прошёл
    provider.status = "succeeded"
    r = client.get(f"/api/account/renewal/{renewal_id}/status", headers=auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "active", r.json()
    assert calls["renew_client"] == 1

    # Повторный polling — не продлевает повторно
    r = client.get(f"/api/account/renewal/{renewal_id}/status", headers=auth(token))
    assert r.json()["status"] == "active"
    assert calls["renew_client"] == 1

    renew = run(get_renewal_by_id(renewal_id))
    assert renew["status"] == "active"


def test_webhook_renewal_ok(client, mocks):
    """Webhook для продления: 200, renew_client вызван, заявка active."""
    provider, calls = mocks
    provider.status = "succeeded"

    token, uid, oid = make_user_and_sub(client)
    renewal_id = "r" + uuid.uuid4().hex[:6]
    run(create_renewal(renewal_id, oid, uid, 31, 300, f"tx-{renewal_id}"))

    r = client.post(
        "/webhook/platega",
        json={"id": f"tx-{renewal_id}", "payload": renewal_id},
    )
    assert r.status_code == 200, r.text
    assert calls["renew_client"] == 1
    assert run(get_renewal_by_id(renewal_id))["status"] == "active"


def test_webhook_rejected_from_unlisted_ip(client, mocks, monkeypatch):
    """IP-allowlist: вебхук с IP вне списка отклоняется ещё до обработки."""
    from config import settings as s
    monkeypatch.setattr(s, "webhook_ip_allowlist", ["192.0.2.1"])

    r = client.post(
        "/webhook/platega",
        json={"id": "tx-fake", "payload": "order-1"},
    )
    assert r.status_code == 403, r.text


def test_webhook_accepted_when_allowlist_empty(client, mocks):
    """Пустой allowlist = проверка выключена (dev-режим), вебхук обрабатывается."""
    r = client.post(
        "/webhook/platega",
        json={"id": "tx-nonexistent", "payload": "order-1"},
    )
    # Провайдер-fake возвращает succeeded, но заказ не найден — не падаем, а отвечаем ok
    assert r.status_code == 200, r.text


def test_webhook_cancelled_payment_marks_order_cancelled(client, mocks):
    """№36: отменённый платёж по обычному заказу → статус 'cancelled', НЕ 'error'."""
    provider, _ = mocks
    provider.status = "cancelled"

    oid = "o" + uuid.uuid4().hex[:6]
    run(create_order(oid, "quantum_month", 300))
    run(save_platega_tx(oid, f"tx-{oid}"))

    r = client.post("/webhook/platega", json={"id": f"tx-{oid}", "payload": oid})
    assert r.status_code == 200, r.text
    order = run(get_order(oid))
    assert order["status"] == "cancelled", order
    assert order["status"] != "error"
    assert order["error_msg"] == "Payment cancelled"


def test_create_payment_failure_cleans_orphan_order(client, mocks):
    """№39: сбой create_payment → 502, заказ-сирота не остаётся в БД."""
    provider, _ = mocks
    provider.fail_create = True
    r = client.post("/api/order/create", json={"tariff": "quantum_month", "addon_type": ""})
    assert r.status_code == 502, r.text
    assert run(_count_orders()) == 0


def test_managed_email_guard():
    """managed_email_guard: отказываемся мутировать неменеджерские email."""
    assert is_managed_panel_email("user@vpn.local") is True
    assert is_managed_panel_email("USER@VPN.LOCAL") is True
    assert is_managed_panel_email("user@gmail.com") is False
    assert is_managed_panel_email(None) is False
    assert is_managed_panel_email("") is False

    assert _require_managed_email("q-user@vpn.local") == "q-user@vpn.local"
    # Регистр не важен — суффикс @vpn.local
    assert _require_managed_email("USER@VPN.LOCAL") == "USER@VPN.LOCAL"
    with pytest.raises(XuiError):
        _require_managed_email("user@gmail.com")
    with pytest.raises(XuiError):
        _require_managed_email("manual@example.com")


def test_race_fulfill_order_single_key(client, mocks):
    """Гонка: два параллельных fulfill (webhook/polling вызывают именно его)
    на один заказ → ключ выдан ровно 1 раз."""
    provider, calls = mocks
    provider.status = "succeeded"

    import main

    token, uid, oid = make_user_and_sub(client)
    # "Обнуляем" выдачу: убираем sub_url, возвращаем статус pending, добавляем tx_id
    run(_reset_order_for_fulfill(oid))

    async def _race():
        await asyncio.gather(
            main.order_lifecycle.fulfill(oid),
            main.order_lifecycle.fulfill(oid),
        )

    asyncio.run(_race())

    # Ключ создан ровно один раз (защита и lock'ом, и атомарным DB-claim)
    assert calls["create_client"] == 1, f"create_client called {calls['create_client']} times"
    order = run(get_order(oid))
    assert order["sub_url"], "sub_url должен быть проставлен"


async def _reset_order_for_fulfill(oid):
    """Возвращает заказ в состояние 'paid без ключа' для проверки re-claim."""
    from database import _db
    async with _db() as db:
        await db.execute(
            "UPDATE orders SET status='pending', sub_url=NULL, xui_sub_id=NULL, "
            "xui_email='q-user@vpn.local', platega_tx_id=?, "
            "fulfillment_status='pending', fulfillment_started_at=NULL WHERE id=?",
            (f"tx-{oid}", oid),
        )
        await db.commit()


def test_addon_payment_return_url_is_account(client, mocks):
    """№20: платёж за add-on ведёт возврат на /account.html (не /payment/success)."""
    from tests.conftest import FakeProvider
    from database import mark_paid

    provider, calls = mocks
    token, uid, oid = make_user_and_sub(client)
    run(mark_paid(oid))  # подписка оплачена → аддон можно купить

    r = client.post(f"/api/account/subscription/{oid}/addon",
                    json={"addon_type": "devices_5"}, headers=auth(token))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data.get("payment_url"), data
    # ЛК сам поллит статус по JWT — return_url должен указывать на ЛК.
    assert FakeProvider.last_return_url == "https://test.local/account.html", \
        FakeProvider.last_return_url


def test_renew_payment_return_url_is_account(client, mocks):
    """№20: платёж за продление ведёт возврат на /account.html."""
    from tests.conftest import FakeProvider

    provider, calls = mocks
    token, uid, oid = make_user_and_sub(client)

    r = client.post(f"/api/account/renew/{oid}", headers=auth(token))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data.get("payment_url"), data
    assert FakeProvider.last_return_url == "https://test.local/account.html", \
        FakeProvider.last_return_url


def test_order_payment_return_url_defaults_to_success(client, mocks):
    """№20: обычный заказ на витрине по-прежнему возвращает на /payment/success."""
    from tests.conftest import FakeProvider

    provider, calls = mocks
    r = client.post("/api/order/create",
                    json={"tariff": "quantum_month", "addon_type": ""})
    assert r.status_code == 200, r.text
    assert r.json().get("payment_url")
    assert FakeProvider.last_return_url == "", FakeProvider.last_return_url
