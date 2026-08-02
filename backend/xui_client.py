import asyncio
import time
import httpx
from config import settings


class XuiError(Exception):
    pass


def _headers():
    return {
        "Authorization": f"Bearer {settings.xui_api_token}",
        "Content-Type": "application/json",
    }


def _ms_timestamp(days: int) -> int:
    """Возвращает timestamp в миллисекундах (как ожидает 3x-UI)."""
    return int((time.time() + days * 86400) * 1000)


def _verify_ssl() -> bool:
    """Проверка SSL: по умолчанию отключена для self-signed сертификатов."""
    return settings.xui_verify_ssl.lower() == "true"


def _parse_inbound_ids() -> list[int]:
    """Парсит XUI_INBOUND_IDS из строки '5,6,7,8,10,12,13,14' в список."""
    raw = settings.xui_inbound_ids
    if isinstance(raw, list):
        return [int(x) for x in raw]
    return [int(x.strip()) for x in str(raw).split(",") if x.strip()]


async def create_client(
    email: str,
    duration_days: int,
    limit_ip: int = 1,
    total_gb: int = 0,
) -> dict:
    """
    Создаёт клиента сразу во ВСЕХ видимых инбаундах и возвращает subId.

    POST /add не возвращает subId — нужно делать GET /get/{email} после создания.

    Возвращает: {"email": ..., "sub_id": ..., "uuid": ...}
    """
    inbound_ids = _parse_inbound_ids()
    body = {
        "client": {
            "email": email,
            "enable": True,
            "totalGB": total_gb * 1073741824 if total_gb else 0,  # 0 = безлимит трафика
            "expiryTime": _ms_timestamp(duration_days),
            "limitIp": limit_ip,
            "tgId": 0,
            "reset": 0,
        },
        "inboundIds": inbound_ids,  # ← КЛЮЧЕВОЕ: передаём весь список!
    }

    async with httpx.AsyncClient(timeout=30, verify=_verify_ssl()) as client:
        # Шаг 1: создаём клиента во всех инбаундах
        resp = await client.post(
            f"{settings.xui_base_url}/panel/api/clients/add",
            json=body, headers=_headers(),
        )

    data = resp.json()
    if not data.get("success"):
        msg = data.get("msg", "")
        # Идемпотентность: если клиент уже существует — не ошибка
        msg_lower = str(msg).lower()
        if "already exists" in msg_lower or "already in use" in msg_lower or "exist" in msg_lower:
            pass
        else:
            raise XuiError(f"3x-UI add error: {msg}")

    # Шаг 2: получаем subId и uuid через GET /get/{email}
    # Небольшая задержка на случай гонки БД
    await asyncio.sleep(0.3)

    async with httpx.AsyncClient(timeout=30, verify=_verify_ssl()) as client:
        resp = await client.get(
            f"{settings.xui_base_url}/panel/api/clients/get/{email}",
            headers=_headers(),
        )

    data = resp.json()
    if not data.get("success"):
        raise XuiError(f"3x-UI get error: {data.get('msg')}")

    # data["obj"]["client"] — полная структура ответа
    client_obj = data["obj"]["client"]

    return {
        "email": email,
        "sub_id": client_obj["subId"],
        "uuid": client_obj.get("uuid", ""),
    }


async def get_subscription_url(sub_id: str) -> str:
    """
    Возвращает полный URL подписки.
    Строится из настроек панели, НЕ из API.
    """
    return f"{settings.xui_sub_base_url}{sub_id}"


async def get_share_links(sub_id: str) -> list[str]:
    """
    Получает отдельные vless:// / hysteria:// ссылки (для отображения/QR).
    /subLinks возвращает массив строк!
    """
    async with httpx.AsyncClient(timeout=30, verify=_verify_ssl()) as client:
        resp = await client.get(
            f"{settings.xui_base_url}/panel/api/clients/subLinks/{sub_id}",
            headers=_headers(),
        )

    data = resp.json()
    if not data.get("success"):
        raise XuiError(f"subLinks error: {data.get('msg')}")

    # obj — это ["vless://...", "hysteria://..."] — массив строк!
    links = data.get("obj", [])
    if not isinstance(links, list):
        raise XuiError(f"subLinks: unexpected obj type: {type(links)}")
    return [str(link).strip() for link in links if str(link).strip()]


