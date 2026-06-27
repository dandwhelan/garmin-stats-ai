"""FastAPI web server — dashboard, per-session chat, and AI scans.

Multi-user aware: each request specifies a `user` parameter selecting which
user's database to query. Agent and visualization service instances are
pooled per-user inside UserContext.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from typing import Any
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


def _scrub_nan(obj: Any) -> Any:
    """Recursively replace NaN/Inf floats with None so json.dumps doesn't raise.

    pandas surfaces missing numeric cells as NaN; FastAPI's default JSONResponse
    rejects those. Applied globally so individual endpoints don't each need to
    sanitise their payloads.
    """
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _scrub_nan(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub_nan(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(_scrub_nan(v) for v in obj)
    return obj


class SafeJSONResponse(JSONResponse):
    def render(self, content: Any) -> bytes:
        return super().render(_scrub_nan(content))


app = FastAPI(
    title="Garmin Health Insights",
    description="Personal health analytics dashboard with AI chat",
    lifespan=lifespan,
    default_response_class=SafeJSONResponse,
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


class PromptRequest(BaseModel):
    user: str = "default"
    message: str | None = None
    focus: str | None = None
    start_date: str | None = None
    end_date: str | None = None


class NoteRequest(BaseModel):
    user: str = "default"
    date: str
    note: str = ""


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


def _extract_chat_tags(text: str) -> list[str]:
    t = (text or "").lower()
    keyword_tags = {
        "sleep": ["sleep", "insomnia", "wake", "bed"],
        "stress": ["stress", "anxious", "anxiety", "overwhelmed"],
        "hrv": ["hrv"],
        "rhr": ["resting heart", "rhr", "pulse"],
        "illness": ["sick", "ill", "fever", "cold", "flu"],
        "nutrition": ["ate", "meal", "food", "dinner", "lunch", "breakfast"],
        "alcohol": ["alcohol", "drink", "beer", "wine"],
        "caffeine": ["coffee", "caffeine", "espresso", "tea"],
        "training": ["workout", "run", "ride", "lifting", "training", "exercise"],
        "mood": ["mood", "sad", "happy", "irritable", "depressed"],
    }
    tags = [k for k, words in keyword_tags.items() if any(w in t for w in words)]
    return tags[:8]


def _extract_assistant_text(history: list[dict]) -> str:
    """Latest assistant text from history, tolerating both plain dict content
    blocks and Anthropic SDK block objects (TextBlock/ToolUseBlock), which is
    what the agent actually stores. The old code only handled dicts, so the
    assistant reply never got persisted into chat memory."""
    for msg in reversed(history):
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for b in content:
                if isinstance(b, dict):
                    if b.get("type") == "text":
                        parts.append(b.get("text", "") or "")
                elif getattr(b, "type", None) == "text":
                    parts.append(getattr(b, "text", "") or "")
            text = " ".join(p for p in parts if p).strip()
            if text:
                return text
    return ""


# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index():
    # Always revalidate the HTML so new ?v= asset references reach the client
    # immediately; the versioned static files themselves stay cacheable.
    return HTMLResponse(
        content=(_STATIC_DIR / "index.html").read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/api/users")
async def list_users():
    """Return the list of configured users for the picker UI.

    The frontend hides the dropdown when this returns a single entry,
    so single-user mode (the default) keeps the header clean.
    """
    if _users is None:
        raise HTTPException(status_code=503, detail="Server not initialised")
    return {"users": [{"id": uid} for uid in _users.user_ids]}


def _resolve_user_identity(settings) -> dict[str, str]:
    """Derive a display name from settings: explicit display_name > email local
    part > 'User'. Returned alongside the email for the UI to show both."""
    email = (settings.garminconnect_email or "").strip()
    explicit = (settings.display_name or "").strip()
    if explicit:
        name = explicit
    elif email:
        local = email.split("@", 1)[0]
        # "jane.doe" -> "Jane Doe"
        name = " ".join(p.capitalize() for p in local.replace("_", ".").split("."))
    else:
        name = "User"
    sex = (settings.biological_sex or "").strip()
    return {"name": name, "email": email, "biological_sex": sex,
            "tracks_cycle": sex.lower() == "female"}


def _tracks_cycle(settings) -> bool:
    """Menstrual-cycle analytics are surfaced only for users whose biological
    sex is Female. Male users never see cycle charts, even if stray cycle rows
    exist in their database."""
    return (getattr(settings, "biological_sex", "") or "").strip().lower() == "female"


_CYCLE_NOT_TRACKED = {
    "available": False,
    "note": "Cycle tracking is shown only for users with biological sex Female.",
}


_FLOW_INTENSITY = {"LIGHT": 1, "MEDIUM": 2, "HEAVY": 3}
_CYCLE_PHASES = ("MENSTRUAL", "FOLLICULAR", "OVULATORY", "LUTEAL")


def _enrich_summaries_with_cycle(bundle, summaries, start, end):
    """Merge menstrual cycle fields into each daily summary so the Entities
    tab can chart them like any other numeric metric. No-op when the user
    doesn't track cycles."""
    if not summaries:
        return
    if not _tracks_cycle(bundle.agent._settings):
        return
    try:
        df = bundle.agent._repo.query_menstrual_cycle(start, end)
    except Exception:
        return
    if df is None or df.empty:
        return
    by_date = {row["date"]: row for _, row in df.iterrows()}
    for s in summaries:
        row = by_date.get(s.get("date"))
        if row is None:
            continue
        day = row.get("current_day_of_cycle")
        if day is not None and not (isinstance(day, float) and day != day):
            try:
                s["cycleDay"] = int(day)
            except (TypeError, ValueError):
                pass
        phase = (row.get("current_cycle_phase") or "").upper()
        for p in _CYCLE_PHASES:
            s[f"cyclePhase{p.title()}"] = 1 if phase == p else 0
        flow = (row.get("menstrual_flow") or "").upper()
        s["cycleFlowIntensity"] = _FLOW_INTENSITY.get(flow, 0)
        clen = row.get("cycle_length") or row.get("predicted_cycle_length")
        if clen is not None and not (isinstance(clen, float) and clen != clen):
            try:
                s["cycleLength"] = int(clen)
            except (TypeError, ValueError):
                pass


