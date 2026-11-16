# FitEasy Auto-Booker ♨

An always-on bot that logs into your FitEasy member account, targets the
**Narimasu (成増) Sauna60 room (shop 230)**, and books the **earliest slot that
fits your rules** (default: any start time **before 18:00**). It re-checks the
calendar continuously and, if an earlier acceptable slot opens up, **rebooks** to
it automatically. A web dashboard lets you change the rules live.

Lightweight (plain HTTP — FitEasy is a server-rendered Laravel site, no headless
browser needed).

## Two ways to run it

1. **GitHub-native — 100% free, no credit card, no server (recommended).**
   The booking engine runs as a **GitHub Actions** cron job; the dashboard is a
   **GitHub Pages** site. Nothing to host, nothing to pay. See
   [Free deploy on GitHub](#free-deploy-on-github-actions--pages) below.
2. **Self-hosted FastAPI app** — one container you can run locally or on any
   host (Render/Koyeb/Fly/your own box). See [Self-hosted](#self-hosted-fastapi).

---

## Free deploy on GitHub (Actions + Pages)

This is already set up in this repo. The pieces:

| Piece | File | What it does |
|---|---|---|
| Booking engine | `bot/runner.py` | logs in, scans the calendar, books/rebooks |
| Schedule | `.github/workflows/bot.yml` | runs every 5 min; each run polls ~10s for ~4.5 min |
| Preferences | `preferences.json` | your rules (edit from the dashboard or on GitHub) |
| Live status | `bot-state` branch → `state.json` | written by the bot, read by the dashboard |
| Dashboard | `docs/` → GitHub Pages | view status + edit preferences |

**Why it's continuous & free:** public repos get **unlimited** standard Actions
minutes. A 5-min cron is GitHub's minimum, so each run keeps a login session open
and polls every ~10s for ~4.5 minutes; `concurrency` lets the next run queue so
coverage is near-continuous. The bot's own state commits keep the repo active so
the schedule is never auto-disabled (GitHub disables cron after 60 days idle).

**Setup (already done here, for reference):**
```bash
gh repo create fiteasy-autobooker --public --source=. --push
gh secret set FITEASY_EMAIL   --body 'you@example.com'
gh secret set FITEASY_PASSWORD --body 'your-password'
gh api -X POST repos/<owner>/fiteasy-autobooker/pages \
   -f 'source[branch]=main' -f 'source[path]=/docs'
gh workflow run bot.yml
```

**Going LIVE:** it ships in **dry-run** (detects + logs, books nothing) because a
real reservation carries FitEasy's no-show penalty. To arm real booking, set
`"dry_run": false` in `preferences.json` — either with the **Edit on GitHub**
button on the dashboard (uses your normal login) or by toggling it in the
dashboard after pasting a fine-grained GitHub token (Contents: write).

**Note on cadence:** GitHub cron can be delayed under heavy platform load, so in
practice checks are "every few minutes, continuous" rather than a hard 10s SLA.
That's the trade-off for zero-cost, zero-card, always-on hosting.

---

## How it works

```
Browser ── dashboard ──▶ FastAPI ──┬─ background loop (every ~10s ± jitter)
                                    │     1. log in (session cookie + _token)
                                    │     2. GET /mypage/sauna60-room?select_shop=230
                                    │     3. parse available slots + current booking
                                    │     4. pick earliest slot matching your prefs
                                    │     5. book it — or rebook if it beats current
                                    └─ SQLite (prefs, current booking, logs)
```

**Rebook logic:** each cycle it finds the earliest slot that satisfies your rules.
If you have no booking → it books. If an acceptable slot is earlier than your
current booking by at least *min improvement* minutes → it rebooks (strategy
configurable: `cancel_then_book` assumes one active reservation; `book_then_cancel`
is safer if multiple are allowed).

---

## Self-hosted FastAPI

Run the same engine as one process with a live (real-time) dashboard:

```bash
cd fiteasy
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# .env already contains your credentials; edit DASHBOARD_TOKEN before deploying
uvicorn backend.app:app --reload
```

Open http://localhost:8000 , enter your `DASHBOARD_TOKEN`, and you'll see the
dashboard. It starts in **dry-run** mode: it detects and logs what it *would*
book without touching your account. Confirm the detected slots look right, then
press **Go LIVE** to let it book for real.

---

## Configuration

Secrets & target live in `.env` (see `.env.example`):

| Variable | Meaning |
|---|---|
| `FITEASY_EMAIL` / `FITEASY_PASSWORD` | your FitEasy login (used server-side only) |
| `FITEASY_SHOP_SLUG` / `FITEASY_SHOP_ID` | `sauna60-room` / `230` (Narimasu) |
| `DASHBOARD_TOKEN` | password for the dashboard — **set a long random value** |
| `DEFAULT_LATEST_TIME` | seed for "book before" (default `18:00`) |
| `DEFAULT_DRY_RUN` | start in dry-run (default `true`) |

Everything else — latest/earliest time, date window, allowed weekdays, min
improvement, poll cadence, rebook strategy, pause, dry-run — is editable live in
the dashboard and persisted in SQLite.

---

## Deploy to a free cloud (get a link)

### Fly.io — recommended (always-on, Tokyo region)
```bash
fly launch --no-deploy --copy-config --name fiteasy-autobooker --region nrt
fly secrets set FITEASY_EMAIL='you@example.com' \
                FITEASY_PASSWORD='your-password' \
                DASHBOARD_TOKEN='a-long-random-string'
fly deploy
```
Your link: `https://fiteasy-autobooker.fly.dev`

### Koyeb — Docker, also stays on
Create a service from this repo (Dockerfile autodetected), add the same env
vars, deploy. Link: `https://<name>-<org>.koyeb.app`.

### Render — works but free tier sleeps
Use `render.yaml` (Blueprint). The free web service spins down after ~15 min of
no traffic, which pauses polling. Keep it awake free by pointing
[UptimeRobot](https://uptimerobot.com) at `https://<app>/healthz` every 5 min, or
use the paid Starter plan.

> Any Docker host works — the image is tiny (~150 MB) and uses ~80 MB RAM.

---

## The one part that may need a 2-minute tune

Login and shop-select were mapped from the real site. The **authenticated
schedule/booking markup** is behind your login, so the slot parser
(`backend/fiteasy_client.py` → `parse_schedule`) is heuristic. If the dashboard
shows **0 detected slots** while slots clearly exist:

1. Click **"raw page ↗"** in the Activity log card (dumps the fetched schedule HTML).
2. Find how a bookable slot is represented (a `<form>` or `<a>` with the time).
3. Either set `SLOT_FORM_SELECTOR` / `CANCEL_FORM_SELECTOR` (CSS) in your env, or
   adjust the `RESERVE_WORDS` / `FULL_WORDS` / `BOOKED_WORDS` lists at the top of
   `fiteasy_client.py`.

Keep **dry-run on** until detected slots look correct — then go live.

---

## Safety & notes

- Credentials never reach the browser; they're server-side env vars only.
- The dashboard is gated by `DASHBOARD_TOKEN` — anyone with the link **and** token
  can change your bookings, so keep the token private.
- Polling adds random jitter and backs off on errors to stay gentle on FitEasy.
- This automates **your own** account for your own convenience. Make sure it's
  consistent with FitEasy's terms of use.
