"""
AWX-WEB-lite: FastAPI entry point (рефакторинг Части 4).

Здесь только: создание app, middleware, lifespan, include_router.
Вся логика — в shared_state.py (общие хелперы и инстансы PaymentLifecycle)
и routers/*.py (эндпоинты). Причина: main.py вырос до ~1700 строк, что
затрудняло навигацию; вынесение в роутеры избавило от этого.
"""
import asyncio
import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from config import settings
from database import init_db, load_runtime_settings, prune_site_log
from shared_state import (
    cleanup_expired_subscriptions,
    fulfill_order, fulfill_addon, _renew_subscription_core,
    order_lifecycle, addon_lifecycle, renewal_lifecycle,
    get_real_ip, check_rate_limit,
    logger,
)
from admin import admin_router
from admin_debug import admin_debug_router
from auth import auth_router
from routers import orders as orders_mod
from routers import account as account_mod
from routers import webhook as webhook_mod
from routers import pages as pages_mod


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await load_runtime_settings()
    logger.info("Database initialized, runtime settings loaded")

    # Запускаем фоновую очистку устаревших записей (каждые 6 часов)
    async def periodic_cleanup():
        while True:
            await asyncio.sleep(6 * 3600)  # 6 часов
            try:
                await cleanup_expired_subscriptions()
                await prune_site_log(keep=5000)  # держим общий лог в пределах 5000 записей
            except Exception as e:
                logger.error("Periodic cleanup error: %s", e)

    cleanup_task = asyncio.create_task(periodic_cleanup())
    logger.info("Periodic cleanup task started (every 6 hours)")

    yield

    cleanup_task.cancel()
    # Закрываем persistent httpx client для 3x-UI
    from xui_client import close_http_client
    await close_http_client()


app = FastAPI(title="VPN Shop", lifespan=lifespan)

# CORS: разрешаем только указанные домены (в .env ALLOWED_ORIGINS)
try:
    allowed_origins = json.loads(settings.allowed_origins)
except (AttributeError, json.JSONDecodeError):
    allowed_origins = ["http://localhost:3000", "http://localhost:8000"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
    allow_credentials=True,
)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    """Добавляет заголовки безопасности (CSP, referrer, nosniff) к HTML-ответам."""
    response = await call_next(request)
    # Referrer-Policy: no-referrer — критично: capability-токен в URL возврата
    # не должен утекать через Referer на сторонние CDN (fonts).
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if response.headers.get("content-type", "").startswith("text/html"):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "frame-ancestors 'none'"
        )
    return response


# ————————————————— РОУТЕРЫ —————————————————
app.include_router(admin_router)
app.include_router(admin_debug_router)
app.include_router(auth_router)
app.include_router(account_mod.router)
app.include_router(account_mod.referral_router)
app.include_router(orders_mod.router)
app.include_router(webhook_mod.router)
app.include_router(pages_mod.router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