_ENV_NUMERIC_KEYS = (
    "temp_min_c", "temp_mean_c", "temp_max_c", "apparent_temp_max_c",
    "precipitation_mm", "wind_max_kmh", "humidity_mean", "uv_index_max",
    "european_aqi", "pm25", "pm10", "o3", "no2",
    "pollen_alder", "pollen_birch", "pollen_grass",
    "pollen_mugwort", "pollen_olive", "pollen_ragweed",
)


def _enrich_summaries_with_environment(bundle, summaries, start, end):
    """Merge Open-Meteo + Home Assistant fields into each daily summary so
    they appear in the Entities picker (env_* prefix) alongside Garmin
    metrics. No-op when the user has no HOME_LAT/LON or HA configured."""
    if not summaries:
        return
    # Open-Meteo env data
    try:
        df = bundle.agent._repo.query_environment(start, end)
    except Exception:
        df = None
    env_by_date: dict[str, dict] = {}
    if df is not None and not df.empty:
        if df.index.name is not None:
            df = df.reset_index()
        for row in df.to_dict(orient="records"):
            d = str(row.get("date", ""))[:10]
            if not d:
                continue
            env_by_date[d] = {
                k: row[k] for k in _ENV_NUMERIC_KEYS
                if row.get(k) is not None
                and not (isinstance(row.get(k), float) and row.get(k) != row.get(k))
            }
    # Bedroom / other HA sensors — pivot entity_id to a column name.
    try:
        ha_df = bundle.agent._repo.query_ha_sensors(start, end)
    except Exception:
        ha_df = None
    if ha_df is not None and not ha_df.empty:
        for _, row in ha_df.iterrows():
            d = str(row.get("date", ""))[:10]
            entity = (row.get("entity_id") or "").lower()
            if not d or not entity:
                continue
            short = entity.split(".", 1)[-1].replace("_temperature", "").replace("_temp", "")
            short = short.replace("xi_", "").strip("_")
            if "bedroom" in entity:
                short = "bedroom_temp"
            slot = env_by_date.setdefault(d, {})
            for src, suffix in (
                ("mean_value", "mean"),
                ("min_value", "min"),
                ("max_value", "max"),
                ("overnight_mean", "overnight"),
            ):
                val = row.get(src)
                if val is None or (isinstance(val, float) and val != val):
                    continue
                try:
                    slot[f"{short}_{suffix}_c"] = float(val)
                except (TypeError, ValueError):
                    pass
    if not env_by_date:
        return
    for s in summaries:
        d = str(s.get("date", ""))[:10]
        fields = env_by_date.get(d)
        if not fields:
            continue
        for k, v in fields.items():
            s[f"env_{k}"] = v


