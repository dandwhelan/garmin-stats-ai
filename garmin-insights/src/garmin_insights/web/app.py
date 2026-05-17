"""FastAPI web server — dashboard, per-session chat, and AI scans."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from garmin_insights.agent import HealthAgent
from garmin_insights.config import get_settings
from garmin_insights.web.sessions import SessionManager

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"

# Module-level singletons set up by the lifespan handler
_agent: HealthAgent | None = None
_sessions: SessionManager | None = None

# Throttle the dashboard cache rebuild to at most once per 60 s so the UI
# always reflects fresh fetcher data without hammering SQLite on every poll.
_last_cache_refresh: datetime | None = None
_CACHE_REFRESH_INTERVAL = timedelta(seconds=60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _agent, _sessions
    settings = get_settings()
    logger.info("Initialising health agent...")
    _agent = HealthAgent(settings)
    _sessions = SessionManager(ttl_seconds=3600, max_sessions=200)
    try:
        _agent.ensure_cache_fresh(days=90)
        logger.info("Agent ready.")
    except Exception as e:
        logger.warning("Cache refresh on startup failed: %s", e)
    yield
    if _agent:
        logger.info("Closing agent...")
        _agent.close()


app = FastAPI(
    title="Garmin Health Insights",
    description="Personal health analytics dashboard with AI chat",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


# ------------------------------------------------------------------
# Request models
# ------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


class ResetRequest(BaseModel):
    session_id: str


class ScanRequest(BaseModel):
    focus: str = "general"


# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(content=(_STATIC_DIR / "index.html").read_text(encoding="utf-8"))


def _resolve_user_identity(settings) -> dict[str, str]:
    """Derive a display name from settings: explicit display_name > email local
    part > 'User'. Returned alongside the email for the UI to show both."""
    email = (settings.garminconnect_email or "").strip()
    explicit = (settings.display_name or "").strip()
    if explicit:
        name = explicit
    elif email:
        local = email.split("@", 1)[0]
        # "helen.wadge" -> "Helen Wadge"
        name = " ".join(p.capitalize() for p in local.replace("_", ".").split("."))
    else:
        name = "User"
    return {"name": name, "email": email}


def _last_sync_iso(db_path: str) -> str | None:
    """Return the SQLite DB file's mtime as an ISO 8601 UTC timestamp.

    The fetcher writes to the DB on every successful Garmin pull, so the file
    mtime is a faithful "last synced" indicator for the UI. None if missing.
    """
    try:
        from pathlib import Path
        p = Path(db_path)
        if not p.exists():
            return None
        return datetime.utcfromtimestamp(p.stat().st_mtime).isoformat() + "Z"
    except Exception:
        return None


@app.get("/api/health")
async def health_check():
    if _agent is None:
        raise HTTPException(status_code=503, detail="Agent not initialised")
    try:
        repo_health = _agent._repo.health_check()
        return {
            "status": "ok",
            "user": _resolve_user_identity(_agent._settings),
            "database": repo_health,
            "last_sync": _last_sync_iso(_agent._settings.sqlite_db_path),
            "model": _agent._settings.claude_model,
            "sessions": _sessions.stats() if _sessions else {},
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
    except Exception as e:
        return JSONResponse(status_code=503, content={"status": "error", "detail": str(e)})


@app.get("/api/dashboard")
async def dashboard():
    """Return the last 30 days of daily summaries plus baselines.

    Rebuilds the daily_summaries cache from fresh daily_stats data at most once
    every 60 s so that new fetcher writes appear on the dashboard automatically
    without requiring a web-server restart.
    """
    global _last_cache_refresh
    if _agent is None:
        raise HTTPException(status_code=503, detail="Agent not initialised")

    now = datetime.utcnow()
    if _last_cache_refresh is None or (now - _last_cache_refresh) >= _CACHE_REFRESH_INTERVAL:
        try:
            loop = asyncio.get_event_loop()
            # Rebuild last 7 days only (fast; full history rebuilt on startup)
            await loop.run_in_executor(None, _agent.ensure_cache_fresh, 7)
            _last_cache_refresh = now
        except Exception as e:
            logger.warning("Dashboard cache refresh failed (non-fatal): %s", e)

    end = now.strftime("%Y-%m-%d")
    start = (now - timedelta(days=30)).strftime("%Y-%m-%d")

    try:
        loop = asyncio.get_event_loop()
        summaries = await loop.run_in_executor(
            None, _agent._memory.get_daily_summaries_range, start, end
        )
        baselines = await loop.run_in_executor(None, _agent._memory.get_baselines)
        return {
            "summaries": summaries,
            "baselines": baselines,
            "date_range": {"start": start, "end": end},
        }
    except Exception as e:
        logger.error("Dashboard query failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/chat")
async def chat(body: ChatRequest):
    """Stream a chat response via Server-Sent Events with per-session history."""
    if _agent is None or _sessions is None:
        raise HTTPException(status_code=503, detail="Agent not initialised")

    session_id, history = _sessions.get_or_create(body.session_id)

    async def sse_generator() -> AsyncGenerator[str, None]:
        # Send the session id first so the client can store it
        yield f"data: {json.dumps({'type': 'session', 'session_id': session_id})}\n\n"

        loop = asyncio.get_event_loop()
        gen = _agent.chat_stream(body.message, history=history)
        sentinel = object()

        try:
            while True:
                event = await loop.run_in_executor(None, next, gen, sentinel)
                if event is sentinel:
                    break
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as e:
            logger.exception("Chat stream failed")
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"
        finally:
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        sse_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.post("/api/chat/reset")
async def chat_reset(body: ResetRequest):
    if _sessions is None:
        raise HTTPException(status_code=503, detail="Agent not initialised")
    _sessions.reset(body.session_id)
    return {"reset": True, "session_id": body.session_id}


@app.post("/api/scan")
async def scan(body: ScanRequest):
    """Run a proactive health scan and return the report."""
    if _agent is None:
        raise HTTPException(status_code=503, detail="Agent not initialised")

    valid_focus = {"general", "morning", "midday", "evening", "weekly"}
    if body.focus not in valid_focus:
        raise HTTPException(status_code=400, detail=f"focus must be one of {valid_focus}")

    try:
        loop = asyncio.get_event_loop()
        report = await loop.run_in_executor(
            None, _agent.generate_scan_report, body.focus
        )
        return {"focus": body.focus, "report": report, "timestamp": datetime.utcnow().isoformat()}
    except Exception as e:
        logger.error("Scan failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def run_server():
    import uvicorn
    settings = get_settings()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    uvicorn.run(
        "garmin_insights.web.app:app",
        host=settings.web_host,
        port=settings.web_port,
        reload=False,
    )


if __name__ == "__main__":
    run_server()
