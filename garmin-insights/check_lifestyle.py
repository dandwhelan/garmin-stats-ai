from garmin_insights.config import get_settings
from garmin_insights.db.influxdb import InfluxRepo
from datetime import datetime
import pandas as pd

settings = get_settings()
repo = InfluxRepo(settings)

print(f"Checking LifestyleJournal in {settings.influxdb_database}...")

try:
    # Query last 100 entries
    query = 'SELECT * FROM "LifestyleJournal" ORDER BY time DESC LIMIT 100'
    points = list(repo._client.query(query).get_points())
    
    if not points:
        print("No data found in LifestyleJournal measurement.")
    else:
        df = pd.DataFrame(points)
        print(f"\nFound {len(df)} entries.")
        print("\nColumns found:", df.columns.tolist())
        
        # Show what the important columns contain
        cols = [c for c in ['behavior', 'Behavior', 'category', 'Category', 'status', 'value'] if c in df.columns]
        print("\nFirst 10 rows:")
        print(df[cols + ['time']].head(10))
        
        # List all unique behaviors
        behaviors = set()
        for c in ['behavior', 'Behavior']:
            if c in df.columns:
                behaviors.update(df[c].dropna().unique())
        
        print("\nUnique behaviors found:", sorted(list(behaviors)))

except Exception as e:
    print(f"Error querying InfluxDB: {e}")
