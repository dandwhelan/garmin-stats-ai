"""Tests for DB integrity checks + online backups."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from garmin_insights.db.maintenance import (
    backup_db,
    integrity_check,
    run_maintenance,
)


def _make_db(path: str, rows: int = 5) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany("INSERT INTO t (v) VALUES (?)", [(f"row{i}",) for i in range(rows)])
    conn.commit()
    conn.close()


def test_integrity_check_ok(tmp_path):
    db = tmp_path / "ok.db"
    _make_db(str(db))
    result = integrity_check(str(db))
    assert result["ok"] is True
    assert result["result"] == ["ok"]


def test_quick_check_ok(tmp_path):
    db = tmp_path / "ok.db"
    _make_db(str(db))
    assert integrity_check(str(db), quick=True)["ok"] is True


def test_backup_creates_verified_copy(tmp_path):
    db = tmp_path / "src.db"
    _make_db(str(db), rows=10)
    backup_dir = tmp_path / "backups"
    result = backup_db(str(db), str(backup_dir), keep=7)
    assert result["ok"] is True
    assert Path(result["path"]).exists()
    assert result["bytes"] > 0
    # The backup is a real, queryable copy with all rows.
    conn = sqlite3.connect(result["path"])
    count = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    conn.close()
    assert count == 10


def test_backup_rotation_keeps_n(tmp_path):
    db = tmp_path / "src.db"
    _make_db(str(db))
    backup_dir = tmp_path / "backups"
    # Pre-seed more "old" backups than we want to keep.
    backup_dir.mkdir()
    for stamp in ("20200101-000000", "20200102-000000", "20200103-000000"):
        (backup_dir / f"src-{stamp}.db").write_bytes(b"old")
    result = backup_db(str(db), str(backup_dir), keep=2)
    remaining = sorted(backup_dir.glob("src-*.db"))
    assert len(remaining) == 2
    assert result["rotated"]  # something was deleted
    # The freshest (just-created) backup must survive rotation.
    assert Path(result["path"]).exists()


def test_run_maintenance_reports_all_steps(tmp_path):
    db = tmp_path / "src.db"
    _make_db(str(db))
    report = run_maintenance(str(db), backup_dir=str(tmp_path / "bk"), keep=3)
    assert report["integrity"]["ok"] is True
    assert report["backup"]["ok"] is True


def test_run_maintenance_default_backup_dir(tmp_path):
    db = tmp_path / "src.db"
    _make_db(str(db))
    report = run_maintenance(str(db))
    assert Path(report["backup_dir"]).name == "backups"
    assert Path(report["backup"]["path"]).exists()
