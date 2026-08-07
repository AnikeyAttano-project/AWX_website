"""
JWT authentication for AWX-WEB-lite.
Provides register, login, verification, and token validation.
"""
import uuid
import logging
from collections import defaultdict
from datetime import datetime, timedelta

from fastapi import APIRouter, Body, Depends, HTTPException, Header, Request
from pydantic import BaseModel
from passlib.context import CryptContext
from jose import JWTError, jwt

from config import settings
from database import (
    get_user_by_email, create_user, get_user_by_id, set_user_verified,
    get_user_by_referral_code, apply_referral_code, get_setting,
    get_user_by_telegram, create_telegram_user, set_user_telegram,
    add_site_log,
)
from shared_state import get_real_ip, check_rate_limit
from tg_auth import verify_telegram_auth, TelegramAuthError

logger = logging.getLogger(__name__)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
auth_router = APIRouter(prefix="/api/auth", tags=["auth"])

# Защита /auth/login от перебора: email -> timestamps неудачных попыток.
# (in-memory, как и rate_limit_storage; теряется при рестарте — приемлемо.)
_failed_logins = defaultdict(list)


def _prune_failed_logins():
    """Удаляет ключи, у которых все попытки истекли (ограничение памяти)."""
    now = datetime.now()
    cutoff = now - timedelta(minutes=settings.auth_lockout_minutes)
    expired = [
        email for email, stamps in _failed_logins.items()
        if not [t for t in stamps if t > cutoff]
    ]
    for email in expired:
        del _failed_logins[email]


def _is_account_locked(email: str) -> bool:
    now = datetime.now()
    cutoff = now - timedelta(minutes=settings.auth_lockout_minutes)
    recent = [t for t in _failed_logins[email] if t > cutoff]
    _failed_logins[email] = recent
    return len(recent) >= settings.auth_lockout_after


def _record_failed_login(email: str):
    _failed_logins[email].append(datetime.now())
    if len(_failed_logins[email]) > 50:  # cap рост на один email
        _failed_logins[email] = _failed_logins[email][-50:]
    if len(_failed_logins) > 2000:  # глобальная чистка
        _prune_failed_logins()


def _reset_failed_logins(email: str):
    _failed_logins.pop(email, None)


class RegisterRequest(BaseModel):
    email: str
    password: str
    referral_code: str = ""  # необязательный реферальный код


class LoginRequest(BaseModel):
    email: str
    password: str


def create_token(user_id: str) -> str:
    """Create a JWT token for the given user_id."""
    expire = datetime.utcnow() + timedelta(hours=settings.jwt_expire_hours)
    payload = {"sub": user_id, "exp": expire}
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def create_verification_token(user_id: str) -> str:
    """JWT used ONLY for email verification. 24h expiry."""
    expire = datetime.utcnow() + timedelta(hours=24)
    payload = {"sub": user_id, "type": "email_verify", "exp": expire}
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


