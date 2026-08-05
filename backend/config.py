import json

from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # 3x-UI
    xui_base_url: str  # https://host:port/webBasePath
    xui_api_token: str

    # Теперь это СТРОКА через запятую, парсится в список
    # "5,6,7,8,10,12,13,14" → [5, 6, 7, 8, 10, 12, 13, 14]
    xui_inbound_ids: str = "5,6,7,8,10,12,13,14"

    # Базовый URL subscription-сервера (port 2096)
    # Формат: https://your-panel-host:2096/sub/
    xui_sub_base_url: str = ""

    # SSL: отключить проверку сертификата для self-signed (3x-UI)
    # В продакшене с валидным сертификатом установите true
    xui_verify_ssl: bool = False

    # Retry-конфигурация для операций с 3x-UI (create_client и др.).
    # Задаётся через RETRY_CONFIG в .env как JSON-строка:
    #   RETRY_CONFIG={"retries": 3, "base_delay": 0.5, "backoff": 1.0}
    # retries     — сколько попыток сделать
    # base_delay  — задержка перед первой повторной попыткой, сек
    # backoff     — на сколько сек растёт задержка с каждой попыткой
    retry_config: dict = {"retries": 3, "base_delay": 0.5, "backoff": 1.0}

    # Platega
    platega_merchant_id: str
    platega_secret: str
    platega_api_url: str = "https://app.platega.io"
    # Активная платёжка: 'platega' | 'yookassa'. Меняется в админке
    # (Настройки → Платёжная система), перекрывается из таблицы settings.
    payment_provider: str = "platega"
    yookassa_shop_id: str = ""
    yookassa_secret_key: str = ""

    # Site
    site_base_url: str  # https://your-domain.com (БЕЗ слэша)
    database_path: str = "orders.db"
    allowed_origins: str = '["http://localhost:3000","http://localhost:8000"]'

    # Admin panel
    admin_api_key: str = ""  # X-Admin-Key для /admin/* — задаётся через ADMIN_API_KEY

    # JWT Auth
    jwt_secret: str = ""  # Обязателен! Без него сервер не стартует (fail-fast).
    jwt_expire_hours: int = 720  # 30 дней

    # Email verification (false = auto-verified at registration)
    email_verification_required: bool = False

    # Telegram Login Widget (авторизация через Telegram).
    # token — для криптографической проверки подписи виджета (@BotFather).
    # username — публичное имя бота, фронт по нему рендерит кнопку «Войти через Telegram».
    # Домен должен быть привязан в @BotFather (/setdomain), напр. awx-vpn.duckdns.org.
    telegram_bot_token: str = ""
    telegram_bot_username: str = ""

    # Редактируемый пул инбаундов (если пусто — берётся из XUI_INBOUND_IDS).
    available_inbounds: list[int] = []

    # Trial subscription
    trial_enabled: bool = True
    trial_days: int = 3
    trial_gb: int = 25
    trial_devices: int = 1

    # Демо-режим: кнопка "Демо подписка" на витрине (выдаёт ключ без оплаты).
    # По умолчанию ВЫКЛЮЧЕН для безопасности. Включите DEMO_MODE=true в .env для отладки.
    demo_mode: bool = False
    demo_password: str = "AxZz123@Tt"  # Пароль для демо-оплаты (только для тестирования)

    # Дебаг-песочница биллинга — включает /admin/debug/* эндпоинты.
    # По умолчанию ВЫКЛЮЧЕНА. Никогда не включай на проде с реальными платежами —
    # инструменты умеют мутировать реальные лимиты в 3x-UI и обходить оплату по дизайну.
    debug_sandbox_enabled: bool = False

    # Тарифы: slug → (дней, цена в RUB, название, кол-во устройств, скидка %)
    # inbounds: список инбаундов для ТАРИФА. Пусто ([]) = наследовать от группы,
    # если группы нет — использовать все из XUI_INBOUND_IDS.
    tariffs: dict = {
        "quantum_month": {"days": 31, "price": 300, "title": "Quantum Месяц", "devices": 5, "discount": 0, "inbounds": []},
        "quantum_quarter": {"days": 93, "price": 855, "title": "Quantum 3 Месяца", "devices": 5, "discount": 5, "inbounds": []},
        "quantum_halfyear": {"days": 186, "price": 1620, "title": "Quantum 6 Месяцев", "devices": 5, "discount": 10, "inbounds": []},
        "quantum_year": {"days": 365, "price": 2900, "title": "Quantum 12 Месяцев", "devices": 5, "discount": 20, "inbounds": []},
    }

    # Группы тарифов: {group_id: {id, title, description, inbounds: [...], tariffs: [slug, ...]}}.
    # Пусто = витрина плоская. inbounds группы применяются, если у тарифа своих нет.
    tariff_groups: dict = {}

    # Брендинг: название сайта, акцентный цвет, контакт поддержки, логотип
    # (base64 data-URL), hero-тексты, meta-описание, текст подвала.
    # Редактируется в админке (Настройки → Брендинг), применяется на витрине и в ЛК.
    # Пустые строки = витрина использует дефолтные значения из HTML.
    branding: dict = {
        "site_name": "AWX VPN",
        "accent_color": "#1F5F52",
        "support_contact": "support@awxvpn.com",
        "logo_data_url": "",
        "hero_title": "",
        "hero_subtitle": "",
        "site_description": "",
        "footer_text": "",
    }

    # Add-on пакеты: покупка доп. устройств с пропорциональной оплатой (proration)
    device_addons: dict = {
        "devices_5":  {"extra_devices": 5,  "base_price": 100, "title": "+5 устройств"},
        "devices_10": {"extra_devices": 10, "base_price": 170, "title": "+10 устройств"},
    }

    @field_validator("retry_config")
    @classmethod
    def validate_retry_config(cls, v):
        # Из .env приходит JSON-строка — парсим в dict.
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except json.JSONDecodeError as e:
                raise ValueError(
                    f"RETRY_CONFIG должен быть валидным JSON: {e}"
                ) from e
        if not isinstance(v, dict):
            raise ValueError("RETRY_CONFIG должен быть объектом JSON")
        # Заполняем отсутствующие ключи дефолтами, валидируем типы.
        defaults = {"retries": 3, "base_delay": 0.5, "backoff": 1.0}
        for key, default in defaults.items():
            val = v.get(key, default)
            if not isinstance(val, (int, float)) or val < 0:
                raise ValueError(f"RETRY_CONFIG.{key} должен быть числом >= 0")
            v[key] = val
        v["retries"] = max(1, int(v["retries"]))
        return v

    @field_validator("xui_inbound_ids")
    @classmethod
    def validate_inbound_ids(cls, v):
        if isinstance(v, list):
            return ",".join(str(i) for i in v)
        return v

    @field_validator("xui_sub_base_url")
    @classmethod
    def validate_sub_base_url(cls, v):
        v = str(v or "").strip().rstrip("/")
        if not v:
            raise ValueError("xui_sub_base_url must not be empty")
        # Ensure https:// prefix
        if not v.startswith("http"):
            raise ValueError("xui_sub_base_url must start with http:// or https://")
        return v

    @field_validator("jwt_secret")
    @classmethod
    def validate_jwt_secret(cls, v):
        v = str(v or "").strip()
        # Fail-fast: не запускаемся с публично известным секретом
        if not v or v == "change-me-in-production-use-random-string":
            raise ValueError(
                "JWT_SECRET обязателен. Сгенерируйте случайный: "
                "python -c \"import secrets; print(secrets.token_urlsafe(48))\""
            )
        if len(v) < 32:
            raise ValueError("JWT_SECRET слишком короткий — минимум 32 символа")
        return v

    class Config:
        env_file = Path(__file__).resolve().parent / ".env"


settings = Settings()
