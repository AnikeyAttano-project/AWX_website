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
    # https://attanovpn.duckdns.org:2096/sub/
    xui_sub_base_url: str = ""

    # SSL: отключить проверку сертификата для self-signed (3x-UI)
    # В продакшене с валидным сертификатом установите "true"
    xui_verify_ssl: str = "false"

    # Platega
    platega_merchant_id: str
    platega_secret: str
    platega_api_url: str = "https://app.platega.io"

    # Site
    site_base_url: str  # https://your-domain.com (БЕЗ слэша)
    database_path: str = "orders.db"
    allowed_origins: str = '["http://localhost:3000","http://localhost:8000"]'

    # Тарифы: slug → (дней, цена в RUB, название)
    tariffs: dict = {
        "month": {"days": 30, "price": 199, "title": "1 месяц"},
        "quarter": {"days": 90, "price": 499, "title": "3 месяца"},
        "year": {"days": 365, "price": 1499, "title": "12 месяцев"}
    }

    @field_validator("xui_inbound_ids")
    @classmethod
    def validate_inbound_ids(cls, v):
        if isinstance(v, list):
            return ",".join(str(i) for i in v)
        return v

    class Config:
        env_file = ".env"


settings = Settings()
