"""One-off script: re-fetch a date range for a single user.
Run as:
  source users/dan.env && python backfill_dates.py [START] [END]
  source users/helen.env && python backfill_dates.py [START] [END]
Defaults to 2026-05-10 .. 2026-05-10 if no args.
"""
import os, sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__),
    'garmin-grafana/src'))

start = sys.argv[1] if len(sys.argv) > 1 else '2026-05-10'
end   = sys.argv[2] if len(sys.argv) > 2 else start

import garmin_grafana.garmin_fetch as gf
gf.garmin_obj = gf.garmin_login()
gf.fetch_write_bulk(start, end)
print(f"Backfill done for {start} .. {end}")
