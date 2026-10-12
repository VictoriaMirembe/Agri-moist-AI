# App settings, read from environment variables and .env. Never put secrets in code.
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "sqlite:///./data/agrimoist.db"
    app_port: int = 8000
    app_env: str = "development"

    # Open-weights model (Groq, OpenAI-compatible API). Blank key = deterministic stub mode.
    groq_api_key: str | None = None
    groq_model: str = "openai/gpt-oss-120b"
    groq_base_url: str = "https://api.groq.com/openai/v1"

    # Weather. Open-Meteo needs no key.
    open_meteo_base_url: str = "https://api.open-meteo.com/v1/forecast"
    weather_fail: bool = False  # T23: demo/test switch that forces get_weather to fail

    # Optional demo/eval override so a specific rain scenario can be rehearsed or
    # evaluated on demand (live weather cannot be forced to "little rain" on
    # purpose). When set, get_weather returns these figures instead of calling
    # Open-Meteo, and labels the source as an override rather than a live reading -
    # never silently pretends to be real data (README "Safety rules").
    weather_override_past_mm: float | None = None
    weather_override_next_mm: float | None = None

    farmer_language: str = "Ngakarimojong"

    @property
    def model_configured(self) -> bool:
        return bool(self.groq_api_key)


settings = Settings()
