"""Tests for MemoryStore's local-first scale_readings table."""

import pytest

from garmin_insights.config import Settings
from garmin_insights.db.memory import MemoryStore


@pytest.fixture
def store(tmp_path):
    settings = Settings(sqlite_db_path=str(tmp_path / "garmin.db"), anthropic_api_key="test")
    ms = MemoryStore(settings)
    ms.initialise_schema()
    return ms


def test_save_and_get_round_trip(store):
    reading_id = store.save_scale_reading(
        taken_at="2026-08-29T08:15:00",
        adapter="fitdays",
        weight_kg=78.4,
        impedance_ohm=512.3,
        bmi=23.1,
        body_fat_pct=18.2,
        body_water_pct=56.1,
        muscle_mass_kg=60.2,
        bone_mass_kg=3.1,
        visceral_fat=8.0,
        metabolic_age=24.0,
        physique_rating=5.0,
        extras={"channel_impedances": [500, 520, 505, 515]},
    )
    assert isinstance(reading_id, int)

    rows = store.get_scale_readings("2026-08-29", "2026-08-29")
    assert len(rows) == 1
    row = rows[0]
    assert row["taken_at"] == "2026-08-29T08:15:00"
    assert row["date"] == "2026-08-29"
    assert row["adapter"] == "fitdays"
    assert row["weight_kg"] == 78.4
    assert row["extras"] == {"channel_impedances": [500, 520, 505, 515]}
    assert row["uploaded_to_garmin"] is False
    assert "raw_frames" not in row


def test_upsert_on_duplicate_taken_at_replaces_and_preserves_upload_flag(store):
    first_id = store.save_scale_reading(
        taken_at="2026-08-29T08:15:00", adapter="fitdays", weight_kg=78.4,
    )
    store.mark_scale_reading_uploaded(first_id)

    second_id = store.save_scale_reading(
        taken_at="2026-08-29T08:15:00", adapter="fitdays", weight_kg=78.9,
    )
    assert second_id == first_id

    rows = store.get_scale_readings("2026-08-29", "2026-08-29")
    assert len(rows) == 1
    assert rows[0]["weight_kg"] == 78.9
    assert rows[0]["uploaded_to_garmin"] is True  # not clobbered by the retry


def test_date_derived_from_full_iso_datetime(store):
    store.save_scale_reading(
        taken_at="2026-08-29T23:59:59", adapter="fitdays", weight_kg=78.0,
    )
    rows = store.get_scale_readings("2026-08-29", "2026-08-29")
    assert len(rows) == 1
    assert rows[0]["date"] == "2026-08-29"


def test_date_derived_from_bare_date(store):
    store.save_scale_reading(
        taken_at="2026-08-29", adapter="fitdays", weight_kg=78.0,
    )
    rows = store.get_scale_readings("2026-08-29", "2026-08-29")
    assert len(rows) == 1
    assert rows[0]["date"] == "2026-08-29"


def test_unparseable_taken_at_raises_value_error(store):
    with pytest.raises(ValueError):
        store.save_scale_reading(taken_at="not-a-date", adapter="fitdays", weight_kg=78.0)


def test_corrupt_extras_json_becomes_empty_dict(store, tmp_path):
    reading_id = store.save_scale_reading(
        taken_at="2026-08-29T08:15:00", adapter="fitdays", weight_kg=78.0,
    )
    # Corrupt the stored JSON directly.
    import sqlite3

    conn = sqlite3.connect(store.db_path)
    conn.execute(
        "UPDATE scale_readings SET extras_json = ? WHERE id = ?",
        ("{not valid json", reading_id),
    )
    conn.commit()
    conn.close()

    rows = store.get_scale_readings("2026-08-29", "2026-08-29")
    assert rows[0]["extras"] == {}


def test_raw_frames_omitted_by_default_present_with_include_raw(store):
    store.save_scale_reading(
        taken_at="2026-08-29T08:15:00",
        adapter="fitdays",
        weight_kg=78.0,
        raw_frames="AA:BB:CC",
    )
    default_rows = store.get_scale_readings("2026-08-29", "2026-08-29")
    assert "raw_frames" not in default_rows[0]

    raw_rows = store.get_scale_readings("2026-08-29", "2026-08-29", include_raw=True)
    assert raw_rows[0]["raw_frames"] == "AA:BB:CC"


def test_mark_scale_reading_uploaded_flips_flag(store):
    reading_id = store.save_scale_reading(
        taken_at="2026-08-29T08:15:00", adapter="fitdays", weight_kg=78.0,
    )
    rows = store.get_scale_readings("2026-08-29", "2026-08-29")
    assert rows[0]["uploaded_to_garmin"] is False

    store.mark_scale_reading_uploaded(reading_id)

    rows = store.get_scale_readings("2026-08-29", "2026-08-29")
    assert rows[0]["uploaded_to_garmin"] is True


def test_range_query_excludes_out_of_window_days(store):
    store.save_scale_reading(
        taken_at="2026-08-01T08:00:00", adapter="fitdays", weight_kg=79.0,
    )
    store.save_scale_reading(
        taken_at="2026-08-29T08:00:00", adapter="fitdays", weight_kg=78.0,
    )
    rows = store.get_scale_readings("2026-08-28", "2026-08-30")
    assert len(rows) == 1
    assert rows[0]["date"] == "2026-08-29"


def test_get_latest_scale_reading_for_day(store):
    store.save_scale_reading(
        taken_at="2026-08-29T07:00:00", adapter="fitdays", weight_kg=78.5,
    )
    store.save_scale_reading(
        taken_at="2026-08-29T08:00:00", adapter="fitdays", weight_kg=78.2,
    )
    latest = store.get_latest_scale_reading("2026-08-29")
    assert latest is not None
    assert latest["weight_kg"] == 78.2

    assert store.get_latest_scale_reading("2026-08-30") is None
