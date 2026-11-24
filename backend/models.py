"""Pydantic models shared across the engine, store and API."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class Preferences(BaseModel):
    """User-editable booking rules (persisted, changeable from the dashboard)."""

    enabled: bool = True              # master on/off for the polling loop
    dry_run: bool = True             # if True: detect + log, but never book/cancel
    shop_id: str = "230"

    # Only book slots whose START time is in [earliest_time, latest_time).
    earliest_time: str = "00:00"      # HH:MM (JST)
    latest_time: str = "18:00"        # HH:MM (JST) -> "any time before 6pm"

    # Optional date window (inclusive). None = no bound.
    date_from: Optional[str] = None   # YYYY-MM-DD
    date_to: Optional[str] = None     # YYYY-MM-DD

    # Weekdays allowed: 0=Mon .. 6=Sun.
    allowed_weekdays: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4, 5, 6])

    # Only rebook to an earlier slot if it's earlier by at least this many minutes.
    min_improvement_minutes: int = 10

    # How many calendar weeks ahead to scan each cycle (earliest acceptable wins).
    weeks_to_scan: int = 3

    # If True (default), the bot treats your earliest existing reservation as its
    # baseline and will rebook it earlier. FitEasy allows only ONE active
    # reservation, so this must be True for the bot to do anything once you hold a
    # booking. If False, the bot never touches reservations it did not create.
    manage_existing: bool = True

    # Poll cadence (seconds) — a random value in [min, max] is used each cycle.
    poll_min_seconds: float = 8.0
    poll_max_seconds: float = 12.0

    # FitEasy permits only one active reservation, so rebooking is always
    # cancel-then-book (the client restores the original slot if the new booking
    # fails). "book_then_cancel" is not possible on this site.
    rebook_strategy: str = "cancel_then_book"


class Slot(BaseModel):
    """A bookable (or booked) time slot parsed from the schedule calendar."""

    start: datetime                   # naive local (JST) start time
    end: Optional[datetime] = None
    label: str = ""                   # human label, e.g. "11/18 (火) 10:00"
    slot_key: str                     # stable identity for dedupe/compare
    available: bool = True
    room: Optional[str] = None
    reservation_id: Optional[str] = None

    # How to book it:
    book_url: Optional[str] = None            # GET/anchor href
    book_action: Optional[str] = None         # form action URL (fallback)
    book_method: str = "get"
    book_payload: dict = Field(default_factory=dict)

    # How to cancel it (populated only for existing reservations):
    cancel_action: Optional[str] = None
    cancel_method: str = "get"
    cancel_payload: dict = Field(default_factory=dict)


class BookingState(BaseModel):
    """Live status surfaced to the dashboard."""

    logged_in: bool = False
    running: bool = False
    current: Optional[Slot] = None
    detected: list[Slot] = Field(default_factory=list)
    reservations: list[Slot] = Field(default_factory=list)
    last_check: Optional[str] = None
    next_check: Optional[str] = None
    last_action: Optional[str] = None
    last_error: Optional[str] = None


class LogEntry(BaseModel):
    ts: str
    level: str
    message: str
