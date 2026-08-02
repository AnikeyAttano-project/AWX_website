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
) -> dict:
    """
    Создаёт платёжную ссылку.

    Returns: {"transaction_id": str, "payment_url": str, "status": str}
    """
    payload = {
        "paymentDetails": {
            "amount": round(float(amount), 2),
            "currency": "RUB",
        },
        "description": description[:255],
        "return": f"{settings.site_base_url}/payment/success?order_id={order_id}",
        "failedUrl": f"{settings.site_base_url}/payment/failed?order_id={order_id}",
        "payload": order_id,
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

    if status == "CONFIRMED":
        return "succeeded"
    if status in ("CANCELED", "CANCELLED", "CHARGEBACKED"):
        return "cancelled"
    return "pending"
