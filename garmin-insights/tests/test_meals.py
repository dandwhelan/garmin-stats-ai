"""Meal log: storage, glucose-response maths, flags, food summary,
unlogged rises, Open Food Facts parsing, photo estimate, API + agent tool."""

from __future__ import annotations

import json
import math
import sqlite3
import types
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from garmin_insights.insights import glucose, meals, overnight


@pytest.fixture(autouse=True)
def _utc_zone(monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    overnight._system_zone.cache_clear()
    yield
    overnight._system_zone.cache_clear()


def _seed_curve(db: str, start: datetime, days: int, meal_hours=(), amp=3.0, dip=0.0, step=5):
    """Flat 5.0 with a spike (peak at +45 min) and optional dip at +150 min after each meal."""
    rows = []
    n = days * 24 * 60 // step
    for i in range(n):
        t = start + timedelta(minutes=i * step)
        v = 5.0
        for mh in meal_hours:
            for d in range(days):
                m0 = start + timedelta(days=d, hours=mh)
                x = (t - m0).total_seconds() / 60
                if 0 <= x <= 120:
                    v += amp * math.exp(-((x - 45) ** 2) / (2 * 20 ** 2))
                if 120 < x <= 210 and dip:
                    v -= dip * math.exp(-((x - 160) ** 2) / (2 * 20 ** 2))
        rows.append({"time": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "source": "nightscout",
                     "mg_dl": round(v * glucose.MGDL_PER_MMOL, 1), "sensor_id": "A"})
    glucose.upsert_readings(db, rows)


def _meal(db, when: datetime, desc="Porridge", **kw):
    return meals.save_meal(db, {"eaten_at": when.strftime("%Y-%m-%dT%H:%M"), "description": desc, **kw})


START = datetime(2026, 11, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
def test_save_get_update_delete(tmp_path):
    db = str(tmp_path / "m.db")
    mid = _meal(db, datetime(2026, 11, 3, 8, 0), carbs_g="45", tags=["Wholegrain", "fruit"])
    got = meals.get_meal(db, mid)
    assert got["carbs_g"] == 45.0 and got["date"] == "2026-11-03"
    assert got["tags"] == ["wholegrain", "fruit"]
    meals.save_meal(db, {"eaten_at": "2026-11-03T08:30", "description": "Porridge + honey"}, mid)
    assert meals.get_meal(db, mid)["description"] == "Porridge + honey"
    assert "carbs_g" not in meals.get_meal(db, mid)
    assert meals.delete_meal(db, mid) is True
    assert meals.get_meal(db, mid) is None


def test_save_validates(tmp_path):
    db = str(tmp_path / "m.db")
    with pytest.raises(ValueError):
        meals.save_meal(db, {"eaten_at": "yesterday", "description": "x"})
    with pytest.raises(ValueError):
        meals.save_meal(db, {"eaten_at": "2026-11-03T08:00", "description": "  "})
    with pytest.raises(KeyError):
        meals.save_meal(db, {"eaten_at": "2026-11-03T08:00", "description": "x"}, 999)


def test_staples_rank_by_count_with_latest_macros(tmp_path, monkeypatch):
    db = str(tmp_path / "m.db")
    today = datetime.now().replace(hour=8, minute=0, second=0, microsecond=0)
    _meal(db, today - timedelta(days=3), "Porridge", carbs_g=40)
    _meal(db, today - timedelta(days=2), "porridge", carbs_g=50)
    _meal(db, today - timedelta(days=1), "Toast", carbs_g=30)
    staples = meals.recent_staples(db)
    assert staples[0]["n"] == 2 and staples[0]["carbs_g"] == 50
    assert staples[1]["description"] == "Toast"


def test_daily_meal_metrics(tmp_path):
    db = str(tmp_path / "m.db")
    _meal(db, datetime(2026, 11, 3, 8, 0), carbs_g=40)
    _meal(db, datetime(2026, 11, 3, 20, 30), "Pasta", carbs_g=80)
    d = meals.daily_meal_metrics(db, "2026-11-03", "2026-11-03")["2026-11-03"]
    assert d == {"meals_logged": 2, "meal_carbs_g": 120.0, "first_meal_hour": 8.0, "last_meal_hour": 20.5}


# ---------------------------------------------------------------------------
# Response maths
# ---------------------------------------------------------------------------
def test_response_rise_peak_iauc_and_dip(tmp_path):
    db = str(tmp_path / "m.db")
    _seed_curve(db, START, days=3, meal_hours=(12,), amp=3.0, dip=1.0)
    _meal(db, datetime(2026, 11, 2, 12, 0), "Pasta")
    out = meals.analyse(db, "2026-11-02", "2026-11-02")
    r = out["meals"][0]["response"]
    assert r["baseline_mmol"] == 5.0
    assert r["rise_mmol"] == pytest.approx(3.0, abs=0.1)
    assert r["time_to_peak_min"] == 45
    # Gaussian area: 3 * 20 * sqrt(2π) mmol·min ≈ 150 → 2.5 mmol/L·h
    assert r["iauc_mmol_h"] == pytest.approx(2.5, abs=0.15)
    assert r["dip_mmol"] == pytest.approx(1.0, abs=0.15)
    assert r["flags"] == []


def test_no_dip_reported_as_zero(tmp_path):
    db = str(tmp_path / "m.db")
    _seed_curve(db, START, days=3, meal_hours=(12,), amp=2.0)
    _meal(db, datetime(2026, 11, 2, 12, 0))
    r = meals.analyse(db, "2026-11-02", "2026-11-02")["meals"][0]["response"]
    assert r["dip_mmol"] == 0.0


def test_overlap_and_exercise_flags_excluded_from_foods(tmp_path):
    db = str(tmp_path / "m.db")
    _seed_curve(db, START, days=3, meal_hours=(12,))
    _meal(db, datetime(2026, 11, 2, 12, 0), "Pasta")
    _meal(db, datetime(2026, 11, 2, 13, 0), "Cake")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE activity_summary (activity_id INTEGER PRIMARY KEY, time TEXT, elapsed_duration REAL)")
    conn.execute("INSERT INTO activity_summary VALUES (1, '2026-11-02T12:30:00', 1800)")
    conn.commit(); conn.close()
    out = meals.analyse(db, "2026-11-02", "2026-11-02")
    flags = {m["description"]: m["response"]["flags"] for m in out["meals"]}
    assert "overlap" in flags["Pasta"] and "exercise" in flags["Pasta"]
    assert out["foods"] == []


def test_no_response_without_coverage(tmp_path):
    db = str(tmp_path / "m.db")
    _seed_curve(db, START, days=1)
    _meal(db, datetime(2026, 11, 5, 12, 0))
    out = meals.analyse(db, "2026-11-05", "2026-11-05")
    assert "response" not in out["meals"][0]


def test_food_summary_reliable_after_three(tmp_path):
    db = str(tmp_path / "m.db")
    _seed_curve(db, START, days=5, meal_hours=(8,), amp=2.0)
    for d in (1, 2, 3):
        _meal(db, datetime(2026, 11, 1 + d, 8, 0), "Porridge")
    _meal(db, datetime(2026, 11, 5, 8, 0), "Toast")
    foods = {f["description"]: f for f in meals.analyse(db, "2026-11-01", "2026-11-05")["foods"]}
    assert foods["Porridge"]["servings"] == 3 and foods["Porridge"]["reliable"] is True
    assert foods["Toast"]["reliable"] is False
    assert foods["Porridge"]["median_rise_mmol"] == pytest.approx(2.0, abs=0.1)


def test_unlogged_rise_detected_only_without_meal(tmp_path):
    db = str(tmp_path / "m.db")
    _seed_curve(db, START, days=3, meal_hours=(12, 16), amp=3.0)
    _meal(db, datetime(2026, 11, 2, 12, 0))
    rises = meals.analyse(db, "2026-11-02", "2026-11-02")["unlogged_rises"]
    assert len(rises) == 1
    assert rises[0]["start"].startswith("2026-11-02T1")
    assert "T16" in rises[0]["start"] or "T15" in rises[0]["start"] or rises[0]["peak_at"].startswith("2026-11-02T16")


# ---------------------------------------------------------------------------
# Open Food Facts + photo estimate
# ---------------------------------------------------------------------------
class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def test_lookup_barcode_parses_off(monkeypatch):
    body = {"status": 1, "product": {
        "product_name": "Oat Bar", "brands": "Acme, Other", "serving_quantity": "35",
        "nutriments": {"carbohydrates_100g": 60, "sugars_100g": 20, "proteins_100g": 8,
                       "fat_100g": 15, "fiber_100g": 6, "energy-kcal_100g": 420}}}
    monkeypatch.setattr(meals.requests, "get", lambda *a, **k: _Resp(200, body))
    p = meals.lookup_barcode("5000 1234 5678 9")
    assert p["name"] == "Acme Oat Bar" and p["serving_g"] == 35
    assert p["per_100g"]["carbs_g"] == 60 and p["per_100g"]["kcal"] == 420


def test_lookup_barcode_not_found_and_invalid(monkeypatch):
    monkeypatch.setattr(meals.requests, "get", lambda *a, **k: _Resp(200, {"status": 0}))
    assert meals.lookup_barcode("12345678") is None
    with pytest.raises(ValueError):
        meals.lookup_barcode("12")


class _FakeClient:
    def __init__(self, est):
        self.est, self.calls = est, []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        return types.SimpleNamespace(content=[types.SimpleNamespace(type="tool_use", input=self.est)])


def test_estimate_from_photo_forces_tool_and_cleans(tmp_path):
    client = _FakeClient({"description": "Porridge", "carbs_g": 52.345, "protein_g": 10,
                          "fat_g": 7, "kcal": 320, "confidence": "medium", "tags": ["Wholegrain"]})
    est = meals.estimate_from_photo(client, "m", "aGVsbG8=", "image/jpeg", "with honey")
    assert est["carbs_g"] == 52.3 and est["tags"] == ["wholegrain"]
    call = client.calls[0]
    assert call["tool_choice"] == {"type": "tool", "name": "record_meal_estimate"}
    assert call["messages"][0]["content"][0]["type"] == "image"
    assert "with honey" in call["messages"][0]["content"][1]["text"]


def test_estimate_rejects_bad_input():
    with pytest.raises(ValueError):
        meals.estimate_from_photo(_FakeClient({}), "m", "aGVsbG8=", "image/tiff")
    with pytest.raises(ValueError):
        meals.estimate_from_photo(_FakeClient({}), "m", "not base64!!", "image/jpeg")


# ---------------------------------------------------------------------------
# API + agent tool + cache
# ---------------------------------------------------------------------------
def test_api_meal_crud_and_summary_merge(api_client):
    day = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
    r = api_client.post("/api/meals", json={"user": "default", "eaten_at": f"{day}T19:45",
                                            "description": "Curry", "carbs_g": 90})
    assert r.status_code == 200, r.text
    mid = r.json()["id"]
    listing = api_client.get(f"/api/meals?user=default&start={day}&end={day}").json()
    assert listing["meals"][0]["description"] == "Curry"
    assert listing["staples"][0]["description"] == "Curry"
    dash = api_client.get(f"/api/dashboard?user=default&start={day}&end={day}").json()
    s = next(x for x in dash["summaries"] if x["date"] == day)
    assert s["last_meal_hour"] == 19.75 and s["meal_carbs_g"] == 90

    r = api_client.post("/api/meals", json={"user": "default", "id": mid, "eaten_at": f"{day}T20:00",
                                            "description": "Curry"})
    assert r.json()["meal"]["eaten_at"].endswith("20:00")
    assert api_client.delete(f"/api/meals/{mid}?user=default").json()["deleted"] is True
    assert api_client.delete(f"/api/meals/{mid}?user=default").status_code == 404


def test_api_meal_validation(api_client):
    assert api_client.post("/api/meals", json={"user": "default", "eaten_at": "nope",
                                               "description": "x"}).status_code == 400
    assert api_client.post("/api/meals", json={"user": "default", "id": 12345, "eaten_at": "2026-11-01T08:00",
                                               "description": "x"}).status_code == 404


def test_api_estimate_uses_agent_client(api_client, monkeypatch):
    from garmin_insights.web import app as app_module

    bundle = app_module._users.get("default")
    bundle.agent._client = _FakeClient({"description": "Toast", "carbs_g": 30, "protein_g": 5,
                                        "fat_g": 3, "kcal": 170, "confidence": "high"})
    bundle.agent._settings.claude_model = "test-model"
    r = api_client.post("/api/meals/estimate", json={"user": "default", "image_b64": "aGVsbG8="})
    assert r.status_code == 200, r.text
    assert r.json()["estimate"]["description"] == "Toast"
    assert bundle.agent._client.calls[0]["model"] == "test-model"


def test_agent_tool_get_meal_responses(sample_settings):
    from garmin_insights.db.memory import MemoryStore
    from garmin_insights.db.sqlite_repo import SqliteRepo
    from garmin_insights.tools.query_tools import QueryToolHandler, get_all_tools_anthropic

    db = sample_settings.sqlite_db_path
    handler = QueryToolHandler(SqliteRepo(sample_settings), MemoryStore(sample_settings), None)
    assert "get_meal_responses" in [t["name"] for t in get_all_tools_anthropic(handler)]
    assert "No meals" in handler.get_meal_responses()
    day0 = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=4)
    _seed_curve(db, day0, days=3, meal_hours=(12,))
    _meal(db, (day0 + timedelta(days=2, hours=12)).replace(tzinfo=None), "Pasta")
    out = json.loads(handler.get_meal_responses())
    assert out["meals"][0]["response"]["rise_mmol"] == pytest.approx(3.0, abs=0.1)
    assert "id" not in out["meals"][0]
