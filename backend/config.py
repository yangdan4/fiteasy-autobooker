"""Environment-backed configuration (secrets + static defaults).

Runtime-editable *preferences* live in the DB (see store.py); this module only
holds things that come from the environment: credentials, the target shop, the
dashboard token, and the seed values used the first time the DB is created.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


class Config:
    # --- target site / credentials ---
    BASE_URL: str = os.getenv("FITEASY_BASE_URL", "https://join.fiteasy.jp").rstrip("/")
    EMAIL: str = os.getenv("FITEASY_EMAIL", "")
    PASSWORD: str = os.getenv("FITEASY_PASSWORD", "")
    SHOP_SLUG: str = os.getenv("FITEASY_SHOP_SLUG", "sauna60-room")
    SHOP_ID: str = os.getenv("FITEASY_SHOP_ID", "230")

    # --- dashboard auth ---
    DASHBOARD_TOKEN: str = os.getenv("DASHBOARD_TOKEN", "change-me")

    # --- storage ---
    DB_PATH: str = os.getenv("DB_PATH", "data/fiteasy.db")
    TZ: str = os.getenv("TZ", "Asia/Tokyo")

    # --- selector overrides (optional) ---
    SLOT_FORM_SELECTOR: str = os.getenv("SLOT_FORM_SELECTOR", "").strip()
    CANCEL_FORM_SELECTOR: str = os.getenv("CANCEL_FORM_SELECTOR", "").strip()

    # --- seed defaults for first-run preferences ---
    DEFAULT_LATEST_TIME: str = os.getenv("DEFAULT_LATEST_TIME", "18:00")
    DEFAULT_EARLIEST_TIME: str = os.getenv("DEFAULT_EARLIEST_TIME", "00:00")
    DEFAULT_DRY_RUN: bool = _bool(os.getenv("DEFAULT_DRY_RUN"), True)
    DEFAULT_ENABLED: bool = _bool(os.getenv("DEFAULT_ENABLED"), True)


config = Config()