async def get_sub_links(sub_id: str) -> dict:
    """
    Обратная совместимость: возвращает {"sub_url": ..., "links": [...]}.
    """
    sub_url = await get_subscription_url(sub_id)
    links = await get_share_links(sub_id)
    return {"sub_url": sub_url, "links": links}


async def renew_client(email: str, add_days: int) -> dict:
    """
    Продлевает подписку. POST /update/{email} требует ПОЛНУЮ модель клиента,
    а не патч — поэтому сначала читаем текущую.

    Продление идёт от текущей даты истечения (max(current_expiry, now)).
    """
    async with httpx.AsyncClient(timeout=30, verify=_verify_ssl()) as client:
        # 1. Читаем текущие данные
        resp = await client.get(
            f"{settings.xui_base_url}/panel/api/clients/get/{email}",
            headers=_headers(),
        )

    data = resp.json()
    if not data.get("success"):
        raise XuiError(f"Cannot find client {email}")

    current = data["obj"]["client"]

    # Удаляем числовой id (DB primary key) — Go model.Client.id это string (UUID),
    # иначе JSON unmarshal молча падает с "cannot unmarshal number into string"
    current.pop("id", None)

    # 2. Вычисляем новый expiryTime
    now_ms = int(time.time() * 1000)
    current_exp = int(current.get("expiryTime", 0) or 0)

    # Если подписка ещё активна — продлеваем от текущей даты
    # Если истекла — от сейчас
    base = max(current_exp, now_ms)
    current["expiryTime"] = base + add_days * 86400 * 1000
    current["enable"] = True

    # 3. Отправляем ПОЛНУЮ модель
    async with httpx.AsyncClient(timeout=30, verify=_verify_ssl()) as client:
        resp = await client.post(
            f"{settings.xui_base_url}/panel/api/clients/update/{email}",
            json=current, headers=_headers(),
        )

    data = resp.json()
    if not data.get("success"):
        raise XuiError(f"3x-UI update error: {data.get('msg')}")

    return {"email": email, "new_expiry_ms": current["expiryTime"]}


async def check_client_status(email: str) -> dict:
    """
    Проверяет статус клиента (активен, сколько осталось, сколько потрачено).
    """
    async with httpx.AsyncClient(timeout=30, verify=_verify_ssl()) as client:
        resp = await client.get(
            f"{settings.xui_base_url}/panel/api/clients/get/{email}",
            headers=_headers(),
        )

    data = resp.json()
    if not data.get("success"):
        raise XuiError(f"Client not found: {email}")

    c = data["obj"]["client"]
    return {
        "email": email,
        "enable": c.get("enable", False),
        "expiry_ms": c.get("expiryTime", 0),
        "total_gb": c.get("totalGB", 0),
        "sub_id": c.get("subId", ""),
        "inbound_ids": data["obj"].get("inboundIds", []),
    }


async def get_visible_inbound_ids() -> list[int]:
    """
    Получает список видимых инбаундов с панели, фильтруя --! префикс.
    Улучшение: если в .env список не задан — получаем динамически.
    """
    async with httpx.AsyncClient(timeout=30, verify=_verify_ssl()) as client:
        resp = await client.get(
            f"{settings.xui_base_url}/panel/api/inbounds/list",
            headers=_headers(),
        )

    data = resp.json()
    if not data.get("success"):
        raise XuiError(f"inbounds/list error: {data.get('msg')}")

    result = []
    for inbound in data.get("obj", []):
        remark = (inbound.get("remark") or "").lstrip()
        if remark.startswith("--!"):
            continue
        result.append(inbound["id"])
    return result