def _last_sync_iso(db_path: str) -> str | None:
    """Return the freshest SQLite write time as an ISO 8601 UTC timestamp.

    The fetcher writes to the DB on every successful Garmin pull. Under WAL mode
    those writes land in the ``-wal`` sidecar and the main ``.db`` file's mtime
    only advances on a checkpoint (which can lag by hours), so we take the most
    recent mtime across the DB and its ``-wal``/``-shm`` sidecars to get a
    faithful "last synced" indicator for the UI. None if missing.
    """
    try:
        from pathlib import Path
        p = Path(db_path)
        if not p.exists():
            return None
        mtimes = [p.stat().st_mtime]
        for suffix in ("-wal", "-shm"):
            sidecar = Path(db_path + suffix)
            if sidecar.exists():
                mtimes.append(sidecar.stat().st_mtime)
        return datetime.utcfromtimestamp(max(mtimes)).isoformat() + "Z"
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
            "user": _resolve_user_identity(bundle.agent._settings),
            "database": repo_health,
            "last_sync": _last_sync_iso(bundle.agent._settings.sqlite_db_path),
            "model": bundle.agent._settings.claude_model,
            "sessions": _sessions.stats() if _sessions else {},
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
    except Exception as e:
        return JSONResponse(status_code=503, content={"status": "error", "detail": str(e)})


