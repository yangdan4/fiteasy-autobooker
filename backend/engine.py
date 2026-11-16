"""Background booking engine: polls the schedule and books/rebooks per prefs."""
from __future__ import annotations

import asyncio
import random
from datetime import datetime, time
from typing import Optional

from . import store
from .fiteasy_client import FitEasyClient, now_jst
from .models import BookingState, Preferences, Slot


def _parse_hhmm(value: str, fallback: time) -> time:
    try:
        h, m = value.split(":")
        return time(int(h), int(m))
    except Exception:  # noqa: BLE001
        return fallback


class Engine:
    def __init__(self) -> None:
        self.state = BookingState()
        self.client = FitEasyClient(log=self._log)
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    # ---------------------------------------------------------------- logging
    def _log(self, level: str, message: str) -> None:
        store.add_log(level, message)
        if level == "error":
            self.state.last_error = message

    # ---------------------------------------------------------------- filter
    @staticmethod
    def slot_ok(slot: Slot, prefs: Preferences, now: datetime) -> bool:
        if slot.start <= now:
            return False
        earliest = _parse_hhmm(prefs.earliest_time, time(0, 0))
        latest = _parse_hhmm(prefs.latest_time, time(18, 0))
        t = slot.start.time()
        if not (earliest <= t < latest):
            return False
        if slot.start.weekday() not in prefs.allowed_weekdays:
            return False
        d = slot.start.date()
        if prefs.date_from:
            try:
                if d < datetime.strptime(prefs.date_from, "%Y-%m-%d").date():
                    return False
            except ValueError:
                pass
        if prefs.date_to:
            try:
                if d > datetime.strptime(prefs.date_to, "%Y-%m-%d").date():
                    return False
            except ValueError:
                pass
        return True

    # ------------------------------------------------------------------ cycle
    async def _cycle(self) -> None:
        prefs = store.get_preferences()
        if not await self.client.ensure_login():
            self.state.logged_in = False
            return
        self.state.logged_in = True

        slots, reservations = await self.client.scan(prefs.weeks_to_scan)
        self.state.detected = slots[:60]
        self.state.reservations = reservations
        now = now_jst()
        self.state.last_check = now.isoformat()

        # Resolve the booking the bot is responsible for.
        stored = store.get_current_booking()
        current = None
        if stored is not None:
            if stored.reservation_id:
                match = next(
                    (r for r in reservations if r.reservation_id == stored.reservation_id),
                    None,
                )
                if match is None:
                    # our managed booking disappeared (cancelled elsewhere) -> forget it
                    store.save_current_booking(None)
                else:
                    current = match
            else:
                # booked by the bot but not yet linked to a reservation id;
                # match it to the live reservation by start time and adopt it.
                match = next(
                    (r for r in reservations if r.start == stored.start), None
                )
                if match is not None:
                    current = match
                    store.save_current_booking(match)
                else:
                    current = stored
        if current is None and prefs.manage_existing:
            accept_res = [r for r in reservations if self.slot_ok(r, prefs, now)]
            if accept_res:
                current = min(accept_res, key=lambda s: s.start)
        self.state.current = current

        acceptable = [s for s in slots if self.slot_ok(s, prefs, now)]
        best = acceptable[0] if acceptable else None  # slots are sorted ascending

        if best is None:
            self.state.last_action = (
                f"No acceptable available slot "
                f"(scanned {len(slots)} open, {len(reservations)} existing reservation(s))"
            )
            return

        if current is None:
            await self._book(best, prefs, reason="initial")
            return

        improvement_min = (current.start - best.start).total_seconds() / 60.0
        if best.start < current.start and improvement_min >= prefs.min_improvement_minutes:
            await self._rebook(current, best, prefs)
        else:
            self.state.last_action = (
                f"Holding {current.label}; best available is {best.label}"
            )

    async def _book(self, slot: Slot, prefs: Preferences, reason: str) -> None:
        if prefs.dry_run:
            self.state.last_action = f"[DRY RUN] would book {slot.label} ({reason})"
            self._log("info", self.state.last_action)
            return
        if await self.client.book(slot):
            store.save_current_booking(slot)
            self.state.current = slot
            self.state.last_action = f"Booked {slot.label} ({reason})"
        else:
            self.state.last_action = f"Booking {slot.label} failed"

    async def _rebook(self, current: Slot, best: Slot, prefs: Preferences) -> None:
        msg = f"{current.label} -> {best.label}"
        if prefs.dry_run:
            self.state.last_action = f"[DRY RUN] would rebook {msg}"
            self._log("info", self.state.last_action)
            return

        if prefs.rebook_strategy == "book_then_cancel":
            if await self.client.book(best):
                await self.client.cancel(current)
                store.save_current_booking(best)
                self.state.current = best
                self.state.last_action = f"Rebooked {msg}"
            else:
                self.state.last_action = f"Rebook aborted (new slot booking failed) {msg}"
        else:  # cancel_then_book (assumes only one active reservation)
            if await self.client.cancel(current):
                if await self.client.book(best):
                    store.save_current_booking(best)
                    self.state.current = best
                    self.state.last_action = f"Rebooked {msg}"
                else:
                    store.save_current_booking(None)
                    self.state.current = None
                    self.state.last_action = (
                        f"Cancelled {current.label} but new booking failed — will retry"
                    )
            else:
                self.state.last_action = f"Rebook aborted (cancel failed) {msg}"

    # ---------------------------------------------------------------- runloop
    async def _run(self) -> None:
        self.state.running = True
        self._log("info", "Engine started")
        while not self._stop.is_set():
            prefs = store.get_preferences()
            if prefs.enabled:
                try:
                    await self._cycle()
                except Exception as exc:  # noqa: BLE001
                    self._log("error", f"Cycle error: {exc}")
            else:
                self.state.last_action = "Paused (enabled = false)"
            delay = random.uniform(
                max(2.0, prefs.poll_min_seconds), max(2.5, prefs.poll_max_seconds)
            )
            self.state.next_check = now_jst().isoformat()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
        self.state.running = False
        self._log("info", "Engine stopped")

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run())

    async def shutdown(self) -> None:
        self._stop.set()
        if self._task:
            await self._task
        await self.client.close()


engine = Engine()
