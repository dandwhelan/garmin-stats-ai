"""Bluetooth-scale endpoints: adapter descriptors, the frame-relay session
buffer, local persistence, and the /api/weigh-in local-save-before-upload
change.

The browser is a dumb BLE pipe for these routes: it POSTs raw hex notify
frames and the server decodes/persists them, so every request body here is
untrusted input that must degrade to a 4xx, never a 500.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from garmin_insights.web import app as app_module

# A known-good Fitdays/Lefu "ac02" stable-weight frame for 70.5 kg:
# AC 02 | D0 D1 (=705 big-endian) | D2 D3 (=0,0) | STATUS(0xCA=stable) | CKSUM
# built the same way garmin_insights.scales.lefu._decode_ac02 verifies it.
_LEFU_STABLE_FRAME_70_5KG = "ac0202c10000ca8d"


def _iso(days_ago: int) -> str:
    return (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")


# ==================================================================
# GET /api/scale/adapters
# ==================================================================
def test_scale_adapters_lists_lefu(api_client):
    body = api_client.get("/api/scale/adapters").json()
    assert "adapters" in body
    ids = {a["id"] for a in body["adapters"]}
    assert "lefu" in ids
    lefu = next(a for a in body["adapters"] if a["id"] == "lefu")
    # Must be JSON-safe / round-trippable — service/notify/write UUIDs and a
    # handshake list are the minimum the browser needs to talk to the device.
    assert lefu["service_uuid"].startswith("0000ffb0")
    assert isinstance(lefu["handshake"], list) and lefu["handshake"]


def test_scale_adapters_needs_no_user(api_client):
    # No `user` param at all — this endpoint never touches a database.
    r = api_client.get("/api/scale/adapters")
    assert r.status_code == 200


# ==================================================================
# POST /api/scale/frames — validation
# ==================================================================
def test_scale_frames_unknown_adapter_is_400(api_client):
    r = api_client.post("/api/scale/frames", json={
        "user": "default", "session_id": "s1", "adapter": "not-a-scale", "frames": [],
    })
    assert r.status_code == 400


def test_scale_frames_unknown_user_is_404(api_client):
    r = api_client.post("/api/scale/frames", json={
        "user": "nobody", "session_id": "s1", "adapter": "lefu", "frames": [],
    })
    assert r.status_code == 404


def test_scale_frames_oversized_request_is_400(api_client, monkeypatch):
    monkeypatch.setattr(app_module, "_scale_sessions", {})
    r = api_client.post("/api/scale/frames", json={
        "user": "default", "session_id": "s1", "adapter": "lefu",
        "frames": ["00"] * (app_module._SCALE_FRAMES_PER_REQUEST_MAX + 1),
    })
    assert r.status_code == 400


# ==================================================================
# POST /api/scale/frames — decoding + persistence
# ==================================================================
def test_stable_reading_persists_and_returns_final(api_client, monkeypatch):
    monkeypatch.setattr(app_module, "_scale_sessions", {})
    r = api_client.post("/api/scale/frames", json={
        "user": "default", "session_id": "sess-final", "adapter": "lefu",
        "frames": [_LEFU_STABLE_FRAME_70_5KG],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "final"
    assert body["weight_kg"] == 70.5
    assert isinstance(body["reading_id"], int)

    # The session buffer is dropped once a stable reading is persisted.
    assert ("default", "sess-final") not in app_module._scale_sessions

    readings = api_client.get("/api/scale-readings", params={
        "user": "default", "start": _iso(1), "end": _iso(-1),
    }).json()["readings"]
    ids = {row["id"]: row for row in readings}
    assert body["reading_id"] in ids
    assert ids[body["reading_id"]]["weight_kg"] == 70.5
    assert ids[body["reading_id"]]["adapter"] == "lefu"


def test_non_weight_frame_is_waiting_not_final(api_client, monkeypatch):
    monkeypatch.setattr(app_module, "_scale_sessions", {})
    # A single junk byte matches no layout in the lefu adapter.
    r = api_client.post("/api/scale/frames", json={
        "user": "default", "session_id": "sess-waiting", "adapter": "lefu",
        "frames": ["00"],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "waiting"
    assert body["frame_count"] == 1
    # Buffer stays alive across calls for this session.
    assert ("default", "sess-waiting") in app_module._scale_sessions


def test_session_cap_evicts_rather_than_growing_unbounded(api_client, monkeypatch):
    monkeypatch.setattr(app_module, "_scale_sessions", {})
    max_live = app_module._SCALE_SESSION_MAX_LIVE
    for i in range(max_live + 10):
        r = api_client.post("/api/scale/frames", json={
            "user": "default", "session_id": f"sess-{i}", "adapter": "lefu",
            "frames": ["00"],
        })
        assert r.status_code == 200
    assert len(app_module._scale_sessions) <= max_live


# ==================================================================
# GET /api/scale-readings
# ==================================================================
def test_scale_readings_window_filtering_and_no_raw_frames(api_client):
    bundle = app_module._users.get("default")
    old_id = bundle.agent._memory.save_scale_reading(
        taken_at="2020-01-05T08:00:00", adapter="manual", weight_kg=61.0,
        raw_frames="deadbeef",
    )
    recent_id = bundle.agent._memory.save_scale_reading(
        taken_at=datetime.now().isoformat(timespec="seconds"),
        adapter="manual", weight_kg=62.5, raw_frames="cafefeed",
    )

    body = api_client.get("/api/scale-readings", params={
        "user": "default", "start": _iso(1), "end": _iso(-1),
    }).json()
    ids = {row["id"] for row in body["readings"]}
    assert recent_id in ids
    assert old_id not in ids
    for row in body["readings"]:
        assert "raw_frames" not in row


# ==================================================================
# POST /api/weigh-in — local-first persistence
# ==================================================================
def test_weigh_in_saves_local_row_even_when_garmin_upload_fails(api_client, monkeypatch):
    import garmin_insights.garmin_upload as gu_module

    def _raise(*args, **kwargs):
        raise gu_module.GarminUploadError("token expired")

    monkeypatch.setattr(gu_module, "upload_body_composition", _raise)

    r = api_client.post("/api/weigh-in", json={"user": "default", "weight_kg": 71.2})
    assert r.status_code == 502

    bundle = app_module._users.get("default")
    rows = bundle.agent._memory.get_scale_readings(_iso(1), _iso(-1))
    manual_rows = [row for row in rows if row["adapter"] == "manual" and row["weight_kg"] == 71.2]
    assert manual_rows, "manual weigh-in row must survive a failed Garmin upload"


def test_weigh_in_marks_existing_reading_uploaded_on_success(api_client, monkeypatch):
    import garmin_insights.garmin_upload as gu_module

    monkeypatch.setattr(gu_module, "upload_body_composition", lambda *a, **k: None)

    bundle = app_module._users.get("default")
    reading_id = bundle.agent._memory.save_scale_reading(
        taken_at=datetime.now().isoformat(timespec="seconds"),
        adapter="lefu", weight_kg=73.0,
    )
    r = api_client.post("/api/weigh-in", json={
        "user": "default", "weight_kg": 73.0, "reading_id": reading_id,
    })
    assert r.status_code == 200
    assert r.json()["reading_id"] == reading_id
    rows = {row["id"]: row for row in bundle.agent._memory.get_scale_readings(_iso(1), _iso(-1))}
    assert rows[reading_id]["uploaded_to_garmin"] is True


# ==================================================================
# POST /api/weigh-in — bounds validation (previously uncovered)
# ==================================================================
def test_weigh_in_rejects_out_of_range_weight(api_client):
    r = api_client.post("/api/weigh-in", json={"user": "default", "weight_kg": 5})
    assert r.status_code == 400


def test_weigh_in_rejects_out_of_range_body_fat_pct(api_client):
    r = api_client.post("/api/weigh-in", json={
        "user": "default", "weight_kg": 70.0, "body_fat_pct": 150,
    })
    assert r.status_code == 400


def test_weigh_in_rejects_malformed_timestamp(api_client):
    r = api_client.post("/api/weigh-in", json={
        "user": "default", "weight_kg": 70.0, "timestamp": "not-a-date",
    })
    assert r.status_code == 400
