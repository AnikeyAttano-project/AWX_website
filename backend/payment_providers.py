"""Абстракция платёжных провайдеров.

Интерфейс PaymentProvider:
  - create_payment(amount, order_id, description, capability_token) -> dict
      {"transaction_id", "payment_url", "status"}
  - check_status(transaction_id) -> "pending" | "succeeded" | "cancelled"
  - verify_webhook(headers, body) -> bool  (подлинность вебхука)
  - parse_webhook(body) -> {"transaction_id", "status", "order_id"}

Реализации: PlategaProvider (существующий клиент), YooKassaProvider.
Активный провайдер выбирается настройкой settings.payment_provider
('platega' | 'yookassa'), редактируется в админке.
"""
import base64
import json
import uuid

import httpx

from config import settings
from platega_client import (
    create_payment as _platega_create,
    check_status as _platega_check,
    PlategaError,
)


class PaymentError(Exception):
    pass


class PaymentProvider:
    name = "abstract"

    async def create_payment(self, amount, order_id, description, capability_token=""):
        raise NotImplementedError

    async def check_status(self, transaction_id):
        raise NotImplementedError

    def verify_webhook(self, headers, body):
        raise NotImplementedError

    def parse_webhook(self, body):
        raise NotImplementedError


# ————————————————— Platega —————————————————

class PlategaProvider(PaymentProvider):
    name = "platega"

    async def create_payment(self, amount, order_id, description, capability_token=""):
        try:
            return await _platega_create(amount, order_id, description, capability_token)
        except PlategaError as e:
            raise PaymentError(str(e)) from e

    async def check_status(self, transaction_id):
        try:
            return await _platega_check(transaction_id)
        except PlategaError as e:
            raise PaymentError(str(e)) from e

    def verify_webhook(self, headers, body):
        """Проверка через заголовки X-MerchantId + X-Secret."""
        if not settings.platega_secret:
            return False
        merchant_id = headers.get("x-merchantid", "")
        secret = headers.get("x-secret", "")
        return (merchant_id == settings.platega_merchant_id
                and secret == settings.platega_secret)

    def parse_webhook(self, body):
        try:
            data = json.loads(body)
        except Exception:
            data = {}
        tx_id = data.get("id") or data.get("transactionId")
        status = str(data.get("status", "")).upper()
        if status in ("CONFIRMED", "PAID", "COMPLETED"):
            normalized = "succeeded"
        elif status in ("CANCELED", "CANCELLED", "CHARGEBACKED", "REFUNDED", "EXPIRED"):
            normalized = "cancelled"
        else:
            normalized = "pending"
        return {
            "transaction_id": tx_id,
            "status": normalized,
            "order_id": data.get("payload") or "",
        }


# ————————————————— YooKassa —————————————————

class YooKassaProvider(PaymentProvider):
    name = "yookassa"
    API_URL = "https://api.yookassa.ru/v3/payments"

    def _auth_headers(self):
        if not settings.yookassa_shop_id or not settings.yookassa_secret_key:
            raise PaymentError("ЮKassa не настроена: нужны shop_id и secret_key")
        creds = base64.b64encode(
            f"{settings.yookassa_shop_id}:{settings.yookassa_secret_key}".encode()
        ).decode()
        return {
            "Authorization": f"Basic {creds}",
            "Idempotence-Key": uuid.uuid4().hex,
            "Content-Type": "application/json",
        }

    async def create_payment(self, amount, order_id, description, capability_token=""):
        token_param = f"&token={capability_token}" if capability_token else ""
        payload = {
            "amount": {"value": f"{float(amount):.2f}", "currency": "RUB"},
            "capture": True,
            "confirmation": {
                "type": "redirect",
                "return_url": (
                    f"{settings.site_base_url}/payment/success"
                    f"?order_id={order_id}{token_param}"
                ),
            },
            "description": description[:255],
            "metadata": {"order_id": order_id},
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(self.API_URL, json=payload, headers=self._auth_headers())

        if resp.status_code not in (200, 201):
            raise PaymentError(f"YooKassa create failed: {resp.status_code} {resp.text}")

        data = resp.json()
        tx_id = data.get("id")
        confirmation = data.get("confirmation") or {}
        payment_url = confirmation.get("confirmation_url") or ""
        if not tx_id or not payment_url:
            raise PaymentError(f"YooKassa unexpected response: {data}")
        return {
            "transaction_id": str(tx_id),
            "payment_url": payment_url,
            "status": str(data.get("status", "pending")),
        }

    async def check_status(self, transaction_id):
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{self.API_URL}/{transaction_id}", headers=self._auth_headers()
            )
        if resp.status_code != 200:
            raise PaymentError(f"YooKassa status check failed: {resp.status_code} {resp.text}")
        status = str(resp.json().get("status", "")).lower()
        if status == "succeeded":
            return "succeeded"
        if status == "canceled":
            return "cancelled"
        return "pending"

    def verify_webhook(self, headers, body):
        """У ЮKassa нет подписи вебхука. Принимаем, но статус перепроверяем
        через API в обработчике (check_status) — поддельный вебхук не пройдёт."""
        if not settings.yookassa_shop_id or not settings.yookassa_secret_key:
            return False
        try:
            data = json.loads(body)
        except Exception:
            return False
        return bool(data.get("object") and data.get("event"))

    def parse_webhook(self, body):
        try:
            data = json.loads(body)
        except Exception:
            data = {}
        obj = data.get("object") or {}
        event = data.get("event") or ""
        status = str(obj.get("status", "")).lower()
        if status == "succeeded" or event == "payment.succeeded":
            normalized = "succeeded"
        elif status == "canceled":
            normalized = "cancelled"
        else:
            normalized = "pending"
        meta = obj.get("metadata") or {}
        return {
            "transaction_id": obj.get("id"),
            "status": normalized,
            "order_id": meta.get("order_id") or "",
        }


# ————————————————— Фабрика —————————————————

def get_provider(name: str = "") -> PaymentProvider:
    """Возвращает провайдера по имени (или активного из settings)."""
    provider_name = (name or settings.payment_provider or "platega").lower()
    if provider_name == "yookassa":
        return YooKassaProvider()
    return PlategaProvider()


def get_active_provider() -> PaymentProvider:
    return get_provider()
