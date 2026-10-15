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
    # ISO 639-3 code Sunbird uses for the same language (Karamojong / Ngakarimojong).
    farmer_language_code: str = "kdj"

    # Sunbird AI (Ugandan open models). Its sunflower-9b translator supports
    # Karamojong, so it translates approved advice first; Groq is the backup.
    sunbird_api_key: str | None = None
    sunbird_api_url: str = "https://api.sunbird.ai"

    # USSD short code shown in the simulator. Set it to the code Africa's Talking
    # gives your channel (sandbox codes look like *384*NNNN#).
    ussd_service_code: str = "*384*5951#"
    # Shared secret for the Africa's Talking callback URLs (?token=...). Empty means
    # no check, which is fine on localhost but not on a public host.
    ussd_callback_token: str = ""

    # Signs officer session cookies. Blank = a random key each start (see backend/main.py).
    session_secret: str = ""
    # Shared demo password for the officer sign-in page (shown on that page, like the
    # farmer demo phone numbers). Set your own on a public host.
    officer_demo_password: str = "napak2026"

    @property
    def model_configured(self) -> bool:
        return bool(self.groq_api_key)

    @property
    def translator_configured(self) -> bool:
        return bool(self.sunbird_api_key)


settings = Settings()
