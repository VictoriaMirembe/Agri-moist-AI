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
    # Soil moisture comes from a SIMULATED sensor (no hardware). Demo/test switch that
    # forces get_soil_moisture to fail, to show the retry-then-gap behaviour.
    soil_sensor_fail: bool = False
    # Forecast rain (mm over the next 7 days) at or above which a draft that tells
    # farmers to add water is blocked before review (rain guard in draft_advice).
    heavy_rain_threshold_mm: float = 15.0

    # SIMULATED irrigation (no real valve hardware). A plot is proposed for irrigation
    # only when its current soil reading is below this (volumetric %), the weather is
    # known and forecast rain is below HEAVY_RAIN_THRESHOLD_MM. A proposal never opens a
    # valve by itself: a named officer approves it first (backend/approvals/router.py).
    irrigation_dry_threshold_pct: float = 15.0
    irrigation_default_minutes: int = 20
    irrigation_max_minutes: int = 30  # every proposal and every valve opening is capped here
    # How often the app checks for valves whose time is up (the simulated timer).
    irrigation_sweep_seconds: float = 5.0
    # Demo/test switch for the simulated gateway: "open_fails" (the valve never opens)
    # or "no_confirmation" (it opens but the confirmation is lost). Either way the
    # approval executor must leave the valve closed.
    irrigation_gateway_fault: str = ""

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

    # Officer USSD menu (approve or reject drafts from a basic phone). Its own code,
    # so farmers dialling the farmer code never see it.
    ussd_officer_service_code: str = "*384*5952#"
    # Wrong PINs allowed in a row before the officer's USSD sign-in is locked, and
    # for how long.
    ussd_pin_max_attempts: int = 3
    ussd_pin_lock_minutes: int = 15

    # A draft waiting longer than this becomes "expired" and cannot be approved on
    # any channel: the weather and field facts behind it are too old.
    draft_expiry_hours: int = 72

    # Signs officer and farmer session cookies. Blank = a random key each start (see
    # backend/main.py), so everyone signs in again after a restart. Set it on a host.
    session_secret: str = ""

    # Made-up Napak households, officers and readings (backend/db/seed.py). OFF by
    # default: the app starts empty and officers register real farmers. Only the test
    # suite and scripts/run_evals.py turn it on, each on its own throwaway database.
    seed_demo_data: bool = False

    # Where soil moisture comes from when a plot has no current reading:
    # "open-meteo" = Open-Meteo's modelled soil moisture (3-9 cm) at the village's
    # coordinates (real data, a model estimate, not a probe in the field);
    # "sensor-only" = only readings already stored (tests and evals use this).
    soil_source: str = "open-meteo"
    open_meteo_geocoding_url: str = "https://geocoding-api.open-meteo.com/v1/search"

    # Keep the agent's MCP tool servers running between agent runs (saves several
    # seconds a run). Tests turn it off so each test starts its own servers.
    keep_tool_servers_warm: bool = True
    # Translate drafts and approved advice in a background thread (approval never
    # waits for the translator). Tests turn it off to check the result right away.
    translate_in_background: bool = True

    # Wrong passwords/PINs on the web sign-in pages before that account is locked,
    # and for how long (the USSD PIN has its own settings above).
    web_login_max_attempts: int = 5
    web_login_lock_minutes: int = 15

    @property
    def model_configured(self) -> bool:
        return bool(self.groq_api_key)

    @property
    def translator_configured(self) -> bool:
        return bool(self.sunbird_api_key)


settings = Settings()
