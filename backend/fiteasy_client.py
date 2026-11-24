"""HTTP client for the FitEasy (Laravel) member site.

Reverse-engineered from the live site:
  * Login: POST /mypage/login with _token, emailOrMemberID, password (Laravel CSRF).
  * Schedule: GET /mypage/<slug>?select_shop=<id>&select_week=<n> returns a weekly
    calendar grid: table.p-calendar__table, columns = 7 days, rows = time slots.
  * A cell is:
      - unavailable: td.p-calendar__mark-cell.p-calendar__invalid-cell, text "×"
      - reserved by me: <a class="js-<slug>_cancel ..." data-start_date/-start_time/
        -reservation_id href="/mypage/<slug>/cancel/<id>?..."> text "予"
      - available: td.p-calendar__mark-cell with an <a> (text ◯/△) whose href is a
        reserve link (NOT /cancel/). Date/time taken from the anchor's data-* attrs
        when present, else inferred from the grid position.
"""
from __future__ import annotations

import json
import re
from datetime import date as date_cls
from datetime import datetime
from typing import Callable, Optional
from urllib.parse import parse_qs, urljoin, urlparse
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup

from .config import config
from .models import Slot

JST = ZoneInfo("Asia/Tokyo")

_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")
_YM_RE = re.compile(r"(\d{4})\D+(\d{1,2})")
FULL_MARKS = {"×", "✕", "✗", "満", "―", "-", ""}

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def now_jst() -> datetime:
    return datetime.now(JST).replace(tzinfo=None)


