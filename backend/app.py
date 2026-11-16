"""FastAPI app: REST API + dashboard hosting + engine lifecycle."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import store
from .config import config
from .engine import engine
from .models import Preferences

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.init_db()
    engine.start()
    try:
        yield
    finally:
        await engine.shutdown()


app = FastAPI(title="FitEasy Auto-Booker", lifespan=lifespan)


# ------------------------------------------------------------------ auth
def require_token(x_dashboard_token: str | None = Header(default=None)) -> None:
    if not config.DASHBOARD_TOKEN:
        return
    if x_dashboard_token != config.DASHBOARD_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing dashboard token")


# ------------------------------------------------------------------ pages
@app.get("/", response_class=HTMLResponse)
async def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/healthz", response_class=PlainTextResponse)
async def healthz() -> str:
    return "ok"


# ------------------------------------------------------------------ api
@app.get("/api/status")
async def api_status(_: None = Depends(require_token)) -> JSONResponse:
    prefs = store.get_preferences()
    return JSONResponse(
        {
            "state": engine.state.model_dump(mode="json"),
            "preferences": prefs.model_dump(mode="json"),
            "shop": {"slug": config.SHOP_SLUG, "id": config.SHOP_ID},
        }
    )


@app.get("/api/logs")
async def api_logs(_: None = Depends(require_token), limit: int = 200) -> JSONResponse:
    return JSONResponse({"logs": store.get_logs(limit)})


@app.post("/api/preferences")
async def api_set_preferences(request: Request, _: None = Depends(require_token)) -> JSONResponse:
    body = await request.json()
    current = store.get_preferences().model_dump()
    for key, value in body.items():
        if key in current:
            current[key] = value
    try:
        prefs = Preferences.model_validate(current)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))
    store.save_preferences(prefs)
    store.add_log("info", "Preferences updated via dashboard")
    return JSONResponse({"ok": True, "preferences": prefs.model_dump(mode="json")})


@app.post("/api/clear-booking")
async def api_clear_booking(_: None = Depends(require_token)) -> JSONResponse:
    """Forget the locally-tracked current booking (does not cancel on FitEasy)."""
    store.save_current_booking(None)
    engine.state.current = None
    store.add_log("info", "Cleared locally-tracked booking")
    return JSONResponse({"ok": True})


@app.get("/api/debug/last-html", response_class=PlainTextResponse)
async def api_last_html(_: None = Depends(require_token)) -> str:
    return engine.client.last_html or "(no page fetched yet)"


# serve static assets (app.js, styles.css)
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "backend.app:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        reload=False,
    )
