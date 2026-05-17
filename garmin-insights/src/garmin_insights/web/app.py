"""FastAPI web server — dashboard, per-session chat, and AI scans.

Multi-user aware: each request specifies a `user` parameter selecting which
user's database to query. Agent and visualization service instances are
pooled per-user inside UserContext.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from garmin_insights.config import get_settings
from garmin_insights.web.sessions import SessionManager
from garmin_insights.web.user_context import UserBundle, UserContext

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"

_users: UserContext | None = None
_sessions: SessionManager | None = None

# Throttle the dashboard cache rebuild to at most once per 60 s so the UI
# always reflects fresh fetcher data without hammering SQLite on every poll.
_last_cache_refresh: datetime | None = None
_CACHE_REFRESH_INTERVAL = timedelta(seconds=60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _users, _sessions
    settings = get_settings()
    _users = UserContext(settings)
    _sessions = SessionManager(ttl_seconds=3600, max_sessions=200)
    logger.info("Configured users: %s", _users.user_ids)
    # Eagerly warm up the first user so the dashboard isn't cold on first load
    if _users.user_ids:
        try:
            _users.get(_users.user_ids[0])
        except Exception as e:
            logger.warning("Failed to warm up first user: %s", e)
    yield
    if _users:
        logger.info("Closing user agents...")
        _users.close()


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
    user: str = "default"


class ResetRequest(BaseModel):
    session_id: str


class ScanRequest(BaseModel):
    focus: str = "general"
    start_date: str | None = None
    end_date: str | None = None
    user: str = "default"


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _require_user(user: str) -> UserBundle:
    if _users is None:
        raise HTTPException(status_code=503, detail="Server not initialised")
    if not _users.has_user(user):
        raise HTTPException(
            status_code=404,
            detail=f"Unknown user '{user}'. Configured users: {_users.user_ids}",
        )
    try:
        return _users.get(user)
    except Exception as e:
        logger.exception("Failed to load user bundle")
        raise HTTPException(status_code=500, detail=f"Failed to load user: {e}")


def _resolve_range(start: str | None, end: str | None, default_days: int = 30) -> tuple[str, str]:
    end = end or datetime.utcnow().strftime("%Y-%m-%d")
    start = start or (datetime.utcnow() - timedelta(days=default_days)).strftime("%Y-%m-%d")
    return start, end


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
async def health_check(user: str = Query(default="default")):
    if _users is None:
        raise HTTPException(status_code=503, detail="Server not initialised")
    if not _users.has_user(user):
        raise HTTPException(status_code=404, detail=f"Unknown user '{user}'")
    try:
        bundle = _users.get(user)
        repo_health = bundle.agent._repo.health_check()
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
            None, bundle.agent._memory.get_daily_summaries_range, start, end
        )
        baselines = await loop.run_in_executor(None, bundle.agent._memory.get_baselines)
        return {
            "user": user,
            "summaries": summaries,
            "baselines": baselines,
            "date_range": {"start": start, "end": end},
        }
    except Exception as e:
        logger.error("Dashboard query failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/visualizations")
async def visualizations(
    user: str = Query(default="default"),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
):
    bundle = _require_user(user)
    start, end = _resolve_range(start, end, default_days=30)
    loop = asyncio.get_event_loop()
    viz = bundle.viz
    try:
        training, body_comp, hr_zones, behavior, correlations, sleep_tl, anomalies = await asyncio.gather(
            loop.run_in_executor(None, viz.training, start, end),
            loop.run_in_executor(None, viz.body_composition, start, end),
            loop.run_in_executor(None, viz.hr_zones, start, end),
            loop.run_in_executor(None, viz.behavior_impact, 90, 3),
            loop.run_in_executor(None, viz.correlations, start, end),
            loop.run_in_executor(None, viz.sleep_timeline, start, end),
            loop.run_in_executor(None, viz.anomaly_calendar, start, end),
        )
        return {
            "user": user,
            "date_range": {"start": start, "end": end},
            "training": training,
            "body_composition": body_comp,
            "hr_zones": hr_zones,
            "behavior_impact": behavior,
            "correlations": correlations,
            "sleep_timeline": sleep_tl,
            "anomaly_calendar": anomalies,
        }
    except Exception as e:
        logger.exception("Visualizations query failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/lifestyle")
async def lifestyle(
    user: str = Query(default="default"),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
):
    bundle = _require_user(user)
    start, end = _resolve_range(start, end, default_days=90)
    loop = asyncio.get_event_loop()
    svc = bundle.lifestyle

    async def _run(fn, *args):
        try:
            return await loop.run_in_executor(None, fn, *args)
        except Exception as exc:
            logger.warning("Lifestyle %s failed: %s", fn.__name__, exc)
            return {"error": str(exc)}

    results = await asyncio.gather(
        _run(svc.behavior_dose_response, start, end),
        _run(svc.caffeine_cutoff, start, end),
        _run(svc.sleep_regularity, start, end),
        _run(svc.social_jet_lag, start, end),
        _run(svc.behavior_recovery_cost, start, end),
        _run(svc.stress_resilience, start, end),
        _run(svc.body_battery_decay, start, end),
        _run(svc.illness_radar, start, end),
        _run(svc.inflammation_index, start, end),
        _run(svc.recovery_debt, start, end),
        _run(svc.behavior_streak_calendar, start, end),
        _run(svc.habit_half_life, end),
        _run(svc.behavior_cooccurrence, start, end),
        _run(svc.step_distribution, start, end),
        _run(svc.fitness_age_delta, start, end),
        _run(svc.who_intensity_target, start, end),
        _run(svc.cycle_hrv, start, end),
        _run(svc.stress_hour_fingerprint, start, end),
        _run(svc.stress_trigger_leaderboard, start, end),
    )
    keys = [
        "dose_response", "caffeine_cutoff", "sleep_regularity", "social_jet_lag",
        "recovery_cost", "stress_resilience", "body_battery_decay", "illness_radar",
        "inflammation_index", "recovery_debt", "streak_calendar", "habit_half_life",
        "cooccurrence", "step_distribution", "fitness_age_delta", "who_target",
        "cycle_hrv", "stress_hour_fingerprint", "stress_triggers",
    ]
    return {"user": user, "date_range": {"start": start, "end": end}, **dict(zip(keys, results))}


@app.get("/api/intraday/heatmap")
async def intraday_heatmap(
    user: str = Query(default="default"),
    metric: str = Query(default="stress", description="stress | body_battery | heart_rate"),
    days: int = Query(default=14, ge=1, le=90),
):
    bundle = _require_user(user)
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, bundle.viz.intraday_heatmap, metric, days)
        return result
    except Exception as e:
        logger.exception("Intraday heatmap query failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/chat")
async def chat(body: ChatRequest):
    """Stream a chat response via Server-Sent Events with per-session history."""
    bundle = _require_user(body.user)
    if _sessions is None:
        raise HTTPException(status_code=503, detail="Sessions not initialised")

    session_id, history, _ = _sessions.get_or_create(body.session_id, user_id=body.user)

    async def sse_generator() -> AsyncGenerator[str, None]:
        yield f"data: {json.dumps({'type': 'session', 'session_id': session_id, 'user': body.user})}\n\n"

        loop = asyncio.get_event_loop()
        gen = bundle.agent.chat_stream(body.message, history=history)
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
        raise HTTPException(status_code=503, detail="Sessions not initialised")
    _sessions.reset(body.session_id)
    return {"reset": True, "session_id": body.session_id}


@app.post("/api/scan")
async def scan(body: ScanRequest):
    bundle = _require_user(body.user)

    valid_focus = {"general", "morning", "midday", "evening", "weekly"}
    if body.focus not in valid_focus:
        raise HTTPException(status_code=400, detail=f"focus must be one of {valid_focus}")

    try:
        loop = asyncio.get_event_loop()
        report = await loop.run_in_executor(
            None, bundle.agent.generate_scan_report, body.focus, "", body.start_date, body.end_date
        )
        return {
            "user": body.user,
            "focus": body.focus,
            "report": report,
            "timestamp": datetime.utcnow().isoformat(),
        }
    except Exception as e:
        logger.error("Scan failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def run_server(host: str | None = None, port: int | None = None):
    import uvicorn
    settings = get_settings()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    uvicorn.run(
        "garmin_insights.web.app:app",
        host=host or settings.web_host,
        port=port or settings.web_port,
        reload=False,
    )


if __name__ == "__main__":
    run_server()
