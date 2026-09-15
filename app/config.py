"""Configuration: environment secrets plus carpenter-editable business rates.

Rates live in config.yaml rather than here so the carpenter (or family) can
change a margin without touching Python.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
AUDIO_DIR = DATA_DIR / "audio"
QUOTES_DIR = DATA_DIR / "quotes"
DB_PATH = DATA_DIR / "kitchen.db"
CATALOG_PATH = DATA_DIR / "catalog.csv"
ASSETS_DIR = ROOT / "assets"
LOGO_PATH = ASSETS_DIR / "logo.png"
CONFIG_PATH = ROOT / "config.yaml"


def _load_dotenv() -> None:
    """Minimal .env loader so we don't add a dependency for five lines."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()


class BusinessConfig(BaseModel):
    """Carpenter-editable rates. Mirrors config.yaml."""

    business_name: str = "Mishel Boutique Kitchens"
    currency: str = "ILS"
    currency_symbol: str = "₪"
    labor_cost_per_hour: float = 50.0
    labor_bill_per_hour: float = 100.0
    margin_pct: float = 50.0
    transport_flat: float = 250.0
    vat_pct: float = 0.0
    payment_terms: str = ""
    quote_valid_days: int = 14

    @property
    def margin_multiplier(self) -> float:
        return 1.0 + self.margin_pct / 100.0


class Settings(BaseModel):
    telegram_bot_token: str = ""
    telegram_webhook_secret: str = "change-me"
    allowed_telegram_user_id: int | None = None
    anthropic_api_key: str = ""
    transcribe_backend: str = "local"
    openai_api_key: str = ""
    whisper_model: str = "large-v3"
    business: BusinessConfig = Field(default_factory=BusinessConfig)

    @property
    def has_anthropic(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def has_telegram(self) -> bool:
        return bool(self.telegram_bot_token)


def _read_business_config() -> BusinessConfig:
    if not CONFIG_PATH.exists():
        return BusinessConfig()
    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    return BusinessConfig(**(raw.get("business") or {}))


@lru_cache
def get_settings() -> Settings:
    allowed = os.environ.get("ALLOWED_TELEGRAM_USER_ID", "").strip()
    return Settings(
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        telegram_webhook_secret=os.environ.get("TELEGRAM_WEBHOOK_SECRET", "change-me"),
        allowed_telegram_user_id=int(allowed) if allowed.isdigit() else None,
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        transcribe_backend=os.environ.get("TRANSCRIBE_BACKEND", "local"),
        openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
        whisper_model=os.environ.get("WHISPER_MODEL", "large-v3"),
        business=_read_business_config(),
    )


def ensure_dirs() -> None:
    for d in (DATA_DIR, AUDIO_DIR, QUOTES_DIR, ASSETS_DIR):
        d.mkdir(parents=True, exist_ok=True)
