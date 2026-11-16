"""SQLite persistence for preferences, the current booking, and a log ring."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional

from .config import config
from .models import Preferences, Slot

_LOCK = threading.Lock()


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(config.DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS logs ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, level TEXT, message TEXT)"
        )
        conn.commit()
    # seed preferences if absent
    if get_preferences(_raw=True) is None:
        prefs = Preferences(
            enabled=config.DEFAULT_ENABLED,
            dry_run=config.DEFAULT_DRY_RUN,
            shop_id=config.SHOP_ID,
            earliest_time=config.DEFAULT_EARLIEST_TIME,
            latest_time=config.DEFAULT_LATEST_TIME,
        )
        save_preferences(prefs)


def _kv_get(key: str) -> Optional[str]:
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None


def _kv_set(key: str, value: str) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        conn.commit()


# ---- preferences ----

def get_preferences(_raw: bool = False) -> Optional[Preferences]:
    raw = _kv_get("preferences")
    if raw is None:
        return None if _raw else Preferences()
    return Preferences.model_validate_json(raw)


def save_preferences(prefs: Preferences) -> None:
    _kv_set("preferences", prefs.model_dump_json())


# ---- current booking (survives restarts) ----

def get_current_booking() -> Optional[Slot]:
    raw = _kv_get("current_booking")
    if not raw:
        return None
    try:
        return Slot.model_validate_json(raw)
    except Exception:
        return None


def save_current_booking(slot: Optional[Slot]) -> None:
    _kv_set("current_booking", slot.model_dump_json() if slot else "")


# ---- logs ----

def add_log(level: str, message: str, keep: int = 500) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO logs(ts, level, message) VALUES(?, ?, ?)",
            (ts, level, message),
        )
        conn.execute(
            "DELETE FROM logs WHERE id NOT IN "
            "(SELECT id FROM logs ORDER BY id DESC LIMIT ?)",
            (keep,),
        )
        conn.commit()


def get_logs(limit: int = 200) -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT ts, level, message FROM logs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