@app.get("/api/dashboard")
async def dashboard(
    user: str = Query(default="default"),
    start: str | None = Query(default=None, description="Start date YYYY-MM-DD"),
    end: str | None = Query(default=None, description="End date YYYY-MM-DD"),
):
    """Return daily summaries plus baselines for the selected window.

    Rebuilds the daily_summaries cache from fresh daily_stats data at most
    once every 60 s so that new fetcher writes appear on the dashboard
    automatically without requiring a web-server restart.
    """
    global _last_cache_refresh
    bundle = _require_user(user)

    now = datetime.utcnow()
    if _last_cache_refresh is None or (now - _last_cache_refresh) >= _CACHE_REFRESH_INTERVAL:
        try:
            loop = asyncio.get_event_loop()
            # Rebuild last 7 days only (fast; full history rebuilt on startup)
            await loop.run_in_executor(None, bundle.agent.ensure_cache_fresh, 7)
            _last_cache_refresh = now
        except Exception as e:
            logger.warning("Dashboard cache refresh failed (non-fatal): %s", e)

    start, end = _resolve_range(start, end, default_days=30)

    try:
        loop = asyncio.get_event_loop()
        # Backfill the daily_summaries cache for the requested window so
        # historical date ranges (older than the rolling 90-day refresh)
        # return data instead of an empty list. build_range no-ops on
        # already-cached dates so the cost is paid once.
        try:
            await loop.run_in_executor(
                None, bundle.agent._cache.build_range, start, end
            )
        except Exception as e:
            logger.warning("Dashboard build_range failed (non-fatal): %s", e)
        summaries = await loop.run_in_executor(
            None, bundle.agent._memory.get_daily_summaries_range, start, end
        )
        baselines = await loop.run_in_executor(None, bundle.agent._memory.get_baselines)
        _enrich_summaries_with_cycle(bundle, summaries, start, end)
        _enrich_summaries_with_environment(bundle, summaries, start, end)
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

    async def _const(value):
        return value

    tracks_cycle = _tracks_cycle(bundle.agent._settings)

    # Warm the per-window load cache once, single-threaded, so the ~21
    # analytics below (which each re-read daily_summaries / lifestyle_journal)
    # all hit cache instead of racing to read the same tables concurrently.
    await loop.run_in_executor(None, svc.prewarm, start, end)

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
        _run(svc.cycle_hrv, start, end) if tracks_cycle else _const(dict(_CYCLE_NOT_TRACKED)),
        _run(svc.cycle_yearly) if tracks_cycle else _const(dict(_CYCLE_NOT_TRACKED)),
        _run(svc.stress_hour_fingerprint, start, end),
        _run(svc.stress_trigger_leaderboard, start, end),
        _run(svc.research_signal_scorecard, start, end),
    )
    keys = [
        "dose_response", "caffeine_cutoff", "sleep_regularity", "social_jet_lag",
        "recovery_cost", "stress_resilience", "body_battery_decay", "illness_radar",
        "inflammation_index", "recovery_debt", "streak_calendar", "habit_half_life",
        "cooccurrence", "step_distribution", "fitness_age_delta", "who_target",
        "cycle_hrv", "cycle_yearly", "stress_hour_fingerprint", "stress_triggers",
        "research_scorecard",
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


@app.get("/api/activities/gps")
async def activities_with_gps(
    user: str = Query(default="default"),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
):
    """List activities with stored GPS tracks in the requested window."""
    bundle = _require_user(user)
    s, e = _resolve_range(start, end, default_days=30)
    loop = asyncio.get_event_loop()
    try:
        df = await loop.run_in_executor(
            None, bundle.agent._repo.query_activities_with_gps, s, e
        )
        if df.empty:
            return {"start": s, "end": e, "activities": []}
        df = df.reset_index()
        df["time"] = df["time"].astype(str)
        return {"start": s, "end": e, "activities": df.to_dict(orient="records")}
    except Exception as ex:
        logger.exception("Activities-with-GPS query failed")
        raise HTTPException(status_code=500, detail=str(ex))


@app.get("/api/activities/{activity_id}/track")
async def activity_track(
    activity_id: int,
    user: str = Query(default="default"),
):
    """Return the GPS track for a single activity as ordered points."""
    bundle = _require_user(user)
    loop = asyncio.get_event_loop()
    try:
        df = await loop.run_in_executor(
            None, bundle.agent._repo.query_activity_gps, activity_id
        )
        if df.empty:
            return {"activity_id": activity_id, "points": []}
        df = df.reset_index()
        df["time"] = df["time"].astype(str)
        # FastAPI's JSON encoder rejects NaN/Inf as non-compliant — coerce them
        # to None so the client gets a clean payload.
        import numpy as np
        clean = df.replace([np.inf, -np.inf], np.nan)
        clean = clean.astype(object).where(clean.notna(), None)
        return {"activity_id": activity_id, "points": clean.to_dict(orient="records")}
    except Exception as ex:
        logger.exception("Activity track query failed")
        raise HTTPException(status_code=500, detail=str(ex))


@app.get("/api/activities/{activity_id}/export")
async def activity_export(
    activity_id: int,
    user: str = Query(default="default"),
):
    """Return a plain-text stats block for one activity — suitable for pasting into AI."""
    bundle = _require_user(user)
    loop = asyncio.get_event_loop()
    try:
        data = await loop.run_in_executor(
            None, bundle.agent._repo.query_activity_export, activity_id
        )
    except Exception as ex:
        logger.exception("Activity export query failed")
        raise HTTPException(status_code=500, detail=str(ex))

    if not data:
        raise HTTPException(status_code=404, detail="Activity not found")

    s = data["summary"]
    g = data.get("gps", {})

    def _fmt_duration(secs) -> str:
        if secs is None:
            return "—"
        secs = int(secs)
        h, m, sec = secs // 3600, (secs % 3600) // 60, secs % 60
        return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"

    def _fmt_pace(dist_m, secs) -> str:
        if not dist_m or not secs or dist_m < 1:
            return "—"
        sec_per_km = secs / (dist_m / 1000)
        m, s = int(sec_per_km // 60), int(sec_per_km % 60)
        return f"{m}:{s:02d} /km"

    def _fmt_speed(mps) -> str:
        if mps is None:
            return "—"
        return f"{mps * 3.6:.1f} km/h"

    def _zone_line(key, label) -> str:
        val = s.get(key)
        if val is None:
            return ""
        return f"  {label}: {_fmt_duration(val)}\n"

    activity_time = s.get("time", "")
    if hasattr(activity_time, "isoformat"):
        activity_time = activity_time.isoformat()

    dist_m = s.get("distance")
    dist_km = f"{dist_m / 1000:.2f} km" if dist_m else "—"
    activity_type = (s.get("activity_type") or "activity").title()
    name = s.get("activity_name") or activity_type
    location = s.get("location_name") or ""

    lines = [
        f"## {name} — {activity_time}",
        "",
        f"**Type:** {activity_type}" + (f" | {location}" if location else ""),
        f"**Duration:** {_fmt_duration(s.get('elapsed_duration'))} total"
        + (f" ({_fmt_duration(s.get('moving_duration'))} moving)" if s.get("moving_duration") else ""),
        f"**Distance:** {dist_km}",
        f"**Avg pace / speed:** {_fmt_pace(dist_m, s.get('elapsed_duration'))} / {_fmt_speed(s.get('average_speed'))}",
        f"**Best pace / speed:** {_fmt_pace(dist_m, s.get('moving_duration'))} / {_fmt_speed(s.get('max_speed'))}",
        f"**Calories:** {int(s['calories'])} kcal" + (f" (+ {int(s['bmr_calories'])} BMR)" if s.get("bmr_calories") else "")
        if s.get("calories") else "",
        "",
        f"**Heart Rate:** avg {int(s['average_hr'])} bpm | max {int(s['max_hr'])} bpm"
        if s.get("average_hr") and s.get("max_hr") else "",
    ]

    zones = (
        _zone_line("hr_time_in_zone_1", "Zone 1") +
        _zone_line("hr_time_in_zone_2", "Zone 2") +
        _zone_line("hr_time_in_zone_3", "Zone 3") +
        _zone_line("hr_time_in_zone_4", "Zone 4") +
        _zone_line("hr_time_in_zone_5", "Zone 5")
    )
    if zones:
        lines += ["**HR Zones:**", zones.rstrip()]

    if g.get("elevation_gain_m") is not None:
        lines += ["", f"**Elevation:** +{g['elevation_gain_m']} m gain / {g['elevation_loss_m']} m loss"]

    # Running dynamics — only populated for runs (avg_run_cadence et al.).
    rd = []
    if s.get("avg_run_cadence"):
        max_rc = f" | max {int(s['max_run_cadence'])} spm" if s.get("max_run_cadence") else ""
        rd.append(f"  Cadence: avg {int(s['avg_run_cadence'])} spm{max_rc}")
    if s.get("avg_stride_length"):  # stored in cm → metres
        rd.append(f"  Stride length: {s['avg_stride_length'] / 100:.2f} m")
    if s.get("avg_vertical_oscillation") is not None:
        rd.append(f"  Vertical oscillation: {s['avg_vertical_oscillation']:.1f} cm")
    if s.get("avg_vertical_ratio") is not None:
        rd.append(f"  Vertical ratio: {s['avg_vertical_ratio']:.1f} %")
    if s.get("avg_ground_contact_time"):
        rd.append(f"  Ground contact time: {int(s['avg_ground_contact_time'])} ms")
    if rd:
        lines += ["", "**Running Dynamics:**", *rd]

    # Power — prefer the activity summary (incl. normalized power); fall back to
    # GPS-track aggregates when the summary has none (e.g. older fetches).
    if s.get("avg_power"):
        norm = f" | norm {int(s['norm_power'])} W" if s.get("norm_power") else ""
        max_p = f" | max {int(s['max_power'])} W" if s.get("max_power") else ""
        lines.append(f"**Power:** avg {int(s['avg_power'])} W{max_p}{norm}")
    elif g.get("avg_power_w") is not None:
        lines.append(f"**Power:** avg {g['avg_power_w']} W | max {g['max_power_w']} W")

    if g.get("avg_cadence_spm") is not None and not s.get("avg_run_cadence"):
        lines.append(f"**Cadence:** avg {g['avg_cadence_spm']} spm | max {g['max_cadence_spm']} spm")
    if g.get("avg_temp_c") is not None:
        lines.append(f"**Temperature:** avg {g['avg_temp_c']} °C | max {g['max_temp_c']} °C")

    if s.get("lap_count"):
        lines.append(f"**Laps:** {int(s['lap_count'])}")

    text = "\n".join(l for l in lines if l is not None)
    return JSONResponse({"text": text, "activity_id": activity_id, "name": name})


@app.get("/api/environment")
async def environment(
    user: str = Query(default="default"),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
):
    """Open-Meteo weather + air quality + pollen for the requested window.

    Returns `available: false` when the user has no environment_daily rows
    (i.e. HOME_LAT/HOME_LON not configured) so the frontend can hide the
    section gracefully.
    """
    bundle = _require_user(user)
    s, e = _resolve_range(start, end, default_days=30)
    loop = asyncio.get_event_loop()
    try:
        df = await loop.run_in_executor(None, bundle.agent._repo.query_environment, s, e)
        if df is None or df.empty:
            return {"start": s, "end": e, "available": False, "entries": []}
        # pandas float columns store NaN for missing values; json.dumps rejects
        # out-of-range floats. df.to_json serialises NaN→null correctly.
        entries = json.loads(df.to_json(orient="records"))
        return {"start": s, "end": e, "available": True, "entries": entries}
    except Exception as ex:
        logger.exception("Environment query failed")
        raise HTTPException(status_code=500, detail=str(ex))


@app.get("/api/ha_sensors")
async def ha_sensors(
    user: str = Query(default="default"),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
):
    """Home Assistant sensor daily aggregates from ha_sensor_daily."""
    bundle = _require_user(user)
    s, e = _resolve_range(start, end, default_days=30)
    loop = asyncio.get_event_loop()
    try:
        df = await loop.run_in_executor(None, bundle.agent._repo.query_ha_sensors, s, e)
        if df is None or df.empty:
            return {"start": s, "end": e, "available": False, "entries": []}
        entries = json.loads(df.to_json(orient="records"))
        return {"start": s, "end": e, "available": True, "entries": entries}
    except Exception as ex:
        logger.exception("HA sensors query failed")
        raise HTTPException(status_code=500, detail=str(ex))


@app.get("/api/environment/recovery")
async def environment_recovery(
    user: str = Query(default="default"),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
):
    """Join environment (temp/AQI/pollen) with RHR/HRV/respiration/sleep.

    Powers the Environment ↔ Recovery overlay chart and ships Pearson r
    values per (driver, marker) pair so the chart can show research-aligned
    correlation strength. Returns `available: false` when the user has no
    environment_daily rows so the frontend can hide the section.
    """
    bundle = _require_user(user)
    s, e = _resolve_range(start, end, default_days=60)
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, bundle.viz.environment_recovery, s, e)
        result["user"] = user
        return result
    except Exception as ex:
        logger.exception("Environment-recovery query failed")
        raise HTTPException(status_code=500, detail=str(ex))


@app.get("/api/behavior-environment")
async def behavior_environment(
    user: str = Query(default="default"),
    behavior: str = Query(..., description="Lifestyle behavior name (case-sensitive)"),
    drivers: str = Query(
        default="pollen_grass,pollen_birch,pollen_ragweed,pollen_alder,pollen_olive,pollen_mugwort",
        description="Comma-separated environment_daily column names",
    ),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
):
    """Cross-tab a logged lifestyle behavior (e.g. 'Allergy Symptoms',
    'Asthma symptoms') against environmental drivers and recovery markers.

    Returns the on/off-day means, deltas, and Pearson r per driver pair.
    """
    bundle = _require_user(user)
    s, e = _resolve_range(start, end, default_days=90)
    env_cols = [c.strip() for c in drivers.split(",") if c.strip()]
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(
            None, bundle.viz.behavior_environment_impact, behavior, env_cols, s, e
        )
        result["user"] = user
        return result
    except Exception as ex:
        logger.exception("behavior-environment query failed")
        raise HTTPException(status_code=500, detail=str(ex))


@app.get("/api/bedroom-temp-sleep")
async def bedroom_temp_sleep(
    user: str = Query(default="default"),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
):
    """Overnight Home Assistant bedroom temperature vs sleep / HRV / awakenings."""
    bundle = _require_user(user)
    s, e = _resolve_range(start, end, default_days=60)
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, bundle.viz.bedroom_temp_sleep, s, e)
        result["user"] = user
        return result
    except Exception as ex:
        logger.exception("bedroom-temp-sleep query failed")
        raise HTTPException(status_code=500, detail=str(ex))


