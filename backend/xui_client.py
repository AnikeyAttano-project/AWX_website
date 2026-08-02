import time
import uuid
import httpx
from config import settings


class XuiError(Exception):
    pass


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.xui_api_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _ms_timestamp(days: int) -> int:
    """Возвращает epoch-миллисекунды для срока действия."""
    return int((time.time() + days * 86400) * 1000)


async def create_client(
    email: str,
    duration_days: int,
    limit_ip: int = 1,
    total_gb: int = 0,
) -> dict:
    """
    Создаёт клиента в 3x-UI и привязывает к инбаунду.

    Returns: {"email": str, "sub_id": str, "uuid": str}
    """
    body = {
        "client": {
            "email": email,
            "enable": True,
            "totalGB": total_gb * 1073741824 if total_gb else 0,
            "expiryTime": _ms_timestamp(duration_days),
            "limitIp": limit_ip,
            "tgId": 0,
            "reset": 0,
        },
        "inboundIds": [settings.xui_inbound_id],
    }

    async with httpx.AsyncClient(timeout=30, verify=False) as client:
        resp = await client.post(
            f"{settings.xui_base_url}/panel/api/clients/add",
            json=body,
            headers=_headers(),
        )

    if resp.status_code != 200:
        raise XuiError(f"3x-UI add failed: {resp.status_code} {resp.text}")

    data = resp.json()
    if not data.get("success"):
        raise XuiError(f"3x-UI add error: {data.get('msg')}")

    obj = data.get("obj", {})
    # subId и uuid приходят внутри объекта клиента
    client_obj = obj.get("client", obj) if isinstance(obj, dict) else {}
    sub_id = client_obj.get("subId") or obj.get("subId", "")
    client_uuid = client_obj.get("uuid") or obj.get("uuid", "")

    if not sub_id:
        # Если subId не вернулся в ответе add — достаём через get
        sub_id = await _get_sub_id(email)

    return {"email": email, "sub_id": sub_id, "uuid": client_uuid}


async def _get_sub_id(email: str) -> str:
    async with httpx.AsyncClient(timeout=30, verify=False) as client:
        resp = await client.get(
            f"{settings.xui_base_url}/panel/api/clients/get/{email}",
            headers=_headers(),
        )

    data = resp.json()
    obj = data.get("obj", {})
    client_obj = obj.get("client", obj) if isinstance(obj, dict) else {}
    sub_id = client_obj.get("subId", "")

    if not sub_id:
        raise XuiError(f"subId не найден для клиента {email}")

    return sub_id


async def get_sub_links(sub_id: str) -> dict:
    """
    Получает sub-ссылку и share-ссылки.

    Returns: {"sub_url": str, "links": list[str]}
    """
    async with httpx.AsyncClient(timeout=30, verify=False) as client:
        resp = await client.get(
            f"{settings.xui_base_url}/panel/api/clients/subLinks/{sub_id}",
            headers=_headers(),
        )

    if resp.status_code != 200:
        raise XuiError(f"3x-UI subLinks failed: {resp.status_code}")

    data = resp.json()
    obj = data.get("obj", {})

    sub_url = obj.get("subUrl") or obj.get("subscriptionUrl") or ""
    links = obj.get("links") or obj.get("shareLinks") or []

    if not sub_url:
        raise XuiError(f"sub_url пустой для subId={sub_id}: {obj}")

    return {"sub_url": sub_url, "links": links}


async def renew_client(email: str, duration_days: int):
    """Продление: сдвигает expiryTime. Необязательный метод."""
    body = {
        "email": email,
        "enable": True,
        "expiryTime": _ms_timestamp(duration_days),
        "totalGB": 0,
        "limitIp": 1,
        "tgId": 0,
        "reset": 0,
    }

    async with httpx.AsyncClient(timeout=30, verify=False) as client:
        resp = await client.post(
            f"{settings.xui_base_url}/panel/api/clients/update/{email}",
            json=body,
            headers=_headers(),
        )

    if resp.status_code != 200:
        raise XuiError(f"3x-UI renew error: {resp.status_code}")

    data = resp.json()
    if not data.get("success"):
        raise XuiError(f"3x-UI renew error: {data.get('msg')}")
