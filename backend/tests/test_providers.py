"""Тесты фабрики платёжных провайдеров (№35: без угадывания)."""
import pytest

from config import settings
from payment_providers import (
    PaymentError, get_provider, get_active_provider,
    PlategaProvider, YooKassaProvider,
)


def test_get_provider_known_names():
    assert isinstance(get_provider("platega"), PlategaProvider)
    assert isinstance(get_provider("yookassa"), YooKassaProvider)


def test_get_provider_normalizes_case_and_space():
    assert isinstance(get_provider(" YooKassa "), YooKassaProvider)
    assert isinstance(get_provider("PLATEGA"), PlategaProvider)


def test_get_provider_unknown_name_raises():
    """Опечатка/мусор в имени — PaymentError, а не молчаливый Platega (№35)."""
    with pytest.raises(PaymentError):
        get_provider("yookasa")
    with pytest.raises(PaymentError):
        get_provider("magic-provider")


def test_active_provider_unknown_env_raises(monkeypatch):
    monkeypatch.setattr(settings, "payment_provider", "weird")
    with pytest.raises(PaymentError):
        get_active_provider()


def test_active_provider_empty_defaults_to_platega(monkeypatch):
    monkeypatch.setattr(settings, "payment_provider", "")
    assert isinstance(get_active_provider(), PlategaProvider)
