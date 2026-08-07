import httpx
from config import settings


class PlategaError(Exception):
    pass


def _headers() -> dict:
    return {
        "X-MerchantId": settings.platega_merchant_id,
        "X-Secret": settings.platega_secret,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


async def create_payment(
    amount: float,
    order_id: str,
    description: str,
    capability_token: str = "",
    return_url: str = "",
) -> dict:
    """
    Создаёт платёжную ссылку.

    return_url (необязательно): куда редиректит после оплаты. По умолчанию —
    /payment/success?order_id=... (витрина опрашивает статус по capability-токену).
    Для аддонов/продлений передаётся /account.html — ЛК сам поллит статус
    (pollPendingAddon/pollPendingRenewal), т.к. /payment/success не умеет
    опрашивать /api/account/* (там нужен JWT, а не capability-токен).

    Returns: {"transaction_id": str, "payment_url": str, "status": str}
    """
    token_param = f"&token={capability_token}" if capability_token else ""
    return_to = return_url or (
        f"{settings.site_base_url}/payment/success?order_id={order_id}{token_param}"
    )
    payload = {
        "paymentDetails": {
            "amount": round(float(amount), 2),
            "currency": "RUB",
        },
        "description": description[:255],
        "return": return_to,
        "failedUrl": f"{settings.site_base_url}/payment/failed?order_id={order_id}",
        # Поле "payload" не существует в Platega API — Platega молча игнорирует.
        # Для связи заказа с транзакцией используем поиск по transaction_id.
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{settings.platega_api_url}/v2/transaction/process",
            json=payload,
            headers=_headers(),
        )

    if resp.status_code not in (200, 201):
        raise PlategaError(
            f"Platega create failed: {resp.status_code} {resp.text}"
        )

    data = resp.json()
    tx_id = data.get("id") or data.get("transactionId")
    payment_url = (
        data.get("redirect")
        or data.get("redirectUrl")
        or data.get("url")
    )

    if not tx_id or not payment_url:
        raise PlategaError(f"Platega unexpected response: {data}")

    return {
        "transaction_id": str(tx_id),
        "payment_url": payment_url,
        "status": str(data.get("status", "PENDING")).upper(),
    }


async def check_status(transaction_id: str) -> str:
    """
    Возвращает нормализованный статус:
    'pending' | 'succeeded' | 'cancelled'

    Platega API: GET /transaction/{id}
    Подтверждённые статусы: CONFIRMED, PAID, COMPLETED
    Отменённые: CANCELED, CANCELLED, CHARGEBACKED, REFUNDED
    """
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{settings.platega_api_url}/transaction/{transaction_id}",
            headers=_headers(),
        )

    if resp.status_code != 200:
        raise PlategaError(
            f"Platega status check failed: {resp.status_code} {resp.text}"
        )

    data = resp.json()
    status = str(data.get("status", "")).upper()

    # Успешные статусы — платёж прошёл
    if status in ("CONFIRMED", "PAID", "COMPLETED"):
        return "succeeded"
    # Отменённые / возврат
    if status in ("CANCELED", "CANCELLED", "CHARGEBACKED", "REFUNDED"):
        return "cancelled"
    # Всё остальное — ожидание
    return "pending"
