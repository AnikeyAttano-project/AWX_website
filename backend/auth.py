"""
JWT authentication for AWX-WEB-lite.
Provides register, login, verification, and token validation.
"""
import uuid
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel
from passlib.context import CryptContext
from jose import JWTError, jwt

from config import settings
from database import (
    get_user_by_email, create_user, get_user_by_id, set_user_verified,
)

logger = logging.getLogger(__name__)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
auth_router = APIRouter(prefix="/api/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    email: str
    password: str


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
    return await get_user_by_id(user_id)


async def require_verified_email(user: dict = Depends(get_current_user)):
    """403 if email verification is required but not done."""
    if settings.email_verification_required and not user.get("verified"):
        raise HTTPException(status_code=403, detail="Email not verified")
    return user


@auth_router.post("/register")
async def register(req: RegisterRequest):
    """Register a new user. Returns JWT token."""
    if len(req.password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    if len(req.email) < 3 or "@" not in req.email:
        raise HTTPException(400, "Invalid email address")

    existing = await get_user_by_email(req.email)
    if existing:
        raise HTTPException(409, "Email already registered")

    user_id = uuid.uuid4().hex[:12]
    password_hash = pwd_context.hash(req.password)
    verified = 0 if settings.email_verification_required else 1
    await create_user(user_id, req.email.lower().strip(), password_hash, verified=verified)

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
async def login(req: LoginRequest):
    """Login with email and password. Returns JWT token."""
    user = await get_user_by_email(req.email.lower().strip())
    if not user:
        raise HTTPException(401, "Invalid email or password")
    if not pwd_context.verify(req.password, user["password_hash"]):
        raise HTTPException(401, "Invalid email or password")
    token = create_token(user["id"])
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
