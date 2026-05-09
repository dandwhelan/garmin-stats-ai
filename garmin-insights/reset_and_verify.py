from garmin_insights.config import get_settings
from garmin_insights.db.memory import MemoryStore
from garmin_insights.db.influxdb import InfluxRepo
from garmin_insights.db.cache import CacheBuilder
import logging

logging.basicConfig(level=logging.INFO)

settings = get_settings()
memory = MemoryStore(settings)
influx = InfluxRepo(settings)
cache = CacheBuilder(influx, memory)

print("1. Clearing daily_summaries table...")
conn = memory._get_conn()
with conn.cursor() as cursor:
    cursor.execute("TRUNCATE TABLE daily_summaries")
    conn.commit()

print("2. Rebuilding cache for last 60 days (this checks fixed logic)...")
# Since table is empty, refresh(60) will rebuild all dates
cache.refresh(days=60)

print("3. Verifying Sleep Data presence...")
summaries = memory.get_daily_summaries_range("2026-01-01", "2026-02-12")
sleep_count = sum(1 for s in summaries if s.get("sleepScore") is not None)
print(f"Total Cached Summaries: {len(summaries)}")
print(f"Summaries with Sleep Score: {sleep_count}")

if sleep_count > 0:
    print("SUCCESS: Sleep data is now being cached!")
else:
    print("FAILURE: Still no sleep data found.")

memory.close()
