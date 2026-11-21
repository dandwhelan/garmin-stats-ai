"""get_uncached_dates: empty summaries are retried hourly, not on every call."""

import sqlite3
import types

from garmin_insights.db.memory import MemoryStore


def _store(tmp_path):
    store = MemoryStore(types.SimpleNamespace(sqlite_db_path=str(tmp_path / "g.db")))
    store.initialise_schema()
    return store


def test_fresh_empty_summary_counts_as_cached(tmp_path):
    store = _store(tmp_path)
    store.upsert_daily_summary("2026-01-02", {"date": "2026-01-02", "is_complete": True})
    store.upsert_daily_summary("2026-01-03", {"restingHeartRate": 50.0})
    assert store.get_uncached_dates("2026-01-01", "2026-01-03") == ["2026-01-01"]


def test_stale_empty_summary_is_rebuilt(tmp_path):
    store = _store(tmp_path)
    store.upsert_daily_summary("2026-01-02", {"date": "2026-01-02", "is_complete": True})
    store.upsert_daily_summary("2026-01-03", {"restingHeartRate": 50.0})
    conn = sqlite3.connect(store.db_path)
    conn.execute("UPDATE daily_summaries SET computed_at = datetime('now', '-2 hours')")
    conn.commit()
    conn.close()
    # Stale empty row → retried; a real summary stays cached however old.
    assert store.get_uncached_dates("2026-01-02", "2026-01-03") == ["2026-01-02"]
