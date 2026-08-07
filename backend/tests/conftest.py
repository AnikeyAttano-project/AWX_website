"""
Общие фикстуры для тестов.

КРИТИЧНО: env-переменные выставляются ДО импорта main — модуль config
читает Settings() на import-time, и менять их после бесполезно.

Что мокаем:
  - платёжного провайдера (FakeProvider: create_payment/check_status/verify/parse)
    в main.get_active_provider, main.get_provider и payment_lifecycle.get_provider —
    ТАМ, где эти функции реально используются (импортированы).
  - сетевые вызовы xui_client (renew_client/update_client_limit/create_client/...).

БД: отдельный временный файл на сессию pytest; между тестами таблицы чистятся.
"""
import asyncio
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path

# ——— env ДО импорта main (обязательно!) ———
_tmp = tempfile.mkdtemp(prefix="awx_tests_")
os.environ["DATABASE_PATH"] = os.path.join(_tmp, "test.db")
os.environ["JWT_SECRET"] = "test-secret-32-chars-minimum-xxxx"
os.environ["XUI_BASE_URL"] = "https://test.local"
os.environ["XUI_API_TOKEN"] = "test-token"
os.environ["XUI_SUB_BASE_URL"] = "https://test.local:2096/sub/"
os.environ["PLATEGA_MERCHANT_ID"] = "test-merchant"
os.environ["PLATEGA_SECRET"] = "test-secret"
os.environ["SITE_BASE_URL"] = "https://test.local"
os.environ["DEBUG_SANDBOX_ENABLED"] = "true"
os.environ["ADMIN_API_KEY"] = "test-admin-key-0123456789abcdefghijklmnopqrstuvwxyz"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient

import main
import shared_state
import payment_lifecycle
from database import _db, init_db


def run(coro):
    """Синхронная обёртка для async-функций БД (из синхронных тестов)."""
    return asyncio.run(coro)


class FakeProvider:
    """Мок платёжного провайдера. Управляй статусом: provider.status = ..."""
    name = "fake"
    status = "pending"
    create_calls = 0
    last_return_url = ""

    async def create_payment(self, amount, order_id, description, capability_token="", return_url=""):
        type(self).create_calls += 1
        type(self).last_return_url = return_url
        return {
            "transaction_id": f"tx-{order_id}",
            "payment_url": f"https://pay.local/{order_id}",
            "status": "pending",
        }

    async def check_status(self, tx):
        return self.status

    def verify_webhook(self, headers, body):
        return True

    def parse_webhook(self, body):
        # В обработчике body приходит как raw bytes — парсим так же, как реальный провайдер
        if isinstance(body, (bytes, bytearray)):
            body = body.decode("utf-8")
        data = json.loads(body) if isinstance(body, str) else (body or {})
        return {
            "transaction_id": data.get("id") or data.get("transaction_id") or "tx-unknown",
            "status": "succeeded",
            "order_id": data.get("payload") or data.get("order_id") or "",
        }


# ——— Мок всех сетевых вызовов (никогда не ходим в настоящий 3x-UI/Platega) ———

async def _fake_renew_client(email, add_days):
    assert email.endswith("@vpn.local"), f"renew_client: unmanaged email {email!r}"
    return {"new_expiry_ms": 1_000_000_000_000}


async def _fake_update_client_limit(email, new_limit_ip):
    assert email.endswith("@vpn.local"), f"update_client_limit: unmanaged email {email!r}"
    return {"ok": True}


async def _fake_create_client(email, duration_days, limit_ip, inbound_ids=None, **kw):
    return {"email": email, "sub_id": f"sub-{uuid.uuid4().hex[:8]}", "inbound_ids": inbound_ids or []}


async def _fake_get_subscription_url(sub_id):
    return f"https://sub.local/{sub_id}"


async def _fake_get_share_links(sub_id):
    return [f"vless://fake-{sub_id}"]


async def _fake_check_client_status(email):
    return {"isOnline": True, "expiryTime": 1_000_000_000_000, "up": 100, "down": 200}


@pytest.fixture(scope="session", autouse=True)
def _prepare_db():
    run(init_db())


@pytest.fixture(autouse=True)
def mocks(monkeypatch):
    """Подменяем всё, что ходит в сеть. Возвращает (FakeProvider, calls-cчётчики)."""
    provider = FakeProvider()
    provider.status = "pending"
    FakeProvider.create_calls = 0
    FakeProvider.last_return_url = ""
    calls = {"renew_client": 0, "update_client_limit": 0, "create_client": 0}

    async def _fake_renew_client(email, add_days):
        calls["renew_client"] += 1
        assert email.endswith("@vpn.local"), f"renew_client: unmanaged email {email!r}"
        return {"new_expiry_ms": 1_000_000_000_000}

    async def _fake_update_client_limit(email, new_limit_ip):
        calls["update_client_limit"] += 1
        assert email.endswith("@vpn.local"), f"update_client_limit: unmanaged email {email!r}"
        return {"ok": True}

    async def _fake_create_client(email, duration_days, limit_ip, inbound_ids=None, **kw):
        calls["create_client"] += 1
        return {"email": email, "sub_id": f"sub-{uuid.uuid4().hex[:8]}", "inbound_ids": inbound_ids or []}

    # После рефакторинга (Часть 4) все сетевые/провайдерские вызовы идут через
    # shared_state (роутеры вызывают shared_state.X) и payment_lifecycle.
    # Патчим ТАМ, где функции реально используются (не в исходном модуле!).
    monkeypatch.setattr(shared_state, "get_active_provider", lambda: provider)
    monkeypatch.setattr(shared_state, "get_provider", lambda name="": provider)
    monkeypatch.setattr(payment_lifecycle, "get_provider", lambda name="": provider)

    monkeypatch.setattr(shared_state, "renew_client", _fake_renew_client)
    monkeypatch.setattr(shared_state, "update_client_limit", _fake_update_client_limit)
    monkeypatch.setattr(shared_state, "create_client", _fake_create_client)
    monkeypatch.setattr(shared_state, "get_subscription_url", _fake_get_subscription_url)
    monkeypatch.setattr(shared_state, "get_sub_links", _fake_get_share_links)
    monkeypatch.setattr(shared_state, "check_client_status", _fake_check_client_status)
    monkeypatch.setattr(shared_state, "delete_client",
                        lambda email: {"ok": True})
    monkeypatch.setattr(shared_state, "rekey_client",
                        lambda email, inbound_ids=None: {"sub_id": "sub-rekeyed"})

    yield provider, calls


@pytest.fixture(autouse=True)
def _clean_db():
    """Чистим все таблицы между тестами (config/БД — одна на процесс)."""
    yield
    run(_truncate_all())


async def _truncate_all():
    async with _db() as db:
        for table in ("renewals", "device_addons", "orders", "users",
                      "referrals", "referral_settings", "settings",
                      "site_log", "promo_codes", "debug_audit_log"):
            try:
                await db.execute(f"DELETE FROM {table}")
            except Exception:
                pass
        await db.commit()


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        yield c