@app.get("/api/behavior-root-cause")
async def behavior_root_cause(
    user: str = Query(default="default"),
    behavior: str = Query(..., description="Behavior to root-cause"),
    lookback_hours: int = Query(default=48, ge=24, le=168),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
):
    """Per-event 24-48h confounder scan for a logged behavior (e.g. Migraines)."""
    bundle = _require_user(user)
    s, e = _resolve_range(start, end, default_days=180)
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(
            None, bundle.viz.behavior_root_cause, behavior, s, e, lookback_hours
        )
        result["user"] = user
        return result
    except Exception as ex:
        logger.exception("behavior-root-cause query failed")
        raise HTTPException(status_code=500, detail=str(ex))


@app.get("/api/menstrual")
async def menstrual(
    user: str = Query(default="default"),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
):
    """Menstrual cycle data for the requested window. Empty result if user doesn't track."""
    bundle = _require_user(user)
    s, e = _resolve_range(start, end, default_days=90)
    if not _tracks_cycle(bundle.agent._settings):
        return {"start": s, "end": e, "tracked": False, "entries": []}
    loop = asyncio.get_event_loop()
    try:
        df = await loop.run_in_executor(None, bundle.agent._repo.query_menstrual_cycle, s, e)
        if df.empty:
            return {"start": s, "end": e, "tracked": False, "entries": []}
        return {
            "start": s,
            "end": e,
            "tracked": True,
            "entries": df.to_dict(orient="records"),
        }
    except Exception as ex:
        logger.exception("Menstrual query failed")
        raise HTTPException(status_code=500, detail=str(ex))


