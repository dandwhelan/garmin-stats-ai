"""End-to-end /api/scale/frames for the conversational Fitdays full-BIA scan.

The browser relays the scale's frames; the server must answer with the
handshake to write once a person is standing on the scale, then persist the
settled reading with its segmental impedance and composition extras.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from garmin_insights.scales.fitdays import split_frame
from garmin_insights.web import app as app_module

HELLO = "07001d00aab3711e1a0025010a00010000000000000000000000540000000340002e"
LIVE_72_4 = "68000700a20100011ad0000e"
SETTLED_72_1 = "65000700a203000119a40003"
FRESH_RESULT = "52002300a76aa9a5d3250119a4000a011b0bf40c520ab90af500da0a5e0ad10959098e124de8bf36"


def _post(client, session, frames):
    return client.post("/api/scale/frames", json={
        "user": "default", "session_id": session, "adapter": "fitdays", "frames": frames,
    })


@pytest.fixture()
def profile(monkeypatch):
    real = app_module._resolve_user_identity

    def with_profile(settings):
        ident = dict(real(settings))
        ident.update(height_cm="185", age=38, biological_sex="male", name="Dan")
        return ident

    monkeypatch.setattr(app_module, "_resolve_user_identity", with_profile)
    monkeypatch.setattr(app_module, "_scale_sessions", {})


def test_descriptor_advertises_server_writes(api_client):
    adapters = api_client.get("/api/scale/adapters").json()["adapters"]
    assert adapters[0]["id"] == "fitdays" and adapters[0]["server_writes"] is True


def test_handshake_is_returned_once_someone_stands_on_the_scale(api_client, profile):
    body = _post(api_client, "s-arm", [HELLO]).json()
    assert body["state"] == "waiting" and body["writes"] == []

    body = _post(api_client, "s-arm", [LIVE_72_4]).json()
    assert body["state"] == "reading"
    types = [split_frame(bytes.fromhex(w))[1] for w in body["writes"]]
    assert types == [0xB0, 0xC0, 0xC1, 0xC0, 0xB6, 0xC0, 0xC1, 0xC0, 0xB0, 0xB0]

    # Polling again with nothing new must not resend the handshake.
    assert _post(api_client, "s-arm", []).json()["writes"] == []


def test_settled_result_persists_segments_and_acknowledges(api_client, profile, monkeypatch):
    from garmin_insights.scales import wla37

    monkeypatch.setattr(wla37, "compute_wla37", lambda *a, **k: {
        "metrics": {"bmi": 21.0, "body_fat_pct": 9.4, "body_water_pct": 66.5, "muscle_mass_kg": 60.9,
                    "bone_mass_kg": 4.4, "visceral_fat": 1.0, "metabolic_age": 35},
        "extras": {"composition_engine": "wla37", "bmr_kcal": 1780, "protein_pct": 18.1,
                   "segments": {"left_arm": {"fat_pct": 15.9, "muscle_pct": 109.3}}},
    })
    _post(api_client, "s-final", [HELLO, LIVE_72_4])
    body = _post(api_client, "s-final", [SETTLED_72_1, FRESH_RESULT]).json()

    assert body["state"] == "final"
    assert body["metrics"]["body_fat_pct"] == 9.4 and body["extras"]["bmr_kcal"] == 1780
    ack = [split_frame(bytes.fromhex(w)) for w in body["writes"]]
    assert [p[1] for p in ack] == [0xB0, 0xC0, 0xC1] and ack[0][2][0] == 0x3A

    day = datetime.now().strftime("%Y-%m-%d")
    rows = api_client.get("/api/scale-readings", params={
        "user": "default", "start": day, "end": (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d"),
    }).json()["readings"]
    row = next(r for r in rows if r["id"] == body["reading_id"])
    assert row["body_fat_pct"] == 9.4 and row["adapter"] == "fitdays"
    assert row["extras"]["segments"]["left_arm"]["muscle_pct"] == 109.3
    assert row["extras"]["impedance_segments_ohm"]["trunk"] == {"f1": 28.3, "f2": 21.8}


def test_missing_vendor_engine_still_saves_weight_and_impedance(api_client, profile, monkeypatch):
    from garmin_insights.scales import wla37

    monkeypatch.setattr(wla37, "compute_wla37", lambda *a, **k: None)
    monkeypatch.setattr(wla37, "engine_status", lambda: {"available": False, "error": "not installed", "path": "x"})
    _post(api_client, "s-noengine", [HELLO, LIVE_72_4])
    body = _post(api_client, "s-noengine", [SETTLED_72_1, FRESH_RESULT]).json()
    assert body["state"] == "final" and body["weight_kg"] == pytest.approx(72.1)
    assert "engine isn't installed" in body["note"]
    assert body["metrics"].get("bmi") is not None and "body_fat_pct" not in body["metrics"]


# ==================================================================
# BMR -> Garmin's basal_met (the one extra field the scan unlocked)
# ==================================================================
def test_bmr_is_uploaded_to_garmin_as_basal_met(api_client, monkeypatch):
    import garmin_insights.garmin_upload as gu

    sent: dict = {}
    monkeypatch.setattr(gu, "upload_body_composition", lambda *a, **kw: sent.update(kw))
    r = api_client.post("/api/weigh-in", json={
        "user": "default", "weight_kg": 72.0, "basal_met_kcal": 1780, "body_fat_pct": 9.4,
    })
    assert r.status_code == 200
    assert sent["basal_met_kcal"] == 1780 and sent["percent_fat"] == 9.4


def test_implausible_bmr_is_rejected_before_garmin(api_client):
    r = api_client.post("/api/weigh-in", json={
        "user": "default", "weight_kg": 72.0, "basal_met_kcal": 99999,
    })
    assert r.status_code == 400 and "basal_met_kcal" in r.json()["detail"]


def test_uploader_passes_bmr_through_to_garminconnect(monkeypatch, tmp_path):
    """The kwarg must survive into the Garmin SDK call, not just the endpoint."""
    from garmin_insights import garmin_upload as gu

    captured: dict = {}

    class FakeGarmin:
        def login(self, _token_dir): return None
        def connectapi(self, _path): return {"userName": "someone@example.com"}
        def add_body_composition(self, timestamp, **kw): captured.update(kw)

    monkeypatch.setitem(__import__("sys").modules, "garminconnect",
                        type("M", (), {"Garmin": FakeGarmin}))
    monkeypatch.setattr(gu, "_verify_token_owner", lambda *a, **k: None)
    gu.upload_body_composition(str(tmp_path), "someone@example.com",
                               weight_kg=72.0, basal_met_kcal=1780)
    assert round(captured["basal_met"]) == 1780
