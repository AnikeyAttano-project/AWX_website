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

    # Platega
    platega_merchant_id: str
    platega_secret: str
    platega_api_url: str = "https://app.platega.io"

    # Site
    site_base_url: str  # https://your-domain.com (БЕЗ слэша)
    database_path: str = "orders.db"
    allowed_origins: str = '["http://localhost:3000","http://localhost:8000"]'

    # Admin panel
    admin_api_key: str = ""  # X-Admin-Key для /admin/* — задаётся через ADMIN_API_KEY

    # JWT Auth
    jwt_secret: str = "change-me-in-production-use-random-string"
    jwt_expire_hours: int = 720  # 30 дней

    # Тарифы: slug → (дней, цена в RUB, название, кол-во устройств, скидка %)
    tariffs: dict = {
        "quantum_month": {"days": 31, "price": 300, "title": "Quantum Месяц", "devices": 5, "discount": 0},
        "quantum_quarter": {"days": 93, "price": 855, "title": "Quantum 3 Месяца", "devices": 5, "discount": 5},
        "quantum_halfyear": {"days": 186, "price": 1620, "title": "Quantum 6 Месяцев", "devices": 5, "discount": 10},
        "quantum_year": {"days": 365, "price": 2900, "title": "Quantum 12 Месяцев", "devices": 5, "discount": 20},
    }

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
        return v

    class Config:
        env_file = Path(__file__).resolve().parent / ".env"


settings = Settings()