@app.get("/api/notes")
async def get_notes(
    user: str = Query(default="default"),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
):
    """Return the user's free-text daily notes for the window as {date: note}."""
    bundle = _require_user(user)
    s, e = _resolve_range(start, end, default_days=30)
    loop = asyncio.get_event_loop()
    entries = await loop.run_in_executor(
        None, bundle.agent._memory.get_daily_notes_range, s, e
    )
    return {"entries": entries, "date_range": {"start": s, "end": e}}


@app.post("/api/notes")
async def save_note(req: NoteRequest):
    """Create, update, or (with an empty body) delete a day's free-text note."""
    bundle = _require_user(req.user)
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None, bundle.agent._memory.upsert_daily_note, req.date, req.note
    )
    saved = bool(req.note and req.note.strip())
    return {"saved": saved, "deleted": not saved, "date": req.date}


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
        streamed_parts: list[str] = []

        try:
            while True:
                event = await loop.run_in_executor(None, next, gen, sentinel)
                if event is sentinel:
                    break
                # Capture the assistant's visible text as it streams — this is
                # the reliable source for persistence (history stores SDK block
                # objects, not dicts, which the old parser silently dropped).
                if isinstance(event, dict) and event.get("type") == "text":
                    streamed_parts.append(event.get("text", "") or "")
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as e:
            logger.exception("Chat stream failed")
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"
        finally:
            try:
                assistant_text = "".join(streamed_parts).strip()
                if not assistant_text:
                    # Fallback: parse it back out of history (SDK-aware).
                    assistant_text = _extract_assistant_text(history)
                bundle.agent._memory.save_chat_memory(
                    user_id=body.user,
                    user_text=body.message,
                    assistant_text=assistant_text[:2000] if assistant_text else None,
                    tags=_extract_chat_tags(body.message),
                )
            except Exception:
                logger.warning("Failed to persist chat memory", exc_info=True)
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


