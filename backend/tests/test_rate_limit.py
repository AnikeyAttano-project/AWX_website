"""Тесты rate-limiting: лимиты, разделение скоупов, TTL-джанетор (№34)."""
from datetime import datetime, timedelta

import shared_state
from shared_state import check_rate_limit, _prune_rate_limit_keys, rate_limit_storage


def test_rate_limit_blocks_after_max():
    assert all(check_rate_limit("x", max_requests=3, window_minutes=60) for _ in range(3))
    assert not check_rate_limit("x", max_requests=3, window_minutes=60)


def test_scopes_are_independent():
    """Запросы к разным скоупам одного IP не считаются вместе (№34)."""
    for _ in range(10):
        assert check_rate_limit(f"order:1.2.3.4", max_requests=10, window_minutes=60)
    # Лимит order исчерпан, но auth-скоуп того же IP не тронут.
    assert check_rate_limit("auth:1.2.3.4", max_requests=20, window_minutes=60)
    assert not check_rate_limit(f"order:1.2.3.4", max_requests=10, window_minutes=60)


def test_check_prunes_expired_entries_by_own_window():
    """Просроченная запись не считается (подрезается по своему окну)."""
    old = (datetime.now() - timedelta(minutes=90), 60)
    fresh = (datetime.now(), 60)
    rate_limit_storage["k"] = [old, fresh]
    assert check_rate_limit("k", max_requests=2, window_minutes=60)  # fresh + новый = 2
    # После аппенда записей стало 2 (старая отброшена).
    assert len(rate_limit_storage["k"]) == 2


def test_janitor_removes_all_expired_keys():
    """TTL-джанетор удаляет ключи, у которых ВСЕ записи истекли (№34)."""
    now = datetime.now()
    rate_limit_storage["expired-1min"] = [(now - timedelta(minutes=5), 1)]
    rate_limit_storage["expired-1440"] = [(now - timedelta(days=2), 1440)]
    rate_limit_storage["active"] = [(now, 60)]
    rate_limit_storage["partly-active"] = [
        (now - timedelta(minutes=90), 60),
        (now, 60),
    ]
    _prune_rate_limit_keys()
    assert "expired-1min" not in rate_limit_storage
    assert "expired-1440" not in rate_limit_storage
    assert "active" in rate_limit_storage
    assert "partly-active" in rate_limit_storage


def test_janitor_removes_empty_keys():
    rate_limit_storage["empty"] = []
    _prune_rate_limit_keys()
    assert "empty" not in rate_limit_storage


def test_invalid_register_does_not_consume_limit(client):
    """№38: кривые регистрации — 400 и не сжигают auth-лимит; валидная проходит."""
    from config import settings
    for _ in range(settings.auth_rate_limit_per_hour + 5):
        r = client.post("/api/auth/register", json={"email": "bad", "password": "x"})
        assert r.status_code == 400, r.text
    r = client.post("/api/auth/register",
                    json={"email": "ok@vpn.local", "password": "strongpass123"})
    assert r.status_code == 200, r.text


def test_invalid_tariff_does_not_consume_order_limit(client):
    """№38: неизвестный тариф — 400 и не сжигает order-лимит (10/час)."""
    for _ in range(15):
        r = client.post("/api/order/create", json={"tariff": "nope", "addon_type": ""})
        assert r.status_code == 400, r.text
    r = client.post("/api/order/create",
                    json={"tariff": "quantum_month", "addon_type": ""})
    assert r.status_code == 200, r.text
