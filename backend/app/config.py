from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"
    database_url: str = "sqlite:///./canteen.db"
    jwt_secret_key: str = "local-development-only-change-me"
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 720
    allowed_origins: str = "http://localhost:5500,http://127.0.0.1:5500,http://localhost:8000,http://127.0.0.1:8000"
    admin_email: str | None = None
    admin_password: str | None = None
    admin_name: str = "Canteen Admin"
    google_client_id: str | None = None
    razorpay_key_id: str | None = None
    razorpay_key_secret: str | None = None
    razorpay_webhook_secret: str | None = None
    upi_vpa: str = "adityaraj123beg@oksbi"
    upi_payee_name: str = "GEC Khagaria Canteen"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.allowed_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if bool(settings.admin_email) != bool(settings.admin_password):
        raise ValueError("Set both ADMIN_EMAIL and ADMIN_PASSWORD to provision the admin account.")
    if bool(settings.razorpay_key_id) != bool(settings.razorpay_key_secret):
        raise ValueError("Set both RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET to enable UPI payments.")
    if settings.app_env == "production" and settings.jwt_secret_key == "local-development-only-change-me":
        raise ValueError("JWT_SECRET_KEY must be changed in production.")
    return settings