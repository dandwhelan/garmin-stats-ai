"""FastAPI web server — dashboard and streaming chat interface."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from garmin_insights.agent import HealthAgent
from garmin_insights.config import get_settings

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"

# Global agent instance — initialised on startup
_agent: HealthAgent | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _agent
    settings = get_settings()
    logger.info("Initialising health agent...")
    _agent = HealthAgent(settings)
    try:
        _agent.ensure_cache_fresh(days=90)
        logger.info("Agent ready.")
    except Exception as e:
        logger.warning("Cache refresh on startup failed: %s", e)
    yield
    if _agent:
        logger.info("Saving session and closing agent...")
        _agent.save_session()
        _agent.close()


app = FastAPI(
    title="Garmin Health Insights",
    description="Personal health analytics dashboard with AI chat",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


# ------------------------------------------------------------------
# Request / response models
# ------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str


class ScanRequest(BaseModel):
    focus: str = "general"


# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = _STATIC_DIR / "index.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


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
            "timestamp": datetime.utcnow().isoformat(),
        }
    except Exception as e:
        return JSONResponse(status_code=503, content={"status": "error", "detail": str(e)})


@app.get("/api/dashboard")
async def dashboard():
    """Return the last 30 days of daily summaries for the dashboard."""
    if _agent is None:
        raise HTTPException(status_code=503, detail="Agent not initialised")

    end = datetime.utcnow().strftime("%Y-%m-%d")
    start = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")

    try:
        summaries = _agent._memory.get_daily_summaries_range(start, end)
        baselines = _agent._memory.get_baselines()
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
    """Stream a chat response via Server-Sent Events."""
    if _agent is None:
        raise HTTPException(status_code=503, detail="Agent not initialised")

    async def sse_generator() -> AsyncGenerator[str, None]:
        loop = asyncio.get_event_loop()
        gen = _agent.chat_stream(body.message)
        sentinel = object()

        try:
            while True:
                # Run the blocking next() call in a thread pool so we don't block the event loop
                chunk = await loop.run_in_executor(None, next, gen, sentinel)
                if chunk is sentinel:
                    break
                payload = json.dumps({"text": chunk})
                yield f"data: {payload}\n\n"
        except Exception as e:
            logger.error("Chat stream error: %s", e)
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        finally:
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        sse_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/chat/reset")
async def chat_reset():
    """Clear the current conversation history."""
    if _agent is None:
        raise HTTPException(status_code=503, detail="Agent not initialised")
    _agent.reset_conversation()
    return {"reset": True}


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
