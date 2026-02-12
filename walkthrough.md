# Walkthrough: Garmin Data Migration to SQLite

I have successfully migrated the data storage for both `garmin-grafana` and `garmin-insights` from InfluxDB and MariaDB to a single, centralized SQLite database. This simplifies the architecture, removes external database dependencies, and ensures data consistency between fetching and analysis.

## Changes Overview

### 1. `garmin-grafana` (Data Fetching)

- **New File:** `src/garmin_grafana/sqlite_manager.py`
  - Defines the SQLite schema for all Garmin metrics (Daily Stats, Sleep, Heart Rate, Activities, etc.).
  - Implements the `GarminDB` class to handle database connection and data insertion.
  - Uses `ON CONFLICT` clauses to efficiently update existing records (upsert).
- **Modified:** `src/garmin_grafana/garmin_fetch.py`
  - Removed InfluxDB client and configuration.
  - Integrated `GarminDB` for all data writing operations.
  - Updated the sync logic to check the latest data timestamp from SQLite instead of InfluxDB.
- **Cleanup:**
  - Removed `influxdb` dependencies from `pyproject.toml`.

### 2. `garmin-insights` (Analysis & Memory)

- **Modified:** `src/garmin_insights/config.py`
  - Replaced InfluxDB and MariaDB settings with a single `sqlite_db_path`.
- **New File:** `src/garmin_insights/db/sqlite_repo.py`
  - Implements `SqliteRepo` to query health metrics from the SQLite database.
  - Replaces the previous `InfluxRepo`.
  - Returns data as pandas DataFrames for analysis.
- **Modified:** `src/garmin_insights/db/memory.py`
  - Replaced `pymysql` (MariaDB) with `sqlite3`.
  - Implemented a SQLite-compatible schema for:
    - `daily_summaries`: Cached daily analysis.
    - `baselines`: Rolling averages and standard deviations.
    - `insights`: Discovered health insights.
    - `sessions`: Chat history and session summaries.
    - `user_profile`: User preferences and metadata.
- **Modified:** `src/garmin_insights/agent.py`, `cli.py`, `db/cache.py`, `tools/query_tools.py`
  - Updated all components to use `SqliteRepo` and the updated `MemoryStore`.
- **Cleanup:**
  - Removed `influxdb` and `pymysql` dependencies from `pyproject.toml`.

## Verification Results

I performed the following verification steps:

1. **Data Insertion Test:** Created a script mimicking `garmin_fetch.py` to insert sample metrics (Daily Stats, Sleep, Heart Rate) into a test SQLite database. Result: **PASSED**.
2. **Data Retrieval Test:** Created a script mimicking `garmin-insights` to query those metrics using `SqliteRepo`. Result: **PASSED**.
3. **Memory Persistence Test:** Verified `MemoryStore` operations (saving sessions, upserting daily summaries) using the same test database. Result: **PASSED**.
4. **Codebase Sweep:** Verified that all references to `InfluxRepo` and `pymysql` have been removed from the active code paths.

## Next Steps for User

- **Run the Fetcher:** Execute the updated `garmin_fetch.py` to start populating your new localized `garmin.db`.

  ```bash
  python src/garmin_grafana/garmin_fetch.py
  ```

- **Run Insights:** You can now run `garmin-insights` without needing MariaDB or InfluxDB services running in the background.

  ```bash
  garmin-insights
  ```

- **Database Location:** The default database path is `garmin.db` in the working directory, but this can be configured via the `SQLITE_DB_PATH` environment variable.
