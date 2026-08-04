import asyncio
import logging
import time
import httpx
from config import settings

logger = logging.getLogger(__name__)

# ── Persistent HTTP client (singleton) ──────────────────────────────────────
# Все запросы к 3x-UI идут через одну сессию с connection pooling.
# Раньше httpx.AsyncClient создавался на каждый вызов — лишний TLS handshake
# на каждом запросе. Теперь: один TCP/TLS handshake → keep-alive → reuse.
_http_client: httpx.AsyncClient | None = None


async def get_http_client() -> httpx.AsyncClient:
    """Возвращает (или создаёт) единственный httpx.AsyncClient для 3x-UI."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            verify=_verify_ssl(),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=5),
        )
        logger.debug("Created new httpx.AsyncClient for 3x-UI")
    return _http_client


async def close_http_client():
    """Закрывает persistent client при shutdown сервера."""
    global _http_client
    if _http_client is not None and not _http_client.is_closed:
        await _http_client.aclose()
        _http_client = None
        logger.debug("Closed httpx.AsyncClient for 3x-UI")


# ── Per-email locks (TOCTOU protection) ─────────────────────────────────────
# update_client_limit и renew_client делают read-modify-write: GET → modify → POST.
# Без лока два одновременных вызова перезапишут изменения друг друга.
_email_locks: dict[str, asyncio.Lock] = {}


def _get_email_lock(email: str) -> asyncio.Lock:
    """Возвращает Lock для конкретного email (создаётся один раз)."""
    if email not in _email_locks:
        _email_locks[email] = asyncio.Lock()
    return _email_locks[email]


# ── Managed-panel-email guard ────────────────────────────────────────────────
# Сайт создаёт клиентов в панели ТОЛЬКО как "<something>@vpn.local"
# (см. fulfill_order / trial / rekey в main.py). Всё остальное в панели —
# ручные клиенты админа или другой системы: их мутировать нельзя.
PANEL_EMAIL_SUFFIX = "@vpn.local"


def is_managed_panel_email(email: object) -> bool:
    """Возвращает True, если email клиента создан этим сайтом.

    Защита перед любой мутацией: отказываемся трогать клиентов панели,
    которых мы не создавали (ручные, из бота, из других сервисов).
    """
    if not isinstance(email, str):
        return False
    return email.strip().casefold().endswith(PANEL_EMAIL_SUFFIX)


def _require_managed_email(email: object) -> str:
    """Проверяет email и возвращает его; иначе бросает XuiError."""
    if not is_managed_panel_email(email):
        raise XuiError(
            f"Refusing to mutate unmanaged panel client: {email!r} "
            f"(managed emails end with {PANEL_EMAIL_SUFFIX})"
        )
    return email


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
    return settings.xui_verify_ssl


def _parse_inbound_ids() -> list[int]:
    """Парсит XUI_INBOUND_IDS из строки '5,6,7,8,10,12,13,14' в список."""
    raw = settings.xui_inbound_ids
    if isinstance(raw, list):
        return [int(x) for x in raw]
    return [int(x.strip()) for x in str(raw).split(",") if x.strip()]


async def create_client(
    email: str,
    duration_days: int = 0,
    limit_ip: int = 1,
    total_gb: int = 0,
    expiry_ms: int | None = None,
) -> dict:
    """
    Создаёт клиента сразу во ВСЕХ видимых инбаундах и возвращает subId.

    POST /add не возвращает subId — нужно делать GET /get/{email} после создания.

    ``expiry_ms`` (мс) задаёт точную дату истечения и имеет приоритет над
    ``duration_days`` — используется при перевыпуске ключа (rekey), чтобы
    сохранить оставшееся время подписки.

    Возвращает: {"email": ..., "sub_id": ..., "uuid": ...}
    """
    _require_managed_email(email)
    inbound_ids = _parse_inbound_ids()
    expiry = expiry_ms if expiry_ms is not None else _ms_timestamp(duration_days)
    body = {
        "client": {
            "email": email,
            "enable": True,
            "totalGB": total_gb * 1073741824 if total_gb else 0,  # 0 = безлимит трафика
            "expiryTime": expiry,
            "limitIp": limit_ip,
            "tgId": 0,
            "reset": 0,
        },
        "inboundIds": inbound_ids,  # ← КЛЮЧЕВОЕ: передаём весь список!
    }

    client = await get_http_client()

    # Шаг 1: создаём клиента во всех инбаундах
    resp = await client.post(
        f"{settings.xui_base_url}/panel/api/clients/add",
        json=body, headers=_headers(),
    )

    if resp.status_code != 200:
        raise XuiError(f"3x-UI add error: HTTP {resp.status_code}")

    data = resp.json()
    if not data.get("success"):
        msg = data.get("msg", "")
        # Идемпотентность: если клиент уже существует — не ошибка.
        # ВАЖНО: не матчим просто "exist" — иначе "does not exist" тоже попадёт сюда
        msg_lower = str(msg).lower()
        if "already exists" in msg_lower or "already in use" in msg_lower:
            pass
        else:
            raise XuiError(f"3x-UI add error: {msg}")

    # Шаг 2: получаем subId и uuid через GET /get/{email}
    # Retry loop вместо sleep(0.3) — panelsync может быть медленным.
    # Параметры ретраев — из RETRY_CONFIG (.env).
    rc = settings.retry_config
    for attempt in range(rc["retries"]):
        await asyncio.sleep(rc["base_delay"] + attempt * rc["backoff"])
        resp = await client.get(
            f"{settings.xui_base_url}/panel/api/clients/get/{email}",
            headers=_headers(),
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("success"):
                client_obj = data["obj"]["client"]
                return {
                    "email": email,
                    "sub_id": client_obj["subId"],
                    "uuid": client_obj.get("uuid", ""),
                }
        if attempt < rc["retries"] - 1:
            logger.debug("create_client: GET /get/%s attempt %d failed, retrying", email, attempt + 1)

    raise XuiError(f"3x-UI get error: client {email} not found after create")


async def get_subscription_url(sub_id: str) -> str:
    """
    Возвращает полный URL подписки.
    Строится из настроек панели, НЕ из API.
    Гарантирует '/' между базовым URL и sub_id.
    """
    base = settings.xui_sub_base_url.rstrip("/")
    return f"{base}/{sub_id}"


async def get_share_links(sub_id: str) -> list[str]:
    """
    Получает отдельные vless:// / hysteria:// ссылки (для отображения/QR).
    /subLinks возвращает массив строк!
    """
    client = await get_http_client()
    resp = await client.get(
        f"{settings.xui_base_url}/panel/api/clients/subLinks/{sub_id}",
        headers=_headers(),
    )

    if resp.status_code != 200:
        raise XuiError(f"subLinks error: HTTP {resp.status_code}")

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


async def get_client_info(email: str) -> dict:
    """Read-only: возвращает текущие данные клиента из 3x-UI (без мутаций).

    Используется дебаг-песочницей для Force Sync Preview и инспекции
    реального limitIp в 3x-UI относительно ожидаемого в БД.
    """
    client = await get_http_client()
    resp = await client.get(
        f"{settings.xui_base_url}/panel/api/clients/get/{email}",
        headers=_headers(),
    )
    if resp.status_code != 200:
        raise XuiError(f"Cannot find client {email}: HTTP {resp.status_code}")
    data = resp.json()
    if not data.get("success"):
        raise XuiError(f"Cannot find client {email}")
    return data["obj"]["client"]  # содержит limitIp, expiryTime, enable, total и т.д.


async def update_client_limit(email: str, new_limit_ip: int) -> dict:
    """Обновляет limit_ip клиента в 3x-UI. Защищено per-email lock от TOCTOU."""
    _require_managed_email(email)
    async with _get_email_lock(email):
        return await _update_client_limit_unlocked(email, new_limit_ip)


async def _update_client_limit_unlocked(email: str, new_limit_ip: int) -> dict:
    """Внутренняя реализация — вызывается из-под lock."""
    client = await get_http_client()
    resp = await client.get(
        f"{settings.xui_base_url}/panel/api/clients/get/{email}",
        headers=_headers(),
    )

    if resp.status_code != 200:
        raise XuiError(f"Cannot find client {email}: HTTP {resp.status_code}")

    data = resp.json()
    if not data.get("success"):
        raise XuiError(f"Cannot find client {email}")

    current = data["obj"]["client"]
    current.pop("id", None)
    current["limitIp"] = new_limit_ip

    resp = await client.post(
        f"{settings.xui_base_url}/panel/api/clients/update/{email}",
        json=current, headers=_headers(),
    )

    if resp.status_code != 200:
        raise XuiError(f"3x-UI update limit error: HTTP {resp.status_code}")

    data = resp.json()
    if not data.get("success"):
        raise XuiError(f"3x-UI update limit error: {data.get('msg')}")

    return {"email": email, "new_limit_ip": new_limit_ip}


async def renew_client(email: str, add_days: int) -> dict:
    """
    Продлевает подписку. POST /update/{email} требует ПОЛНУЮ модель клиента,
    а не патч — поэтому сначала читаем текущую.

    Продление идёт от текущей даты истечения (max(current_expiry, now)).
    Защищено per-email lock от TOCTOU.
    """
    _require_managed_email(email)
    async with _get_email_lock(email):
        return await _renew_client_unlocked(email, add_days)


async def _renew_client_unlocked(email: str, add_days: int) -> dict:
    """Внутренняя реализация — вызывается из-под lock."""
    client = await get_http_client()

    # 1. Читаем текущие данные
    resp = await client.get(
        f"{settings.xui_base_url}/panel/api/clients/get/{email}",
        headers=_headers(),
    )

    if resp.status_code != 200:
        raise XuiError(f"Cannot find client {email}: HTTP {resp.status_code}")

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
    resp = await client.post(
        f"{settings.xui_base_url}/panel/api/clients/update/{email}",
        json=current, headers=_headers(),
    )

    if resp.status_code != 200:
        raise XuiError(f"3x-UI update error: HTTP {resp.status_code}")

    data = resp.json()
    if not data.get("success"):
        raise XuiError(f"3x-UI update error: {data.get('msg')}")

    return {"email": email, "new_expiry_ms": current["expiryTime"]}


async def check_client_status(email: str) -> dict:
    """
    Проверяет статус клиента (активен, сколько осталось, сколько потрачено).
    """
    client = await get_http_client()
    resp = await client.get(
        f"{settings.xui_base_url}/panel/api/clients/get/{email}",
        headers=_headers(),
    )

    if resp.status_code != 200:
        raise XuiError(f"Client not found: {email} (HTTP {resp.status_code})")

    data = resp.json()
    if not data.get("success"):
        raise XuiError(f"Client not found: {email}")

    c = data["obj"]["client"]
    return {
        "email": email,
        "enable": c.get("enable", False),
        "expiry_ms": c.get("expiryTime", 0),
        "total_gb": c.get("totalGB", 0),
        "up": c.get("up", 0),      # трафик аплоад, байты
        "down": c.get("down", 0),  # трафик даунлоад, байты
        "sub_id": c.get("subId", ""),
        "inbound_ids": data["obj"].get("inboundIds", []),
    }


async def delete_client(email: str) -> dict:
    """
    Удаляет клиента из 3x-UI (из всех инбаундов, где он есть).

    Используется при удалении подписки и при перевыпуске ключа.
    """
    _require_managed_email(email)
    client = await get_http_client()
    resp = await client.post(
        f"{settings.xui_base_url}/panel/api/clients/del/{email}",
        headers=_headers(),
    )

    if resp.status_code != 200:
        raise XuiError(f"3x-UI delete error: HTTP {resp.status_code}")

    data = resp.json()
    if not data.get("success"):
        raise XuiError(f"3x-UI delete error: {data.get('msg')}")

    return {"email": email, "deleted": True}


async def rekey_client(
    old_email: str,
    new_email: str,
    expiry_ms: int,
    limit_ip: int = 1,
    total_gb: int = 0,
) -> dict:
    """
    Перевыпуск ключа: создаёт НОВОГО клиента, затем удаляет старого.

    Порядок важен для атомарности: create-first, then delete. Если создание
    нового клиента упадёт — старый ключ остаётся цел, подписка не потеряна.

    ``expiry_ms`` — дата истечения нового клиента (мс). Позволяет сохранить
    оставшееся время подписки при перевыпуске.

    Возвращает: {"email": ..., "sub_id": ..., "uuid": ...}
    """
    _require_managed_email(old_email)
    # Шаг 1: создаём нового клиента. При ошибке поднимаем XuiError —
    # старый клиент не тронут, подписка продолжает работать.
    result = await create_client(
        email=new_email,
        expiry_ms=expiry_ms,
        limit_ip=limit_ip,
        total_gb=total_gb,
    )

    # Шаг 2: удаляем старого клиента. Если он уже удалён (not found) —
    # продолжаем. Ошибка удаления не фатальна: новый ключ уже работает.
    try:
        await delete_client(old_email)
    except XuiError as e:
        msg = str(e).lower()
        if "not found" not in msg and "not exist" not in msg:
            logger.warning("Rekey: delete old client %s failed: %s", old_email, e)

    return result


async def get_visible_inbound_ids() -> list[int]:
    """
    Получает список видимых инбаундов с панели, фильтруя --! префикс.
    Улучшение: если в .env список не задан — получаем динамически.
    """
    client = await get_http_client()
    resp = await client.get(
        f"{settings.xui_base_url}/panel/api/inbounds/list",
        headers=_headers(),
    )

    if resp.status_code != 200:
        raise XuiError(f"inbounds/list error: HTTP {resp.status_code}")

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
