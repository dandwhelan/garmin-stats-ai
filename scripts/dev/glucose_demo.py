"""Offline demo server for the glucose + meal UI (no Garmin, no API key).

Builds a synthetic DB from the test fixtures (tests/conftest.py), adds 12
days of 15-min Libre-style glucose with three daily meal spikes + post-meal
dips + one overnight compression low, logs matching meals, and serves the
real FastAPI app on http://127.0.0.1:8099 with a stub agent (chat / scan /
photo-estimate endpoints that need Claude will not work).

    cd garmin-insights && python ../scripts/dev/glucose_demo.py
    # DEMO_PORT=8100 DEMO_DB=/tmp/x.db to override

Local time is pinned to UTC. Chart.js etc. load from CDNs; in a sandbox
without CDN access route those requests to local copies (see
docs/handover-glucose-llm.md, "Verifying the UI").
"""
import sys, os, types, threading, time, math, random
os.environ["TZ"]="UTC"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "garmin-insights", "tests"))
import conftest as c
from datetime import datetime, timedelta, timezone
import tempfile
db = os.environ.get("DEMO_DB") or os.path.join(tempfile.gettempdir(), "garmin-glucose-demo.db")
if os.path.exists(db): os.remove(db)
rows = c._sample_rows(); c._write_sample_db(db, rows)
from garmin_insights.insights import glucose
start = (datetime.now(timezone.utc) - timedelta(days=12)).replace(hour=0, minute=0, second=0, microsecond=0)
random.seed(1); out=[]
for i in range(12*96):
    t = start + timedelta(minutes=15*i); h = t.hour + t.minute/60
    v = 5.0 + 0.2*math.sin(i/7)
    for meal, amp in ((8, 2.2), (13, 2.8), (19.5, 3.2)):
        d = h - meal
        if 0 <= d < 3: v += amp*math.exp(-((d-0.75)**2)/0.25) - (0.6 if 1.8 < d < 3 else 0)
    if i == 5*96 + 10: v = 3.0
    if i == 5*96 + 11: v = 3.1
    out.append({"time": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "source": "libreview_csv", "mg_dl": v*18.0182 + random.uniform(-3,3), "sensor_id": "S1"})
glucose.upsert_readings(db, out)
from garmin_insights.insights import meals as M
for d in range(1, 12):
    day = (start + timedelta(days=d)).strftime('%Y-%m-%d')
    M.save_meal(db, {'eaten_at': day+'T08:00', 'description': 'Porridge with banana', 'carbs_g': 55})
    M.save_meal(db, {'eaten_at': day+'T13:00', 'description': 'Sandwich' if d % 2 else 'Chicken salad', 'carbs_g': 45 if d % 2 else 12})
    if d % 3: M.save_meal(db, {'eaten_at': day+'T19:30', 'description': 'Pasta bolognese', 'carbs_g': 85})
settings = types.SimpleNamespace(sqlite_db_path=db, display_name="Demo", garminconnect_email="d@example.com", biological_sex="Male", claude_model="x", claude_effort="low", user_timezone="UTC", height_cm="", birth_date="")
from garmin_insights.insights.overnight import OvernightService
from garmin_insights.web import app as m
from garmin_insights.web.lifestyle_viz import LifestyleService
from garmin_insights.web.sessions import SessionManager
from garmin_insights.web.user_context import UserBundle
from garmin_insights.web.visualizations import VisualizationService
bundle = UserBundle(user_id="default", agent=c._StubAgent(settings), viz=VisualizationService(db), lifestyle=LifestyleService(db), overnight=OvernightService(db))
class P:
    user_ids=["default"]
    def has_user(s,u): return u=="default"
    def get(s,u): return bundle
    def list_users(s): return [{"id":"default","db_path":db}]
    def close(s): pass
m._users=P(); m._sessions=SessionManager(ttl_seconds=60,max_sessions=8)
m.app.router.lifespan_context = c._noop_lifespan
import uvicorn
uvicorn.run(m.app, host="127.0.0.1", port=int(os.environ.get("DEMO_PORT", "8099")), log_level="warning")
