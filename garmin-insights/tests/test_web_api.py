"""Web API endpoint behaviour.

The dashboard renders whatever these return, and in multi-user mode they are
the boundary between two people's health data. The isolation tests below
assert responses came from the *right database*, not merely that the request
succeeded — a routing bug returns 200 either way.

Cycle sex-gating lives in test_fixtures_smoke.py; this file covers routing,
range handling, and the read/write endpoints.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest


def _iso(days_ago: int) -> str:
    return (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")


# ==================================================================
# Multi-user isolation
# ==================================================================
@pytest.mark.parametrize("uid", ["alice", "bob"])
def test_dashboard_serves_each_users_own_database(two_user_client, uid):
    client, marks = two_user_client
    body = client.get("/api/dashboard", params={"user": uid, "start": _iso(10),
                                                "end": _iso(1)}).json()
    steps = {s.get("totalSteps") for s in body["summaries"]}
    assert steps == {marks[uid]}, f"{uid} was served another user's data: {steps}"


def test_the_two_users_never_see_the_same_numbers(two_user_client):
    client, marks = two_user_client
    params = {"start": _iso(10), "end": _iso(1)}

    alice = client.get("/api/dashboard", params={**params, "user": "alice"}).json()
    bob = client.get("/api/dashboard", params={**params, "user": "bob"}).json()

    assert {s["totalSteps"] for s in alice["summaries"]} == {marks["alice"]}
    assert {s["totalSteps"] for s in bob["summaries"]} == {marks["bob"]}


def test_interleaved_requests_do_not_cross_contaminate(two_user_client):
    """A per-user cache keyed wrongly (or a shared module global) shows up as
    the second requester inheriting the first's data."""
    client, marks = two_user_client
    params = {"start": _iso(10), "end": _iso(1)}

    for uid in ("alice", "bob", "alice", "bob", "bob", "alice"):
        body = client.get("/api/dashboard", params={**params, "user": uid}).json()
        assert {s["totalSteps"] for s in body["summaries"]} == {marks[uid]}


def test_health_reports_each_users_own_identity(two_user_client):
    client, _marks = two_user_client
    alice = client.get("/api/health", params={"user": "alice"}).json()
    bob = client.get("/api/health", params={"user": "bob"}).json()

    assert alice["user"]["email"] == "alice@example.com"
    assert bob["user"]["email"] == "bob@example.com"
    assert alice["user"]["name"] != bob["user"]["name"]


def test_notes_written_for_one_user_are_invisible_to_the_other(two_user_client):
    """Notes are free-text personal journal entries — the most sensitive thing
    in the database."""
    client, _marks = two_user_client
    day = _iso(2)

    client.post("/api/notes", json={"user": "alice", "date": day,
                                    "note": "alice private note"})

    alice = client.get("/api/notes", params={"user": "alice", "start": day, "end": day})
    bob = client.get("/api/notes", params={"user": "bob", "start": day, "end": day})

    assert alice.json()["entries"].get(day) == "alice private note"
    assert bob.json()["entries"] == {}


def test_user_list_exposes_ids_but_not_database_paths(two_user_client):
    client, _marks = two_user_client
    body = client.get("/api/users").json()
    assert {u["id"] for u in body["users"]} == {"alice", "bob"}
    assert all("db_path" not in u for u in body["users"])


@pytest.mark.parametrize("path", [
    "/api/dashboard", "/api/visualizations", "/api/lifestyle",
    "/api/environment", "/api/menstrual", "/api/notes",
])
def test_unknown_user_is_rejected_everywhere(two_user_client, path):
    client, _marks = two_user_client
    r = client.get(path, params={"user": "mallory"})
    assert r.status_code == 404
    assert "mallory" in r.json()["detail"]


# ==================================================================
# Range resolution
# ==================================================================
def test_omitted_range_falls_back_to_the_endpoint_default(api_client):
    body = api_client.get("/api/dashboard", params={"user": "default"}).json()
    rng = body["date_range"]
    span = (datetime.strptime(rng["end"], "%Y-%m-%d")
            - datetime.strptime(rng["start"], "%Y-%m-%d")).days
    assert 0 < span <= 366


@pytest.mark.parametrize("params", [
    {"start": "not-a-date"},
    {"end": "13/02/2026"},
    {"start": "2026-02-30"},
])
def test_malformed_dates_are_rejected_with_400(api_client, params):
    r = api_client.get("/api/dashboard", params={"user": "default", **params})
    assert r.status_code == 400
    assert "expected YYYY-MM-DD" in r.json()["detail"] or "Invalid" in r.json()["detail"]


def test_inverted_range_is_rejected(api_client):
    r = api_client.get("/api/dashboard", params={"user": "default",
                                                 "start": _iso(1), "end": _iso(30)})
    assert r.status_code == 400
    assert "must not be after" in r.json()["detail"]


def test_absurd_range_is_clamped_not_served_whole(api_client):
    """The dashboard feeds this window into build_range, which constructs every
    missing day — an unbounded span turns one request into thousands of cache
    builds."""
    body = api_client.get("/api/dashboard", params={
        "user": "default", "start": "1990-01-01", "end": _iso(0)}).json()
    rng = body["date_range"]
    span = (datetime.strptime(rng["end"], "%Y-%m-%d")
            - datetime.strptime(rng["start"], "%Y-%m-%d")).days
    assert span <= 366


def test_far_future_end_is_clamped_to_the_forecast_window(api_client):
    """A small future window is allowed on purpose — environment_daily carries
    Open-Meteo forecast days the pollen chart asks for."""
    body = api_client.get("/api/dashboard", params={
        "user": "default", "start": _iso(10), "end": "2099-01-01"}).json()
    end = datetime.strptime(body["date_range"]["end"], "%Y-%m-%d")
    assert end <= datetime.now() + timedelta(days=8)


