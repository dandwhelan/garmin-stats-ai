"""FastAPI web server — dashboard, per-session chat, and AI scans."""

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

from garmin_insights.agent import HealthAgent
from garmin_insights.config import get_settings
from garmin_insights.web.sessions import SessionManager

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"

# Module-level singletons set up by the lifespan handler
_agent: HealthAgent | None = None
_sessions: SessionManager | None = None


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
    start_date: str | None = None
    end_date: str | None = None


# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(content=(_STATIC_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/api/health")
async def health_check():
    if _agent is None:
        raise HTTPException(status_code=503, detail="Agent not initialised")
    try:
        repo_health = _agent._repo.health_check()
        return {
            "status": "ok",
            "database": repo_health,
            "model": _agent._settings.claude_model,
            "sessions": _sessions.stats() if _sessions else {},
            "timestamp": datetime.utcnow().isoformat(),
        }
    except Exception as e:
        return JSONResponse(status_code=503, content={"status": "error", "detail": str(e)})


@app.get("/api/dashboard")
async def dashboard(
    start: str | None = Query(default=None, description="Start date YYYY-MM-DD"),
    end: str | None = Query(default=None, description="End date YYYY-MM-DD"),
):
    """Return daily summaries plus baselines for the requested date range (default: last 30 days)."""
    if _agent is None:
        raise HTTPException(status_code=503, detail="Agent not initialised")

    end = end or datetime.utcnow().strftime("%Y-%m-%d")
    start = start or (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")

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
            None, _agent.generate_scan_report, body.focus, "", body.start_date, body.end_date
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