class FitEasyClient:
    def __init__(self, log: Optional[Callable[[str, str], None]] = None) -> None:
        self._log = log or (lambda level, msg: None)
        self.http = httpx.AsyncClient(
            follow_redirects=True,
            timeout=30.0,
            headers={"User-Agent": UA, "Accept-Language": "ja,en;q=0.8"},
        )
        self.logged_in = False
        self.last_html: str = ""

    def log(self, level: str, msg: str) -> None:
        self._log(level, msg)

    async def close(self) -> None:
        await self.http.aclose()

    # ------------------------------------------------------------------ auth
    @staticmethod
    def _token(html: str) -> Optional[str]:
        soup = BeautifulSoup(html, "html.parser")
        inp = soup.find("input", attrs={"name": "_token"})
        if inp and inp.get("value"):
            return inp["value"]
        meta = soup.find("meta", attrs={"name": "csrf-token"})
        if meta and meta.get("content"):
            return meta["content"]
        return None

    @staticmethod
    def _is_login_page(html: str) -> bool:
        return "emailOrMemberID" in html

    async def ensure_login(self) -> bool:
        if self.logged_in:
            return True
        return await self.login()

    async def login(self) -> bool:
        url = f"{config.BASE_URL}/mypage/login"
        r = await self.http.get(url)
        token = self._token(r.text)
        if not token:
            self.log("error", "Could not find CSRF _token on login page")
            return False
        r = await self.http.post(
            url,
            data={
                "_token": token,
                "emailOrMemberID": config.EMAIL,
                "password": config.PASSWORD,
            },
        )
        if self._is_login_page(r.text) or r.url.path.endswith("/login"):
            self.logged_in = False
            self.log("error", "Login failed — check FITEASY_EMAIL / FITEASY_PASSWORD")
            return False
        self.logged_in = True
        self.log("info", "Logged in to FitEasy")
        return True

    # -------------------------------------------------------------- schedule
    async def fetch_week(self, week: int) -> str:
        url = (
            f"{config.BASE_URL}/mypage/{config.SHOP_SLUG}"
            f"?select_shop={config.SHOP_ID}&select_week={week}"
        )
        r = await self.http.get(url)
        if self._is_login_page(r.text):
            self.logged_in = False
            if await self.login():
                r = await self.http.get(url)
        self.last_html = r.text
        return r.text

    async def scan(self, weeks: int) -> tuple[list[Slot], list[Slot]]:
        """Scan `weeks` weeks, returning (available_slots_sorted, my_reservations)."""
        all_slots: dict[str, Slot] = {}
        reservations: dict[str, Slot] = {}
        cancel_ids: dict[str, str] = {}  # reservation_id -> cancel href
        for w in range(max(1, weeks)):
            try:
                html = await self.fetch_week(w)
            except Exception as exc:  # noqa: BLE001
                self.log("error", f"Fetch week {w} failed: {exc}")
                break
            slots, res = self.parse_calendar(html)
            for s in slots:
                all_slots.setdefault(s.slot_key, s)
            for r in res:
                reservations.setdefault(r.slot_key, r)
            # The reservation markup is inconsistent — sometimes only a bare
            # /cancel/<id> link renders (no date/time attrs). Collect every id so
            # we can resolve it from its own cancel page below.
            for m in re.finditer(r"/mypage/[^\"'<> ]*?/cancel/(\d+)", html):
                cancel_ids.setdefault(
                    m.group(1),
                    f"{config.BASE_URL}/mypage/{config.SHOP_SLUG}/cancel/{m.group(1)}",
                )
            if not slots and not res and not cancel_ids and w > 0:
                break  # no more weeks rendered

        # Resolve any reservation id we did not already capture with a time.
        have_ids = {r.reservation_id for r in reservations.values() if r.reservation_id}
        for rid, href in cancel_ids.items():
            if rid in have_ids:
                continue
            slot = await self._reservation_from_cancel_page(href, rid)
            if slot is not None:
                reservations.setdefault(slot.slot_key, slot)

        # De-dupe reservations by (date, time); prefer entries carrying an id.
        res_by_time: dict[tuple, Slot] = {}
        for r in reservations.values():
            tkey = (r.start.month, r.start.day, r.start.hour, r.start.minute)
            prev = res_by_time.get(tkey)
            if prev is None or (not prev.reservation_id and r.reservation_id):
                res_by_time[tkey] = r

        slots_sorted = sorted(all_slots.values(), key=lambda s: s.start)
        res_sorted = sorted(res_by_time.values(), key=lambda s: s.start)
        return slots_sorted, res_sorted

    async def _reservation_from_cancel_page(
        self, href: str, rid: Optional[str] = None
    ) -> Optional[Slot]:
        """GET a /cancel/<id> page (a confirmation page — does not cancel) and
        build the reservation Slot from its authoritative data-* attributes."""
        try:
            page = await self.http.get(href)
            if page.status_code >= 400 or self._is_login_page(page.text):
                return None
            soup = BeautifulSoup(page.text, "html.parser")
            el = soup.find(attrs={"data-start_date": True})
            ds = el.get("data-start_date") if el else None
            ts = el.get("data-start_time") if el else None
            if not (ds and ts):
                return None
            y, mo, dd = (int(x) for x in ds.replace("-", "/").split("/"))
            hh, mm = (int(x) for x in ts.split(":")[:2])
            start = datetime(y, mo, dd, hh, mm)
            return Slot(
                start=start,
                label=f"{start.month}/{start.day} {start:%H:%M}",
                slot_key=f"res-{rid or start.isoformat()}",
                available=False,
                reservation_id=rid,
                cancel_action=href,
                cancel_method="get",
            )
        except Exception:  # noqa: BLE001
            return None

    # ---------------------------------------------------------------- parse
    @staticmethod
    def _column_dates(table, ref: datetime) -> list[date_cls]:
        ym = table.select_one(".p-calendar__year-month")
        base_y, base_m = ref.year, ref.month
        if ym:
            m = _YM_RE.search(ym.get_text(" ", strip=True))
            if m:
                base_y, base_m = int(m.group(1)), int(m.group(2))
        days = [
            int(d.get_text(strip=True))
            for d in table.select(".p-calendar__date")
            if d.get_text(strip=True).isdigit()
        ]
        dates: list[date_cls] = []
        y, mth, prev = base_y, base_m, None
        for d in days:
            if prev is not None and d < prev:
                mth += 1
                if mth > 12:
                    mth, y = 1, y + 1
            try:
                dates.append(date_cls(y, mth, d))
            except ValueError:
                dates.append(ref.date())
            prev = d
        return dates

    @staticmethod
    def _dt_from_anchor(a, fallback: datetime) -> datetime:
        d = a.get("data-start_date")
        t = a.get("data-start_time")
        if d and t:
            d = d.replace("-", "/")
            try:
                parts = [int(x) for x in d.split("/")]
                hh, mm = [int(x) for x in t.split(":")]
                return datetime(parts[0], parts[1], parts[2], hh, mm)
            except Exception:  # noqa: BLE001
                pass
        return fallback

    def parse_calendar(self, html: str) -> tuple[list[Slot], list[Slot]]:
        soup = BeautifulSoup(html, "html.parser")
        ref = now_jst()
        slots: list[Slot] = []
        reservations: list[Slot] = []

        for table in soup.select("table.p-calendar__table"):
            col_dates = self._column_dates(table, ref)
            for tr in table.find_all("tr"):
                time_cell = tr.find("td", class_="p-calendar__time-cell")
                if not time_cell:
                    continue
                tm = _TIME_RE.search(time_cell.get_text(" ", strip=True))
                if not tm:
                    continue
                hh, mm = int(tm.group(1)), int(tm.group(2))
                mark_cells = tr.find_all("td", class_="p-calendar__mark-cell")
                for i, cell in enumerate(mark_cells):
                    if i >= len(col_dates):
                        break
                    day = col_dates[i]
                    grid_dt = datetime(day.year, day.month, day.day, hh, mm)
                    a = cell.find("a")
                    text = cell.get_text(" ", strip=True)
                    href = a.get("href") if a else None
                    is_cancel = bool(a and href and "/cancel/" in href)

                    if is_cancel:
                        start = self._dt_from_anchor(a, grid_dt)
                        rid = a.get("data-reservation_id")
                        slots_key = f"res-{rid or start.isoformat()}"
                        reservations.append(
                            Slot(
                                start=start,
                                label=f"{start.month}/{start.day} {start:%H:%M}",
                                slot_key=slots_key,
                                available=False,
                                reservation_id=rid,
                                room=a.get("data-room"),
                                cancel_action=urljoin(config.BASE_URL, href),
                                cancel_method="get",
                            )
                        )
                        continue

                    # available cell: has a link, not a cancel link, not a full mark
                    bookable = bool(a and href) and text not in FULL_MARKS
                    if not bookable:
                        continue
                    start = self._dt_from_anchor(a, grid_dt)
                    key = start.isoformat()
                    slots.append(
                        Slot(
                            start=start,
                            label=f"{start.month}/{start.day} {start:%H:%M}",
                            slot_key=key,
                            available=True,
                            room=a.get("data-room"),
                            book_url=urljoin(config.BASE_URL, href),
                            book_method="get",
                        )
                    )

        # Robust reservation harvest: a reserved cell can contain several <a>
        # tags (a tooltip link with no data, plus the real "予" link that carries
        # data-start_date/-start_time/-reservation_id). The cell-based pass above
        # may pick the dataless one, so also harvest every /cancel/ anchor that
        # carries full data attributes — these are authoritative.
        for a in soup.select('a[href*="/cancel/"]'):
            ds = a.get("data-start_date")
            ts = a.get("data-start_time")
            if not (ds and ts):
                continue
            try:
                y, mo, dd = (int(x) for x in ds.replace("-", "/").split("/"))
                hh2, mm2 = (int(x) for x in ts.split(":")[:2])
                start = datetime(y, mo, dd, hh2, mm2)
            except Exception:  # noqa: BLE001
                continue
            rid = a.get("data-reservation_id")
            reservations.append(
                Slot(
                    start=start,
                    label=f"{start.month}/{start.day} {start:%H:%M}",
                    slot_key=f"res-{rid or start.isoformat()}",
                    available=False,
                    reservation_id=rid,
                    room=a.get("data-room"),
                    cancel_action=urljoin(config.BASE_URL, a.get("href")),
                    cancel_method="get",
                )
            )

        # de-dupe available slots
        uniq: dict[str, Slot] = {}
        for s in slots:
            uniq.setdefault(s.slot_key, s)

        # de-dupe reservations by (date, time); prefer the entry that carries a
        # reservation_id (the authoritative data-attr anchor).
        res_by_time: dict[tuple, Slot] = {}
        for r in reservations:
            tkey = (r.start.month, r.start.day, r.start.hour, r.start.minute)
            prev = res_by_time.get(tkey)
            if prev is None or (not prev.reservation_id and r.reservation_id):
                res_by_time[tkey] = r
        return list(uniq.values()), list(res_by_time.values())

    # ------------------------------------------------------- act (book/cancel)
    async def _follow_and_confirm(self, first_url: str, confirm_words: list[str]) -> bool:
        r = await self.http.get(first_url)
        if r.status_code >= 400 or self._is_login_page(r.text):
            self.log("error", f"Action GET {first_url} -> HTTP {r.status_code}")
            return False
        # Up to two confirmation steps (e.g. terms agreement -> confirm).
        for _ in range(2):
            soup = BeautifulSoup(r.text, "html.parser")
            form = None
            for f in soup.find_all("form"):
                t = f.get_text(" ", strip=True)
                if any(w in t for w in confirm_words):
                    form = f
                    break
            if not form:
                break
            action = urljoin(str(r.url), form.get("action") or str(r.url))
            method = (form.get("method") or "post").lower()
            payload = self._form_payload(form)
            r = await (
                self.http.post(action, data=payload)
                if method == "post"
                else self.http.get(action, params=payload)
            )
            if r.status_code >= 400:
                self.log("error", f"Confirm step -> HTTP {r.status_code}")
                return False
        self.last_html = r.text
        return True

    @staticmethod
    def _form_payload(form) -> dict:
        data: dict = {}
        for inp in form.find_all("input"):
            name = inp.get("name")
            if not name:
                continue
            itype = (inp.get("type") or "text").lower()
            if itype in ("checkbox", "radio"):
                # auto-agree terms checkboxes
                data[name] = inp.get("value", "on")
                continue
            data[name] = inp.get("value", "")
        for sel in form.find_all("select"):
            name = sel.get("name")
            if not name:
                continue
            opt = sel.find("option", selected=True) or sel.find("option")
            data[name] = opt.get("value", "") if opt else ""
        btn = form.find("button", attrs={"type": "submit"}) or form.find(
            "input", attrs={"type": "submit"}
        )
        if btn and btn.get("name"):
            data[btn["name"]] = btn.get("value", "")
        return data

    async def book(self, slot: Slot) -> bool:
        """Finalize a reservation.

        The slot's book_url carries every value the confirm form needs
        (date, shop_id, start_time, deadline=end, rooms); we GET it only to pick
        up a fresh _token + keep the session, then POST the reservation.
        """
        if not slot.book_url:
            self.log("error", f"No booking URL for {slot.label}")
            return False
        try:
            q = parse_qs(urlparse(slot.book_url).query)

            def g(key: str) -> Optional[str]:
                v = q.get(key)
                return v[0] if v else None

            page = await self.http.get(slot.book_url)
            if page.status_code >= 400 or self._is_login_page(page.text):
                self.log("error", f"Book {slot.label}: confirm page HTTP {page.status_code}")
                return False
            token = self._token(page.text)

            # The confirm page's `.reservation-data` element carries the exact
            # values the site's own JS submits (data-* attrs). Use those as the
            # source of truth, falling back to the book_url query string.
            soup = BeautifulSoup(page.text, "html.parser")
            rd = soup.find(class_="reservation-data")

            def d(attr: str, fallback: Optional[str] = None) -> Optional[str]:
                if rd is not None:
                    val = rd.get(f"data-{attr}")
                    if val:
                        return val
                return fallback

            start_date = d("start_date", g("date")) or ""
            start_time = d("start_time", g("start_time")) or ""
            shop_name = d("shop_name", "") or ""

            rooms_raw = d("rooms", g("rooms"))
            select_room = ""
            if rooms_raw:
                try:
                    select_room = next(iter(json.loads(rooms_raw).keys()))
                except Exception:  # noqa: BLE001
                    pass

            end_time = ""
            deadline = d("deadline", g("deadline"))
            if deadline and " " in deadline:
                end_time = deadline.split(" ", 1)[1][:5]
            if not end_time and start_time:
                dur = int(g("select_minute_per_reservation") or 60)
                hh, mm = (int(x) for x in start_time.split(":"))
                total = hh * 60 + mm + dur
                end_time = f"{total // 60:02d}:{total % 60:02d}"

            # Matches exactly what the browser's JS posts, including shop_name
            # (a required field the earlier version omitted).
            payload = {
                "_token": token or "",
                "reminder": "1",
                "penalty_agreement": "1",
                "shop_id": g("shop_id") or config.SHOP_ID,
                "shop_name": shop_name,
                "start_date": start_date,
                "start_time": start_time,
                "end_time": end_time,
                "select_room": select_room,
            }
            action = f"{config.BASE_URL}/mypage/{config.SHOP_SLUG}/reservation"
            resp = await self.http.post(action, data=payload)
            self.last_html = resp.text
            if resp.status_code >= 400 or self._is_login_page(resp.text):
                self.log("error",
                         f"Book {slot.label} -> HTTP {resp.status_code} rejected")
                return False

            # Ground truth: re-scan and confirm the reservation now exists.
            # (The page template contains the word "エラー" in inert JS, so a
            # string match is not a reliable success/failure signal.)
            confirmed = await self._verify_booked(slot)
            self.log("info" if confirmed else "error",
                     f"Book {slot.label} (room {select_room}) -> "
                     f"HTTP {resp.status_code} "
                     f"{'confirmed' if confirmed else 'not confirmed'}")
            return confirmed
        except Exception as exc:  # noqa: BLE001
            self.log("error", f"Book {slot.label} failed: {exc}")
            return False

    async def _verify_booked(self, slot: Slot) -> bool:
        """Re-scan and return True if a reservation matching `slot` now exists."""
        try:
            today = datetime.now(JST).date()
            days_ahead = (slot.start.date() - today).days
            weeks = max(3, days_ahead // 7 + 2)
            _, reservations = await self.scan(min(weeks, 12))
            want = (slot.start.month, slot.start.day,
                    slot.start.hour, slot.start.minute)
            for r in reservations:
                if (r.start.month, r.start.day,
                        r.start.hour, r.start.minute) == want:
                    return True
            return False
        except Exception as exc:  # noqa: BLE001
            self.log("error", f"Verify {slot.label} failed: {exc}")
            return False

    async def cancel(self, slot: Slot) -> bool:
        if not slot.cancel_action:
            self.log("error", f"No cancel action for {slot.label}")
            return False
        try:
            page = await self.http.get(slot.cancel_action)
            if page.status_code >= 400 or self._is_login_page(page.text):
                self.log("error",
                         f"Cancel {slot.label}: page HTTP {page.status_code}")
                return False
            soup = BeautifulSoup(page.text, "html.parser")
            token = self._token(page.text)
            rd = soup.find(class_="reservation-data") or soup.find(
                attrs={"data-start_date": True})

            def d(attr: str) -> Optional[str]:
                return rd.get(f"data-{attr}") if rd is not None else None

            q = parse_qs(urlparse(slot.cancel_action).query)

            def g(key: str) -> Optional[str]:
                v = q.get(key)
                return v[0] if v else None

            start_date = d("start_date") or g("date") or (
                f"{slot.start.year}/{slot.start.month:02d}/{slot.start.day:02d}")
            start_time = d("start_time") or g("start_time") or f"{slot.start:%H:%M}"
            end_time = g("end_time") or ""
            deadline = d("deadline")
            if not end_time and deadline and " " in deadline:
                end_time = deadline.split(" ", 1)[1][:5]
            if not end_time:
                hh, mm = (int(x) for x in start_time.split(":")[:2])
                total = hh * 60 + mm + 60
                end_time = f"{total // 60:02d}:{total % 60:02d}"

            payload = {
                "_token": token or "",
                "reminder": "1",
                "shop_id": config.SHOP_ID,
                "start_date": start_date,
                "start_time": start_time,
                "end_time": end_time,
            }
            # The cancel form posts back to the cancel URL itself (action="").
            resp = await self.http.post(str(page.url), data=payload)
            self.last_html = resp.text
            if resp.status_code >= 400 or self._is_login_page(resp.text):
                self.log("error",
                         f"Cancel {slot.label} -> HTTP {resp.status_code} failed")
                return False

            gone = await self._verify_gone(slot)
            self.log("info" if gone else "error",
                     f"Cancel {slot.label} -> "
                     f"{'confirmed' if gone else 'not confirmed'}")
            return gone
        except Exception as exc:  # noqa: BLE001
            self.log("error", f"Cancel {slot.label} failed: {exc}")
            return False

    async def _verify_gone(self, slot: Slot) -> bool:
        """Re-scan and return True if the reservation is no longer present."""
        try:
            today = datetime.now(JST).date()
            days_ahead = (slot.start.date() - today).days
            weeks = max(3, days_ahead // 7 + 2)
            _, reservations = await self.scan(min(weeks, 12))
            for r in reservations:
                if slot.reservation_id and r.reservation_id == slot.reservation_id:
                    return False
                if (r.start.month, r.start.day, r.start.hour, r.start.minute) == (
                        slot.start.month, slot.start.day,
                        slot.start.hour, slot.start.minute):
                    return False
            return True
        except Exception as exc:  # noqa: BLE001
            self.log("error", f"Verify-gone {slot.label} failed: {exc}")
            return False

    async def _book_time(self, start: datetime) -> Optional[Slot]:
        """Re-scan and book whatever open slot matches `start`; return it or None."""
        slots, _ = await self.scan(3)
        want = (start.month, start.day, start.hour, start.minute)
        match = next(
            (s for s in slots if (s.start.month, s.start.day,
                                  s.start.hour, s.start.minute) == want),
            None,
        )
        if match is not None and await self.book(match):
            return match
        return None

    async def rebook(self, current: Slot, best: Slot) -> tuple[bool, Optional[Slot]]:
        """Cancel the current reservation then book `best` (FitEasy allows only
        one active reservation, so booking first is impossible). If the new
        booking fails, try to re-grab the original slot so the user is not left
        with nothing. Returns (improved, managed_slot)."""
        if not await self.cancel(current):
            # Could not cancel; original reservation still stands.
            return False, current
        if await self.book(best):
            return True, best
        # New slot slipped away after the cancel — restore the original time.
        restored = await self._book_time(current.start)
        if restored is not None:
            self.log("warn",
                     f"Lost {best.label}; restored original {current.label}")
            return False, restored
        self.log("error",
                 f"Cancelled {current.label} but could not book {best.label} "
                 f"or restore the original slot")
        return False, None
