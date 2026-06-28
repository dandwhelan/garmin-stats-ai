"""SQLite integrity checks and online backups.

The production database lives on a Raspberry Pi that suffers frequent unclean
shutdowns (power instability), which is exactly how SQLite files get corrupted.
The fetcher writes while the web server reads, so this module:

* runs ``PRAGMA integrity_check`` to detect corruption early, and
* takes a *consistent* backup using SQLite's online backup API
  (``Connection.backup``), which is safe to run against a live database being
  written to concurrently — unlike a plain file copy.

Backups are integrity-checked after they are written (so a corrupt source is
caught) and rotated to keep the most recent N. Designed to be run from cron,
e.g. nightly::

    0 4 * * * garmin-insights maintain --backup-dir /home/dan/garmin-backups
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Wait up to 30s for the fetcher's write lock rather than failing immediately.
_BUSY_TIMEOUT_MS = 30_000


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=_BUSY_TIMEOUT_MS / 1000)
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    return conn


def integrity_check(db_path: str, quick: bool = False) -> dict:
    """Run ``PRAGMA integrity_check`` (or ``quick_check``) on a database.

    Returns ``{"ok": bool, "result": [...], "db": path}``. ``ok`` is True when
    SQLite reports the single row ``"ok"``. Never raises for a corrupt DB — the
    corruption is reported in the result list — but will raise if the file is
    missing or unreadable.
    """
    pragma = "quick_check" if quick else "integrity_check"
    conn = _connect(db_path)
    try:
        rows = conn.execute(f"PRAGMA {pragma}").fetchall()
    finally:
        conn.close()
    result = [r[0] for r in rows]
    return {"ok": result == ["ok"], "result": result, "db": db_path}


def checkpoint_wal(db_path: str) -> None:
    """Fold the WAL back into the main DB (TRUNCATE checkpoint).

    Keeps the ``-wal`` file from growing without bound and ensures a subsequent
    backup captures all committed pages. Best-effort: a busy DB may defer it.
    """
    conn = _connect(db_path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.OperationalError as exc:  # locked/busy — not fatal
        logger.warning("WAL checkpoint skipped: %s", exc)
    finally:
        conn.close()


def backup_db(db_path: str, backup_dir: str, keep: int = 7) -> dict:
    """Take a consistent online backup and rotate to the most recent ``keep``.

    Returns ``{"path": str, "ok": bool, "bytes": int, "rotated": [removed...]}``
    where ``ok`` is the integrity-check result of the *backup copy*.
    """
    src_path = Path(db_path)
    out_dir = Path(backup_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = out_dir / f"{src_path.stem}-{ts}.db"

    src = _connect(db_path)
    try:
        dst = sqlite3.connect(str(dest))
        try:
            # Online backup — pages are copied under a read lock the engine
            # manages, so concurrent fetcher writes stay safe and consistent.
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    check = integrity_check(str(dest))
    rotated = _rotate(out_dir, src_path.stem, keep)
    size = dest.stat().st_size if dest.exists() else 0
    if not check["ok"]:
        logger.error("Backup %s FAILED integrity check: %s", dest, check["result"])
    else:
        logger.info("Backup written: %s (%d bytes)", dest, size)
    return {"path": str(dest), "ok": check["ok"], "bytes": size, "rotated": rotated}


def _rotate(out_dir: Path, stem: str, keep: int) -> list[str]:
    """Delete all but the newest ``keep`` ``<stem>-*.db`` backups."""
    backups = sorted(out_dir.glob(f"{stem}-*.db"))
    removed: list[str] = []
    if keep > 0 and len(backups) > keep:
        for old in backups[:-keep]:
            try:
                old.unlink()
                removed.append(str(old))
            except OSError as exc:
                logger.warning("Could not remove old backup %s: %s", old, exc)
    return removed


def run_maintenance(
    db_path: str, backup_dir: str | None = None, keep: int = 7
) -> dict:
    """Integrity-check the live DB, checkpoint the WAL, then back it up.

    ``backup_dir`` defaults to a ``backups/`` folder alongside the database.
    Returns a combined report dict suitable for logging or CLI display.
    """
    if backup_dir is None:
        backup_dir = str(Path(db_path).resolve().parent / "backups")

    report: dict = {"db": db_path, "backup_dir": backup_dir}
    report["integrity"] = integrity_check(db_path)
    if not report["integrity"]["ok"]:
        # Still take a backup — a partially-corrupt DB may have salvageable rows
        # and a snapshot is better than nothing — but flag it loudly.
        logger.error(
            "Live DB %s failed integrity check: %s",
            db_path, report["integrity"]["result"],
        )
    checkpoint_wal(db_path)
    report["backup"] = backup_db(db_path, backup_dir, keep=keep)
    return report