# ==================================================================
# Notes read/write
# ==================================================================
def test_note_roundtrip(api_client):
    day = _iso(3)
    assert api_client.post("/api/notes", json={
        "user": "default", "date": day, "note": "ran 10k"}).status_code == 200
    assert api_client.get("/api/notes", params={
        "user": "default", "start": day, "end": day}).json()["entries"][day] == "ran 10k"


def test_empty_note_clears_the_day(api_client):
    day = _iso(3)
    api_client.post("/api/notes", json={"user": "default", "date": day, "note": "x"})
    api_client.post("/api/notes", json={"user": "default", "date": day, "note": ""})
    assert api_client.get("/api/notes", params={
        "user": "default", "start": day, "end": day}).json()["entries"] == {}


def test_note_overwrites_rather_than_appends(api_client):
    day = _iso(3)
    api_client.post("/api/notes", json={"user": "default", "date": day, "note": "first"})
    api_client.post("/api/notes", json={"user": "default", "date": day, "note": "second"})
    assert api_client.get("/api/notes", params={
        "user": "default", "start": day, "end": day}).json()["entries"][day] == "second"


def test_note_reaches_the_daily_summary_the_agent_reads(api_client):
    """Notes are first-hand ground truth for explaining metric deviations, so
    they must ride into the summaries, not sit in a side table."""
    day = _iso(3)
    api_client.post("/api/notes", json={
        "user": "default", "date": day, "note": "food poisoning"})

    body = api_client.get("/api/dashboard", params={
        "user": "default", "start": day, "end": day}).json()
    entry = next(s for s in body["summaries"] if s["date"] == day)
    assert entry["note"] == "food poisoning"


# ==================================================================
# Other read endpoints
# ==================================================================
def test_environment_endpoint_returns_rows_for_a_located_user(api_client, sample_dates):
    start, end = sample_dates
    body = api_client.get("/api/environment", params={
        "user": "default", "start": start, "end": end}).json()
    assert body.get("available") is not False
    assert body["entries"]


def test_environment_recovery_returns_correlations(api_client, sample_dates):
    start, end = sample_dates
    body = api_client.get("/api/environment/recovery", params={
        "user": "default", "start": start, "end": end}).json()
    assert body.get("available") is not False
    for pair in body.get("correlations", []):
        # Every r must carry its sample size and a BH-corrected flag.
        assert "n" in pair and "significant" in pair


def test_intraday_heatmap_returns_a_24h_matrix(api_client):
    body = api_client.get("/api/intraday/heatmap", params={
        "user": "default", "metric": "stress", "days": 7}).json()
    assert body["hours"] == list(range(24))
    assert body["dates"]


def test_intraday_heatmap_whitelists_the_metric(api_client):
    """The metric selects an interpolated table+column in the SQL, so the
    whitelist is the injection boundary, not a nicety. An unknown metric is
    answered in-band (200 + error body) rather than by building a query."""
    body = api_client.get("/api/intraday/heatmap", params={
        "user": "default", "metric": "not_a_metric", "days": 7}).json()
    assert "unknown metric" in body["error"]
    assert set(body["available"]) == {"stress", "body_battery", "heart_rate", "steps"}
    assert "matrix" not in body


@pytest.mark.parametrize("payload", [
    "stress; DROP TABLE daily_stats--",
    "stress UNION SELECT 1",
    "../../etc/passwd",
])
def test_intraday_heatmap_refuses_injection_payloads(api_client, sample_db, payload):
    import sqlite3

    body = api_client.get("/api/intraday/heatmap", params={
        "user": "default", "metric": payload, "days": 7}).json()
    assert "error" in body

    conn = sqlite3.connect(sample_db)
    still_there = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='daily_stats'").fetchone()[0]
    conn.close()
    assert still_there == 1


def test_activity_export_returns_markdown_without_coordinates(api_client, sample_db):
    """The 'Copy stats' button shares this text; GPS coordinates are location
    data and are deliberately excluded."""
    import sqlite3

    conn = sqlite3.connect(sample_db)
    activity_id = conn.execute(
        "SELECT activity_id FROM activity_summary LIMIT 1").fetchone()[0]
    conn.close()

    body = api_client.get(f"/api/activities/{activity_id}/export",
                          params={"user": "default"}).json()
    text = body.get("markdown", body.get("text", ""))
    assert text
    assert "latitude" not in text.lower()
    assert "longitude" not in text.lower()


def test_missing_activity_export_is_a_404(api_client):
    r = api_client.get("/api/activities/999999999/export", params={"user": "default"})
    assert r.status_code == 404


def test_scans_listing_is_empty_before_any_scan_runs(api_client):
    body = api_client.get("/api/scans", params={"user": "default"}).json()
    assert body["reports"] == []
    assert body["user"] == "default"


def test_index_serves_the_dashboard_html(api_client):
    r = api_client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


# ==================================================================
# NaN scrubbing
# ==================================================================
def test_responses_never_contain_bare_nan(api_client, sample_dates):
    """pandas surfaces gaps as NaN and json.dumps emits a bare `NaN` token,
    which is invalid JSON and breaks the frontend parse. SafeJSONResponse
    scrubs it globally."""
    start, end = sample_dates
    for path in ("/api/dashboard", "/api/visualizations", "/api/lifestyle",
                 "/api/environment/recovery"):
        raw = api_client.get(path, params={"user": "default", "start": start,
                                           "end": end}).text
        assert "NaN" not in raw, f"{path} leaked a NaN token"
        assert "Infinity" not in raw, f"{path} leaked an Infinity token"