async def get_current_user(authorization: str = Header(default="")) -> dict:
    """FastAPI dependency: extract user from Bearer token. Raises 401."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = authorization[7:]
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    user = await get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    if user.get("blocked"):
        raise HTTPException(status_code=403, detail="Account blocked")
    return user


async def get_optional_user(authorization: str = Header(default="")) -> dict | None:
    """Like get_current_user but returns None instead of raising 401."""
    if not authorization.startswith("Bearer "):
        return None
    token = authorization[7:]
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
        user_id = payload.get("sub")
        if not user_id:
            return None
    except JWTError:
        return None
    user = await get_user_by_id(user_id)
    if user and user.get("blocked"):
        return None
    return user


async def require_verified_email(user: dict = Depends(get_current_user)):
    """403 if email verification is required but not done."""
    if settings.email_verification_required and not user.get("verified"):
        raise HTTPException(status_code=403, detail="Email not verified")
    return user


@auth_router.post("/register")
async def register(req: RegisterRequest, request: Request):
    """Register a new user. Returns JWT token."""
    ip = get_real_ip(request)
    if not check_rate_limit(ip, settings.auth_rate_limit_per_hour, 60):
        raise HTTPException(429, "Too many registration attempts, try later")
    if len(req.password) < settings.password_min_length:
        raise HTTPException(
            400, f"Password must be at least {settings.password_min_length} characters")
    if len(req.password) > settings.password_max_length:
        raise HTTPException(
            400, f"Password must be at most {settings.password_max_length} characters")
    if len(req.email) < 3 or "@" not in req.email:
        raise HTTPException(400, "Invalid email address")

    existing = await get_user_by_email(req.email)
    if existing:
        raise HTTPException(409, "Email already registered")

    user_id = uuid.uuid4().hex[:12]
    password_hash = pwd_context.hash(req.password)
    verified = 0 if settings.email_verification_required else 1
    await create_user(user_id, req.email.lower().strip(), password_hash, verified=verified)
    await add_site_log("register", actor=user_id, details=req.email.lower().strip())

    # Привязываем реферальный код, если указан при регистрации (не критично при ошибке)
    if req.referral_code and (await get_setting("referral_enabled", "1")) == "1":
        code = req.referral_code.strip().upper()
        referrer = await get_user_by_referral_code(code)
        if referrer and referrer["id"] != user_id:
            await apply_referral_code(user_id, referrer["id"])

    result = {
        "token": create_token(user_id),
        "user_id": user_id,
        "email": req.email.lower().strip(),
        "verified": bool(verified),
    }

    if settings.email_verification_required:
        vtoken = create_verification_token(user_id)
        verify_url = f"{settings.site_base_url}/api/auth/verify?token={vtoken}"
        # НЕ возвращаем URL в ответе (безопасность) — только в лог
        logger.info("Verify link for %s: %s", user_id, verify_url)
        result["message"] = "Check your email for verification link"

    return result


@auth_router.get("/verify")
async def verify_email(token: str):
    """Verify email via token link (GET so it works in browser)."""
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except JWTError:
        raise HTTPException(400, "Invalid or expired verification link")
    if payload.get("type") != "email_verify":
        raise HTTPException(400, "Invalid verification token")
    user = await get_user_by_id(payload["sub"])
    if not user:
        raise HTTPException(400, "User not found")
    if user.get("verified"):
        return {"ok": True, "message": "Email already verified"}
    await set_user_verified(payload["sub"])
    return {"ok": True, "message": "Email verified"}


@auth_router.post("/login")
async def login(req: LoginRequest, request: Request):
    """Login with email and password. Returns JWT token."""
    ip = get_real_ip(request)
    if not check_rate_limit(ip, settings.auth_rate_limit_per_hour, 60):
        raise HTTPException(429, "Too many login attempts, try later")

    email = req.email.lower().strip()
    if _is_account_locked(email):
        raise HTTPException(
            429, "Too many failed attempts. Try again in a few minutes.")

    user = await get_user_by_email(email)
    if not user:
        _record_failed_login(email)
        raise HTTPException(401, "Invalid email or password")
    if not pwd_context.verify(req.password, user["password_hash"]):
        _record_failed_login(email)
        raise HTTPException(401, "Invalid email or password")
    if user.get("blocked"):
        raise HTTPException(403, "Account blocked")
    _reset_failed_logins(email)
    token = create_token(user["id"])
    await add_site_log("login", actor=user["id"], details=user["email"])
    return {
        "token": token,
        "user_id": user["id"],
        "email": user["email"],
        "verified": bool(user.get("verified")),
    }


@auth_router.get("/me")
async def get_me(user: dict = Depends(get_current_user)):
    """Get current user profile."""
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    trial_active = bool(
        user.get("trial_started_at") and user.get("trial_expires_at")
        and user["trial_expires_at"] > now_str
    )
    return {
        "user_id": user["id"],
        "email": user["email"],
        "telegram_id": user.get("telegram_id"),
        "verified": bool(user.get("verified")),
        "trial_active": trial_active,
        "created_at": user["created_at"],
    }


# ————————————————— TELEGRAM LOGIN —————————————————

async def _telegram_signature_guard(payload: dict):
    """Проверяет подпись Telegram Login Widget; бросает 403 при невалидных данных."""
    try:
        verify_telegram_auth(payload)
    except TelegramAuthError as e:
        raise HTTPException(403, str(e))


@auth_router.post("/telegram")
async def telegram_login(payload: dict = Body(...)):
    """Вход через Telegram Login Widget.

    Принимает сырой объект юзера от виджета (id, first_name, last_name,
    username, photo_url?, auth_date, hash). ВАЖНО: подпись считается по
    ФАКТИЧЕСКИ присланным полям, поэтому берём dict как есть, а не
    Pydantic-модель с дефолтами (они бы сломали проверку).

    Находит пользователя по telegram_id или создаёт нового (email tg_{id}@t.me),
    выдаёт стандартный JWT. Ответ — как у POST /api/auth/login.
    """
    await _telegram_signature_guard(payload)

    tg_id = str(payload.get("id", ""))
    if not tg_id or tg_id == "None":
        raise HTTPException(400, "Missing Telegram id")

    user = await get_user_by_telegram(tg_id)
    if not user:
        dummy_email = f"tg_{tg_id}@t.me"
        # Экстремальный кейс: кто-то зарегистрировался с таким email → конфликт.
        if await get_user_by_email(dummy_email):
            raise HTTPException(409, "Conflict: email already registered")
        user_id = uuid.uuid4().hex[:12]
        await create_telegram_user(user_id, tg_id)
        user = await get_user_by_id(user_id)

    if user.get("blocked"):
        raise HTTPException(403, "Account blocked")

    token = create_token(user["id"])
    await add_site_log("telegram_login", actor=user["id"], details=user["email"])
    return {
        "token": token,
        "user_id": user["id"],
        "email": user["email"],
        "verified": bool(user.get("verified")),
    }


@auth_router.post("/telegram/bind")
async def telegram_bind(payload: dict = Body(...), user: dict = Depends(get_current_user)):
    """Привязка Telegram к текущему аккаунту (OAuth-привязка).

    Позволяет входить одним аккаунтом и по email, и через Telegram.
    Если telegram_id уже привязан к ДРУГОМУ пользователю — 409.
    """
    await _telegram_signature_guard(payload)

    tg_id = str(payload.get("id", ""))
    if not tg_id or tg_id == "None":
        raise HTTPException(400, "Missing Telegram id")

    existing = await get_user_by_telegram(tg_id)
    if existing:
        if existing["id"] == user["id"]:
            return {"ok": True, "telegram_id": tg_id, "already_bound": True}
        raise HTTPException(409, "Этот Telegram уже привязан к другому аккаунту")

    await set_user_telegram(user["id"], tg_id)
    await add_site_log("telegram_bind", actor=user["id"], details=f"tg_id={tg_id}")
    return {"ok": True, "telegram_id": tg_id}
