"""Application settings (Pydantic v2 settings; env-driven, no secrets in code)."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Youhue — Student Wellbeing API"
    environment: str = "local"


settings = Settings()
