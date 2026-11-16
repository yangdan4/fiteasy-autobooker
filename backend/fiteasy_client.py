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
            if not slots and not res and w > 0:
                break  # no more weeks rendered
        slots_sorted = sorted(all_slots.values(), key=lambda s: s.start)
        res_sorted = sorted(reservations.values(), key=lambda s: s.start)
        return slots_sorted, res_sorted

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

        # de-dupe
        uniq: dict[str, Slot] = {}
        for s in slots:
            uniq.setdefault(s.slot_key, s)
        return list(uniq.values()), reservations

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

            select_room = ""
            if g("rooms"):
                try:
                    select_room = next(iter(json.loads(g("rooms")).keys()))
                except Exception:  # noqa: BLE001
                    pass

            start_time = g("start_time") or ""
            end_time = ""
            deadline = g("deadline")
            if deadline and " " in deadline:
                end_time = deadline.split(" ", 1)[1][:5]
            if not end_time and start_time:
                dur = int(g("select_minute_per_reservation") or 60)
                hh, mm = (int(x) for x in start_time.split(":"))
                total = hh * 60 + mm + dur
                end_time = f"{total // 60:02d}:{total % 60:02d}"

            payload = {
                "_token": token or "",
                "reminder": "1",
                "penalty_agreement": "1",
                "shop_id": g("shop_id") or config.SHOP_ID,
                "start_date": g("date") or "",
                "start_time": start_time,
                "end_time": end_time,
                "select_room": select_room,
            }
            action = f"{config.BASE_URL}/mypage/{config.SHOP_SLUG}/reservation"
            resp = await self.http.post(action, data=payload)
            self.last_html = resp.text
            ok = resp.status_code < 400 and not self._is_login_page(resp.text)
            if ok and ("エラー" in resp.text or "失敗" in resp.text):
                ok = False
            self.log("info" if ok else "error",
                     f"Book {slot.label} (room {select_room}) -> "
                     f"HTTP {resp.status_code} {'ok' if ok else 'rejected'}")
            return ok
        except Exception as exc:  # noqa: BLE001
            self.log("error", f"Book {slot.label} failed: {exc}")
            return False

    async def cancel(self, slot: Slot) -> bool:
        if not slot.cancel_action:
            self.log("error", f"No cancel action for {slot.label}")
            return False
        try:
            ok = await self._follow_and_confirm(
                slot.cancel_action, ["キャンセル", "取消", "取り消", "確定", "確認", "はい"]
            )
            self.log("info" if ok else "error",
                     f"Cancel {slot.label} -> {'ok' if ok else 'failed'}")
            return ok
        except Exception as exc:  # noqa: BLE001
            self.log("error", f"Cancel {slot.label} failed: {exc}")
            return False
