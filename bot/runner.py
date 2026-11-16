"""GitHub Actions entrypoint.

Runs one ~RUN_SECONDS session: logs in once, then loops (polling every
~10s, jittered) checking the Narimasu sauna60 calendar and booking/rebooking
per the preferences stored in preferences.json. Status + logs are written to
the bot-state branch for the dashboard. Credentials come from Actions secrets.

The 5-min cron fires a fresh session each time, so polling is continuous while
staying within free public-repo Actions limits.
"""
from __future__ import annotations

import asyncio
import os
import random
import time
from datetime import datetime, time as dtime

from backend.fiteasy_client import FitEasyClient, now_jst
from backend.models import Preferences, Slot
from bot.github_state import GitHubState

RUN_SECONDS = int(os.getenv("RUN_SECONDS", "270"))


def parse_hhmm(value: str, fallback: dtime) -> dtime:
    try:
        h, m = value.split(":")
        return dtime(int(h), int(m))
    except Exception:  # noqa: BLE001
        return fallback


def slot_ok(slot: Slot, prefs: Preferences, now: datetime) -> bool:
    if slot.start <= now:
        return False
    earliest = parse_hhmm(prefs.earliest_time, dtime(0, 0))
    latest = parse_hhmm(prefs.latest_time, dtime(18, 0))
    if not (earliest <= slot.start.time() < latest):
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


class Runner:
    def __init__(self) -> None:
        self.logs: list[dict] = []
        self.client = FitEasyClient(log=self.log)
        self.gh = GitHubState()
        self.state: dict = {}
        self.managed: Slot | None = None
        self.last_action = ""
        self.last_error = None

    def log(self, level: str, message: str) -> None:
        ts = datetime.utcnow().isoformat()
        self.logs.append({"ts": ts, "level": level, "message": message})
        self.logs = self.logs[-200:]
        if level == "error":
            self.last_error = message
        print(f"[{level}] {message}", flush=True)

    async def build_and_write_state(self, prefs: Preferences, slots, reservations) -> None:
        self.state = {
            "logged_in": self.client.logged_in,
            "running": True,
            "updated_at": now_jst().isoformat(),
            "current": self.managed.model_dump(mode="json") if self.managed else None,
            "detected": [s.model_dump(mode="json") for s in slots[:40]],
            "reservations": [r.model_dump(mode="json") for r in reservations],
            "last_check": now_jst().isoformat(),
            "last_action": self.last_action,
            "last_error": self.last_error,
            "preferences": prefs.model_dump(mode="json"),
            "logs": self.logs[-120:],
        }
        if self.managed:
            # remember managed booking between runs
            self.state["managed"] = self.managed.model_dump(mode="json")
        try:
            await self.gh.write_state(self.state)
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] state write failed: {exc}", flush=True)

    async def cycle(self, prefs: Preferences) -> tuple[list, list]:
        await self.client.ensure_login()
        slots, reservations = await self.client.scan(prefs.weeks_to_scan)
        now = now_jst()

        # sync managed booking
        if self.managed is not None and self.managed.reservation_id:
            match = next(
                (r for r in reservations if r.reservation_id == self.managed.reservation_id),
                None,
            )
            self.managed = match  # None if it disappeared
        elif self.managed is not None:
            match = next((r for r in reservations if r.start == self.managed.start), None)
            if match is not None:
                self.managed = match
        current = self.managed
        if current is None and prefs.manage_existing:
            accept_res = [r for r in reservations if slot_ok(r, prefs, now)]
            if accept_res:
                current = min(accept_res, key=lambda s: s.start)

        acceptable = [s for s in slots if slot_ok(s, prefs, now)]
        best = acceptable[0] if acceptable else None

        if best is None:
            self.last_action = (
                f"No acceptable slot (scanned {len(slots)} open, "
                f"{len(reservations)} reservation(s))"
            )
            return slots, reservations

        if current is None:
            await self._book(best, prefs, "initial")
        else:
            improvement = (current.start - best.start).total_seconds() / 60.0
            if best.start < current.start and improvement >= prefs.min_improvement_minutes:
                await self._rebook(current, best, prefs)
            else:
                self.last_action = f"Holding {current.label}; best open {best.label}"
        return slots, reservations

    async def _book(self, slot: Slot, prefs: Preferences, reason: str) -> None:
        if prefs.dry_run:
            self.last_action = f"[DRY RUN] would book {slot.label} ({reason})"
            self.log("info", self.last_action)
            return
        if await self.client.book(slot):
            self.managed = slot
            self.last_action = f"Booked {slot.label} ({reason})"
        else:
            self.last_action = f"Booking {slot.label} failed"

    async def _rebook(self, current: Slot, best: Slot, prefs: Preferences) -> None:
        msg = f"{current.label} -> {best.label}"
        if prefs.dry_run:
            self.last_action = f"[DRY RUN] would rebook {msg}"
            self.log("info", self.last_action)
            return
        if prefs.rebook_strategy == "cancel_then_book":
            if await self.client.cancel(current):
                if await self.client.book(best):
                    self.managed = best
                    self.last_action = f"Rebooked {msg}"
                else:
                    self.managed = None
                    self.last_action = f"Cancelled old but new booking failed ({msg})"
            else:
                self.last_action = f"Rebook aborted (cancel failed) {msg}"
        else:  # book_then_cancel
            if await self.client.book(best):
                await self.client.cancel(current)
                self.managed = best
                self.last_action = f"Rebooked {msg}"
            else:
                self.last_action = f"Rebook aborted (new booking failed) {msg}"

    async def run(self) -> None:
        await self.gh.ensure_state_branch()
        prev = await self.gh.read_state()
        if prev.get("managed"):
            try:
                self.managed = Slot.model_validate(prev["managed"])
            except Exception:  # noqa: BLE001
                self.managed = None

        start = time.time()
        last_write = 0.0
        while time.time() - start < RUN_SECONDS:
            try:
                prefs = Preferences.model_validate(await self.gh.read_prefs())
            except Exception as exc:  # noqa: BLE001
                prefs = Preferences()
                self.log("error", f"Bad preferences.json, using defaults: {exc}")

            slots: list = []
            reservations: list = []
            if prefs.enabled:
                try:
                    slots, reservations = await self.cycle(prefs)
                except Exception as exc:  # noqa: BLE001
                    self.log("error", f"Cycle error: {exc}")
            else:
                self.last_action = "Paused (enabled = false)"

            if time.time() - last_write > 45:
                await self.build_and_write_state(prefs, slots, reservations)
                last_write = time.time()

            delay = random.uniform(
                max(2.0, prefs.poll_min_seconds), max(2.5, prefs.poll_max_seconds)
            )
            await asyncio.sleep(delay)

        # final state flush
        try:
            prefs = Preferences.model_validate(await self.gh.read_prefs())
        except Exception:  # noqa: BLE001
            prefs = Preferences()
        await self.build_and_write_state(prefs, slots, reservations)
        await self.client.close()
        await self.gh.close()


if __name__ == "__main__":
    asyncio.run(Runner().run())
