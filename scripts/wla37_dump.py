"""Dump the Fitdays engine's raw output for a saved scan, to locate unmapped fields.

Re-runs the vendor engine on the stored impedance of the latest scale reading
and prints every plausible double/int in the output struct with its offset.
Pass --find to highlight offsets whose value matches a figure from the Fitdays
app (e.g. WHR 0.81).

    .venv/bin/python scripts/wla37_dump.py --user dan --find 0.81
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import struct

from garmin_insights.config import settings_for_user
from garmin_insights.scales.wla37 import engine_status, run_raw
from garmin_insights.web.app import _resolve_user_identity


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", required=True)
    ap.add_argument("--reading-id", type=int, help="scale_readings.id (default: latest)")
    ap.add_argument("--find", type=float, action="append", default=[],
                    help="value from the Fitdays app to locate (repeatable)")
    args = ap.parse_args()

    settings = settings_for_user(args.user)
    ident = _resolve_user_identity(settings)
    con = sqlite3.connect(settings.sqlite_db_path)
    sql = "SELECT id, taken_at, weight_kg, extras_json FROM scale_readings "
    row = con.execute(sql + ("WHERE id = ?" if args.reading_id else "ORDER BY taken_at DESC LIMIT 1"),
                      (args.reading_id,) if args.reading_id else ()).fetchone()
    if not row:
        raise SystemExit("no scale reading found")
    rid, taken_at, weight, extras_json = row
    imp = json.loads(extras_json or "{}").get("impedance_raw")
    if not imp:
        raise SystemExit(f"reading {rid} has no stored impedance_raw")
    raw = run_raw(weight, float(ident["height_cm"]), ident["age"], ident["biological_sex"], imp)
    if raw is None:
        raise SystemExit(f"engine unavailable: {engine_status()}")

    print(f"reading {rid} @ {taken_at}  weight={weight}  struct={len(raw)} bytes")
    for off in range(0, len(raw) - 7, 4):
        d = struct.unpack_from("<d", raw, off)[0] if off % 8 == 0 else None
        i = struct.unpack_from("<i", raw, off)[0]
        hit = any(d is not None and abs(d - f) < 0.015 for f in args.find) or \
            any(abs(i - f) < 0.5 for f in args.find)
        d_ok = d is not None and d == d and 1e-3 < abs(d) < 1e5
        if d_ok or 0 < abs(i) < 100000 or hit:
            print(f"{'>>' if hit else '  '} 0x{off:03X}  "
                  f"double={d:.4f}  " if d_ok else f"{'>>' if hit else '  '} 0x{off:03X}  ",
                  f"int={i}", sep="")


if __name__ == "__main__":
    main()
