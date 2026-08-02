from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # 3x-UI
    xui_base_url: str  # https://host:port/webBasePath
    xui_api_token: str
    xui_inbound_id: int

    # Platega
    platega_merchant_id: str
    platega_secret: str
    platega_api_url: str = "https://app.platega.io"

    # Site
    site_base_url: str  # https://your-domain.com (БЕЗ слэша)
    database_path: str = "orders.db"

    # Тарифы: slug → (дней, цена в RUB, название)
    tariffs: dict = {
        "month": {"days": 30, "price": 199, "title": "1 месяц"},
        "quarter": {"days": 90, "price": 499, "title": "3 месяца"},
        "year": {"days": 365, "price": 1499, "title": "12 месяцев"}
    }

    class Config:
        env_file = ".env"


settings = Settings()
