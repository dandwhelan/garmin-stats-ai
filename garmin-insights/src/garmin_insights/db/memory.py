"""SQLite-backed persistence for baselines, insights, sessions, and user profile."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from garmin_insights.config import Settings

logger = logging.getLogger(__name__)

# SQLite Schema
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS daily_summaries (
    date TEXT PRIMARY KEY,
    metric_json TEXT NOT NULL,
    lifestyle_json TEXT,
    computed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS baselines (
    metric_name TEXT PRIMARY KEY,
    avg_7d REAL,
    avg_30d REAL,
    std_7d REAL,
    std_30d REAL,
    min_30d REAL,
    max_30d REAL,
    latest_value REAL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS insights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_name TEXT NOT NULL,
    description TEXT NOT NULL,
    significance REAL,
    data_json TEXT,
    discovered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_rule ON insights (rule_name);
CREATE INDEX IF NOT EXISTS idx_discovered ON insights (discovered_at);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    summary TEXT NOT NULL,
    key_findings TEXT,
    messages_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS user_profile (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS chat_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT,
    event_date TEXT,
    tags_json TEXT,
    user_text TEXT,
    assistant_text TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_chat_memory_user_created ON chat_memory (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS daily_notes (
    date TEXT PRIMARY KEY,
    note TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

class MemoryStore:
    """SQLite-backed context and memory persistence."""

    def __init__(self, settings: Settings) -> None:
        self.db_path = settings.sqlite_db_path

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row  # Access columns by name
        return conn

    def close(self) -> None:
        pass  # Connections are per-call; nothing to close

    def initialise_schema(self) -> None:
        """Create tables if they don't exist."""
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.executescript(_SCHEMA_SQL)
            conn.commit()
            logger.info("SQLite schema initialised.")
        except Exception as e:
            logger.error(f"Failed to initialise schema: {e}")
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Daily Summaries
    # ------------------------------------------------------------------
    def upsert_daily_summary(
        self,
        date: str,
        metrics: dict[str, Any],
        lifestyle: dict[str, Any] | None = None,
    ) -> None:
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO daily_summaries (date, metric_json, lifestyle_json, computed_at)
                VALUES (?, ?, ?, datetime('now'))
                ON CONFLICT(date) DO UPDATE SET
                    metric_json = excluded.metric_json,
                    lifestyle_json = excluded.lifestyle_json,
                    computed_at = datetime('now')
                """,
                (date, json.dumps(metrics), json.dumps(lifestyle) if lifestyle else None),
            )
            conn.commit()
        finally:
            conn.close()

    def get_daily_summary(self, date: str) -> dict[str, Any] | None:
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT metric_json, lifestyle_json FROM daily_summaries WHERE date = ?",
                (date,),
            )
            row = cursor.fetchone()
            if row:
                result = json.loads(row["metric_json"]) if row["metric_json"] else {}
                if row["lifestyle_json"]:
                    result["lifestyle"] = json.loads(row["lifestyle_json"])
            else:
                result = None
        finally:
            conn.close()
        if result is not None:
            note = self.get_daily_note(date)
            if note:
                result["note"] = note
        return result

    def get_daily_summaries_range(
        self, start: str, end: str
    ) -> list[dict[str, Any]]:
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT date, metric_json, lifestyle_json FROM daily_summaries "
                "WHERE date >= ? AND date <= ? ORDER BY date",
                (start, end),
            )
            rows = cursor.fetchall()
            results = []
            for row in rows:
                entry = {"date": row["date"]}
                entry.update(json.loads(row["metric_json"]) if row["metric_json"] else {})
                if row["lifestyle_json"]:
                    entry["lifestyle"] = json.loads(row["lifestyle_json"])
                results.append(entry)
        finally:
            conn.close()

        # Merge in the user's free-text daily notes so they ride along with the
        # metrics everywhere summaries are consumed (dashboard, the AI's
        # get_daily_metrics tool, and the portable prompt). Notes live in a
        # separate table so they survive daily_summaries cache rebuilds.
        notes = self.get_daily_notes_range(start, end)
        if notes:
            for entry in results:
                note = notes.get(entry["date"])
                if note:
                    entry["note"] = note
        return results

    def get_uncached_dates(self, start: str, end: str) -> list[str]:
        """Return dates in range that don't have a valid cached daily summary.

        A summary is considered valid only if it contains at least one real
        metric beyond the base keys (date, is_complete). Empty summaries written
        by a broken cache build are treated as uncached so they get rebuilt.
        """
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            # A real summary has at least one metric from daily_stats or sleep_summary.
            # Check for restingHeartRate or sleepScore as proxies for a valid entry.
            cursor.execute(
                """
                SELECT date FROM daily_summaries
                WHERE date >= ? AND date <= ?
                  AND (
                    json_extract(metric_json, '$.restingHeartRate') IS NOT NULL
                    OR json_extract(metric_json, '$.sleepScore') IS NOT NULL
                    OR json_extract(metric_json, '$.totalSteps') IS NOT NULL
                  )
                """,
                (start, end),
            )
            cached = {row["date"] for row in cursor.fetchall()}
        finally:
            conn.close()

        d_start = datetime.strptime(start, "%Y-%m-%d").date()
        d_end = datetime.strptime(end, "%Y-%m-%d").date()
        all_dates = []
        d = d_start
        while d <= d_end:
            ds = d.isoformat()
            if ds not in cached:
                all_dates.append(ds)
            d += timedelta(days=1)
        return all_dates

    # ------------------------------------------------------------------
    # Daily notes (user-authored free text about a given day)
    # ------------------------------------------------------------------
    def upsert_daily_note(self, date: str, note: str) -> None:
        """Save (or replace) the free-text note for a calendar day.

        An empty/whitespace-only note deletes the row so blank days don't
        clutter the AI's context.
        """
        if note is None or not note.strip():
            self.delete_daily_note(date)
            return
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO daily_notes (date, note, updated_at)
                VALUES (?, ?, datetime('now'))
                ON CONFLICT(date) DO UPDATE SET
                    note = excluded.note,
                    updated_at = datetime('now')
                """,
                (date, note.strip()),
            )
            conn.commit()
        finally:
            conn.close()

    def delete_daily_note(self, date: str) -> None:
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM daily_notes WHERE date = ?", (date,))
            conn.commit()
        finally:
            conn.close()

    def get_daily_note(self, date: str) -> str | None:
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT note FROM daily_notes WHERE date = ?", (date,))
            row = cursor.fetchone()
            return row["note"] if row else None
        finally:
            conn.close()

    def get_daily_notes_range(self, start: str, end: str) -> dict[str, str]:
        """Return {date: note} for all days in the inclusive range that have one."""
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT date, note FROM daily_notes "
                "WHERE date >= ? AND date <= ? ORDER BY date",
                (start, end),
            )
            return {row["date"]: row["note"] for row in cursor.fetchall()}
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Baselines
    # ------------------------------------------------------------------
    def upsert_baseline(
        self, metric: str, avg_7d: float | None, avg_30d: float | None,
        std_7d: float | None, std_30d: float | None,
        min_30d: float | None, max_30d: float | None,
        latest: float | None,
    ) -> None:
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO baselines (metric_name, avg_7d, avg_30d, std_7d, std_30d,
                                       min_30d, max_30d, latest_value, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(metric_name) DO UPDATE SET
                    avg_7d = excluded.avg_7d, avg_30d = excluded.avg_30d,
                    std_7d = excluded.std_7d, std_30d = excluded.std_30d,
                    min_30d = excluded.min_30d, max_30d = excluded.max_30d,
                    latest_value = excluded.latest_value,
                    updated_at = datetime('now')
                """,
                (metric, avg_7d, avg_30d, std_7d, std_30d, min_30d, max_30d, latest),
            )
            conn.commit()
        finally:
            conn.close()

    def get_baselines(self) -> dict[str, dict[str, float | None]]:
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM baselines")
            return {
                row["metric_name"]: {
                    "avg_7d": row["avg_7d"],
                    "avg_30d": row["avg_30d"],
                    "std_7d": row["std_7d"],
                    "std_30d": row["std_30d"],
                    "min_30d": row["min_30d"],
                    "max_30d": row["max_30d"],
                    "latest_value": row["latest_value"],
                }
                for row in cursor.fetchall()
            }
        finally:
            conn.close()

    def get_baseline(self, metric: str) -> dict[str, float | None] | None:
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM baselines WHERE metric_name = ?", (metric,))
            row = cursor.fetchone()
            if row:
                return {
                    "avg_7d": row["avg_7d"],
                    "avg_30d": row["avg_30d"],
                    "std_7d": row["std_7d"],
                    "latest_value": row["latest_value"],
                }
            return None
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Insights
    # ------------------------------------------------------------------
    def save_insight(
        self, rule_name: str, description: str,
        significance: float | None = None,
        data: dict | None = None,
        suppress_hours: int = 168,
    ) -> int:
        conn = self._get_conn()
        expires = datetime.utcnow() + timedelta(hours=suppress_hours)
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO insights (rule_name, description, significance, data_json, expires_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (rule_name, description, significance, json.dumps(data) if data else None, expires.isoformat()),
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def get_recent_insights(self, hours: int = 168) -> list[dict[str, Any]]:
        conn = self._get_conn()
        since = datetime.utcnow() - timedelta(hours=hours)
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM insights WHERE discovered_at >= ? ORDER BY discovered_at DESC",
                (since.isoformat(),),
            )
            return [
                {
                    "rule_name": r["rule_name"],
                    "description": r["description"],
                    "significance": r["significance"],
                    "discovered_at": r["discovered_at"],
                }
                for r in cursor.fetchall()
            ]
        finally:
            conn.close()

    def is_insight_suppressed(self, rule_name: str) -> bool:
        """Check if an identical insight was recently reported."""
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            # SQLite 'now' is in UTC if we use default, but let's be consistent
            cursor.execute(
                "SELECT COUNT(*) as cnt FROM insights WHERE rule_name = ? AND expires_at > datetime('now')",
                (rule_name,),
            )
            row = cursor.fetchone()
            return (row["cnt"] or 0) > 0
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------
    def save_session(
        self, summary: str, key_findings: list[str] | None = None,
        messages: list[dict] | None = None,
    ) -> int:
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO sessions (summary, key_findings, messages_json, created_at)
                VALUES (?, ?, ?, datetime('now'))
                """,
                (
                    summary,
                    json.dumps(key_findings) if key_findings else None,
                    json.dumps(messages) if messages else None,
                ),
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def get_last_session(self) -> dict[str, Any] | None:
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM sessions ORDER BY created_at DESC LIMIT 1")
            row = cursor.fetchone()
            if row:
                return {
                    "summary": row["summary"],
                    "key_findings": json.loads(row["key_findings"]) if row["key_findings"] else [],
                    "created_at": row["created_at"],
                }
            return None
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # User profile
    # ------------------------------------------------------------------
    def set_profile(self, key: str, value: Any) -> None:
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO user_profile (key, value_json, updated_at)
                VALUES (?, ?, datetime('now'))
                ON CONFLICT(key) DO UPDATE SET 
                    value_json = excluded.value_json,
                    updated_at = datetime('now')
                """,
                (key, json.dumps(value)),
            )
            conn.commit()
        finally:
            conn.close()

    def get_profile(self, key: str) -> Any | None:
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT value_json FROM user_profile WHERE key = ?", (key,))
            row = cursor.fetchone()
            return json.loads(row["value_json"]) if row else None
        finally:
            conn.close()

    def get_all_profile(self) -> dict[str, Any]:
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT key, value_json FROM user_profile")
            return {row["key"]: json.loads(row["value_json"]) for row in cursor.fetchall()}
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Chat memory (lightweight longitudinal context)
    # ------------------------------------------------------------------
    def save_chat_memory(
        self,
        user_id: str,
        user_text: str,
        assistant_text: str | None = None,
        tags: list[str] | None = None,
        event_date: str | None = None,
    ) -> int:
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO chat_memory (user_id, event_date, tags_json, user_text, assistant_text, created_at)
                VALUES (?, ?, ?, ?, ?, datetime('now'))
                """,
                (
                    user_id,
                    event_date,
                    json.dumps(tags or []),
                    user_text,
                    assistant_text,
                ),
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def get_recent_chat_memory(self, user_id: str, limit: int = 25) -> list[dict[str, Any]]:
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT user_id, event_date, tags_json, user_text, assistant_text, created_at
                FROM chat_memory
                WHERE user_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (user_id, max(1, min(limit, 100))),
            )
            out: list[dict[str, Any]] = []
            for row in cursor.fetchall():
                out.append({
                    "user_id": row["user_id"],
                    "event_date": row["event_date"],
                    "tags": json.loads(row["tags_json"]) if row["tags_json"] else [],
                    "user_text": row["user_text"],
                    "assistant_text": row["assistant_text"],
                    "created_at": row["created_at"],
                })
            return out
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------
    def health_check(self) -> dict[str, Any]:
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            cursor.execute("SELECT COUNT(*) as cnt FROM daily_summaries")
            summary_count = cursor.fetchone()["cnt"]
            cursor.execute("SELECT COUNT(*) as cnt FROM baselines")
            baseline_count = cursor.fetchone()["cnt"]
            cursor.execute("SELECT COUNT(*) as cnt FROM insights")
            insight_count = cursor.fetchone()["cnt"]
            return {
                "connected": True,
                "daily_summaries": summary_count,
                "baselines": baseline_count,
                "insights": insight_count,
            }
        except Exception as e:
            return {"connected": False, "error": str(e)}
        finally:
            conn.close()
