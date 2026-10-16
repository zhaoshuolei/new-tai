from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "tianxiadmin"
    env: Literal["local", "test", "prod"] = "local"
    database_url: str
    redis_url: str
    jwt_secret: str
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7
    cors_origins: list[str] = []
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_redirect_uri: str = ""
    initial_superadmin_username: str = "superadmin"
    initial_superadmin_password: str = ""
    monitor_enabled: bool = True
    monitor_sample_interval_seconds: int = 60
    monitor_metric_retention_days: int = 30
    exception_log_retention_days: int = 90
    login_log_retention_days: int = 180
    audit_log_retention_days: int = 365


settings = Settings()