@app.get("/api/chat/history")
async def chat_history(
    user: str = Query(default="default"),
    limit: int = Query(default=25, ge=1, le=100),
):
    bundle = _require_user(user)
    loop = asyncio.get_event_loop()
    items = await loop.run_in_executor(None, bundle.agent._memory.get_recent_chat_memory, user, limit)
    return {"user": user, "count": len(items), "items": items}


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


@app.post("/api/prompt/generate")
async def generate_prompt(body: PromptRequest):
    """Build a self-contained text prompt the user can paste into a free LLM.

    Includes the full system context plus a pre-fetched data snapshot, so the
    receiving chat (Claude.ai, ChatGPT, Gemini, ...) has the same picture our
    tool-calling agent would build for itself — no API tokens needed.
    """
    bundle = _require_user(body.user)
    if not body.message and not body.focus:
        raise HTTPException(status_code=400, detail="Provide either 'message' or 'focus'")
    if body.focus:
        valid_focus = {"general", "morning", "midday", "evening", "weekly"}
        if body.focus not in valid_focus:
            raise HTTPException(status_code=400, detail=f"focus must be one of {valid_focus}")
    try:
        loop = asyncio.get_event_loop()
        prompt = await loop.run_in_executor(
            None,
            bundle.agent.build_portable_prompt,
            body.message,
            body.focus,
            body.start_date,
            body.end_date,
        )
        return {
            "user": body.user,
            "prompt": prompt,
            "chars": len(prompt),
            "approx_tokens": len(prompt) // 4,
        }
    except Exception as e:
        logger.exception("Prompt generation failed")
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
