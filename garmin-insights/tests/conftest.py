"""Shared pytest fixtures — a small temp SQLite DB with fitness-marker tables."""

from __future__ import annotations

import sqlite3
import types

import pytest


@pytest.fixture
def fitness_db(tmp_path):
    """A temp SQLite DB populated with the slow-moving fitness-marker tables.

    Schema mirrors garmin-grafana's sqlite_manager.py. Timestamps are ISO8601
    so SqliteRepo._query parses them as a datetime index.
    """
    db = tmp_path / "garmin_test.db"
    conn = sqlite3.connect(db)
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE vo2_max (time TEXT, device TEXT, vo2_max_value REAL,
                              vo2_max_value_cycling REAL, PRIMARY KEY (time, device));
        CREATE TABLE fitness_age (time TEXT, device TEXT, chronological_age REAL,
                                  fitness_age REAL, achievable_fitness_age REAL,
                                  PRIMARY KEY (time, device));
        CREATE TABLE endurance_score (time TEXT, device TEXT, endurance_score INTEGER,
                                      PRIMARY KEY (time, device));
        CREATE TABLE hill_score (time TEXT, device TEXT, strength_score INTEGER,
                                 endurance_score INTEGER, overall_score INTEGER,
                                 PRIMARY KEY (time, device));
        CREATE TABLE race_predictions (time TEXT, device TEXT, time_5k REAL,
                                       time_10k REAL, time_half_marathon REAL,
                                       time_marathon REAL, PRIMARY KEY (time, device));
        """
    )
    cur.executemany(
        "INSERT INTO vo2_max VALUES (?,?,?,?)",
        [
            ("2026-06-01T12:00:00", "dev", 48.0, 42.0),
            ("2026-06-10T12:00:00", "dev", 49.0, 43.0),
            ("2026-06-20T12:00:00", "dev", 50.0, 43.0),
        ],
    )
    cur.executemany(
        "INSERT INTO fitness_age VALUES (?,?,?,?,?)",
        [
            ("2026-06-01T12:00:00", "dev", 40.0, 35.0, 33.0),
            ("2026-06-20T12:00:00", "dev", 40.0, 34.0, 33.0),
        ],
    )
    cur.executemany(
        "INSERT INTO endurance_score VALUES (?,?,?)",
        [("2026-06-01T12:00:00", "dev", 7200), ("2026-06-20T12:00:00", "dev", 7400)],
    )
    cur.executemany(
        "INSERT INTO hill_score VALUES (?,?,?,?,?)",
        [("2026-06-20T12:00:00", "dev", 60, 55, 58)],
    )
    cur.executemany(
        "INSERT INTO race_predictions VALUES (?,?,?,?,?,?)",
        [
            ("2026-06-01T12:00:00", "dev", 1500.0, 3120.0, 6900.0, 14400.0),
            ("2026-06-20T12:00:00", "dev", 1470.0, 3060.0, 6780.0, 14100.0),
        ],
    )
    conn.commit()
    conn.close()
    return str(db)


@pytest.fixture
def fake_settings(fitness_db):
    """Minimal stand-in for Settings — SqliteRepo only reads sqlite_db_path."""
    return types.SimpleNamespace(sqlite_db_path=fitness_db)
