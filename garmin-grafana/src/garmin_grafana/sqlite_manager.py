import sqlite3
import json
import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)

class GarminDB:
    def __init__(self, db_path="garmin.db"):
        self.db_path = db_path
        self._init_db()

    def _get_conn(self):
        return sqlite3.connect(self.db_path)

    def _init_db(self):
        """Initialize the database schema."""
        conn = self._get_conn()
        cursor = conn.cursor()

        # Daily Stats
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS daily_stats (
                date TEXT PRIMARY KEY,
                time TEXT,
                device TEXT,
                active_kilocalories REAL,
                bmr_kilocalories REAL,
                total_steps INTEGER,
                total_distance_meters REAL,
                highly_active_seconds INTEGER,
                active_seconds INTEGER,
                sedentary_seconds INTEGER,
                sleeping_seconds INTEGER,
                moderate_intensity_minutes INTEGER,
                vigorous_intensity_minutes INTEGER,
                floors_ascended_meters REAL,
                floors_descended_meters REAL,
                floors_ascended INTEGER,
                floors_descended INTEGER,
                min_heart_rate INTEGER,
                max_heart_rate INTEGER,
                resting_heart_rate INTEGER,
                min_avg_heart_rate INTEGER,
                max_avg_heart_rate INTEGER,
                stress_duration INTEGER,
                rest_stress_duration INTEGER,
                activity_stress_duration INTEGER,
                uncategorized_stress_duration INTEGER,
                total_stress_duration INTEGER,
                low_stress_duration INTEGER,
                medium_stress_duration INTEGER,
                high_stress_duration INTEGER,
                stress_percentage REAL,
                rest_stress_percentage REAL,
                activity_stress_percentage REAL,
                uncategorized_stress_percentage REAL,
                low_stress_percentage REAL,
                medium_stress_percentage REAL,
                high_stress_percentage REAL,
                body_battery_charged_value INTEGER,
                body_battery_drained_value INTEGER,
                body_battery_highest_value INTEGER,
                body_battery_lowest_value INTEGER,
                body_battery_during_sleep INTEGER,
                body_battery_at_wake_time INTEGER,
                average_spo2 REAL,
                lowest_spo2 REAL
            )
        """)

        # Sleep Summary
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sleep_summary (
                date TEXT PRIMARY KEY, -- Derived from end time usually
                time TEXT,
                sleep_start TEXT,
                sleep_end TEXT,
                device TEXT,
                sleep_time_seconds INTEGER,
                deep_sleep_seconds INTEGER,
                light_sleep_seconds INTEGER,
                rem_sleep_seconds INTEGER,
                awake_sleep_seconds INTEGER,
                average_spo2_value REAL,
                lowest_spo2_value REAL,
                highest_spo2_value REAL,
                average_respiration_value REAL,
                lowest_respiration_value REAL,
                highest_respiration_value REAL,
                awake_count INTEGER,
                avg_sleep_stress REAL,
                sleep_score INTEGER,
                restless_moments_count INTEGER,
                avg_overnight_hrv REAL,
                body_battery_change INTEGER,
                resting_heart_rate INTEGER
            )
        """)

        # Intraday tables - Generic Structure where possible
        # We use a combined table for simple time-series if appropriate, or separate ones for clarity
        
        # Sleep Intraday (Complex, stores various metrics)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sleep_intraday (
                time TEXT,
                device TEXT,
                activity_level INTEGER,
                activity_seconds INTEGER,
                stage_level INTEGER,
                stage_seconds INTEGER,
                restless_value INTEGER,
                spo2_reading INTEGER,
                respiration_value INTEGER,
                heart_rate INTEGER,
                stress_value INTEGER,
                body_battery INTEGER,
                hrv_value INTEGER,
                PRIMARY KEY (time, device) -- Might have collisions if multiple types at same second?
                -- Actually InfluxDB distinguishes by measurement/tags. 
                -- We might need separate tables or a 'type' column if timestamps overlap.
            )
        """)
        # Since SleepIntraday in fetcher has overlapping timestamps for different data types (e.g. stress vs heart rate),
        # A single table with nullable columns works, BUT we need to handle upserts correctly.
        # OR we just treat them as separate events.
        # Let's use separate tables for the high volume clean metrics, matching Influx measurements usually.

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS heart_rate_intraday (
                time TEXT,
                device TEXT,
                heart_rate INTEGER,
                PRIMARY KEY (time, device)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS steps_intraday (
                time TEXT,
                device TEXT,
                steps_count INTEGER,
                PRIMARY KEY (time, device)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS stress_intraday (
                time TEXT,
                device TEXT,
                stress_level INTEGER,
                PRIMARY KEY (time, device)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS body_battery_intraday (
                time TEXT,
                device TEXT,
                body_battery_level INTEGER,
                PRIMARY KEY (time, device)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS breathing_rate_intraday (
                time TEXT,
                device TEXT,
                breathing_rate REAL,
                PRIMARY KEY (time, device)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS hrv_intraday (
                time TEXT,
                device TEXT,
                hrv_value INTEGER,
                PRIMARY KEY (time, device)
            )
        """)

        # Body Composition
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS body_composition (
                time TEXT,
                device TEXT,
                weight REAL,
                bmi REAL,
                body_fat REAL,
                body_water REAL,
                bone_mass REAL,
                muscle_mass REAL,
                physique_rating REAL,
                visceral_fat REAL,
                PRIMARY KEY (time, device)
            )
        """)

        # Activity Summary
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS activity_summary (
                activity_id INTEGER PRIMARY KEY,
                time TEXT,
                device TEXT,
                activity_name TEXT,
                activity_type TEXT,
                distance REAL,
                elapsed_duration REAL,
                moving_duration REAL,
                average_speed REAL,
                max_speed REAL,
                calories REAL,
                bmr_calories REAL,
                average_hr REAL,
                max_hr REAL,
                location_name TEXT,
                lap_count INTEGER,
                hr_time_in_zone_1 REAL,
                hr_time_in_zone_2 REAL,
                hr_time_in_zone_3 REAL,
                hr_time_in_zone_4 REAL,
                hr_time_in_zone_5 REAL
            )
        """)
        
        # Activity Details (GPS)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS activity_gps (
                time TEXT,
                activity_id INTEGER,
                device TEXT,
                latitude REAL,
                longitude REAL,
                altitude REAL,
                distance REAL,
                duration_seconds REAL,
                heart_rate REAL,
                speed REAL,
                grade_adjusted_speed REAL,
                running_efficiency REAL,
                cadence INTEGER,
                fractional_cadence REAL,
                temperature REAL,
                accumulated_power INTEGER,
                power INTEGER,
                activity_name TEXT,
                FOREIGN KEY(activity_id) REFERENCES activity_summary(activity_id)
            )
        """)
        # Index for faster querying by activity
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_activity_gps_id ON activity_gps(activity_id)")


        # Lifestyle Journal
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS lifestyle_journal (
                date TEXT,
                behavior TEXT,
                category TEXT,
                status INTEGER,
                value REAL,
                device TEXT,
                PRIMARY KEY (date, behavior)
            )
        """)

        # Training/Wellness Metrics
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS training_status (
                time TEXT,
                device TEXT,
                training_status TEXT,
                training_status_feedback_phrase TEXT,
                weekly_training_load REAL,
                fitness_trend TEXT,
                acwr_percent REAL,
                daily_training_load_acute REAL,
                daily_training_load_chronic REAL,
                max_training_load_chronic REAL,
                min_training_load_chronic REAL,
                daily_acute_chronic_workload_ratio REAL,
                heat_acclimation_percentage REAL,
                altitude_acclimation_percentage REAL,
                heat_trend TEXT,
                altitude_trend TEXT,
                current_altitude REAL,
                PRIMARY KEY (time, device)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS training_readiness (
                time TEXT,
                device TEXT,
                level TEXT,
                score INTEGER,
                sleep_score INTEGER,
                sleep_score_factor_percent INTEGER,
                recovery_time INTEGER,
                recovery_time_factor_percent INTEGER,
                acwr_factor_percent INTEGER,
                acute_load REAL,
                stress_history_factor_percent INTEGER,
                hrv_factor_percent INTEGER,
                PRIMARY KEY (time, device)
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS lactate_threshold (
                time TEXT,
                device TEXT,
                speed_threshold REAL,
                heart_rate_threshold INTEGER,
                sport TEXT,
                PRIMARY KEY (time, sport, device)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS hill_score (
                time TEXT,
                device TEXT,
                strength_score INTEGER,
                endurance_score INTEGER,
                hill_score_classification_id INTEGER,
                overall_score INTEGER,
                hill_score_feedback_phrase_id INTEGER,
                vo2_max_precise_value REAL,
                PRIMARY KEY (time, device)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS race_predictions (
                time TEXT,
                device TEXT,
                time_5k REAL,
                time_10k REAL,
                time_half_marathon REAL,
                time_marathon REAL,
                PRIMARY KEY (time, device)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS fitness_age (
                time TEXT,
                device TEXT,
                chronological_age REAL,
                fitness_age REAL,
                achievable_fitness_age REAL,
                PRIMARY KEY (time, device)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS vo2_max (
                time TEXT,
                device TEXT,
                vo2_max_value REAL,
                vo2_max_value_cycling REAL,
                PRIMARY KEY (time, device)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS endurance_score (
                time TEXT,
                device TEXT,
                endurance_score INTEGER,
                PRIMARY KEY (time, device)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS blood_pressure (
                time TEXT,
                device TEXT,
                systolic INTEGER,
                diastolic INTEGER,
                pulse INTEGER,
                source TEXT,
                PRIMARY KEY (time, device)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS hydration (
                time TEXT,
                device TEXT,
                value_in_ml INTEGER,
                sweat_loss_in_ml INTEGER,
                goal_in_ml INTEGER,
                activity_intake_in_ml INTEGER,
                PRIMARY KEY (time, device)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS solar_intensity (
                time TEXT,
                device TEXT,
                solar_utilization INTEGER,
                activity_time_gain_ms INTEGER,
                PRIMARY KEY (time, device)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS environment_daily (
                date TEXT PRIMARY KEY,
                latitude REAL,
                longitude REAL,
                temp_max_c REAL,
                temp_min_c REAL,
                temp_mean_c REAL,
                apparent_temp_max_c REAL,
                precipitation_mm REAL,
                wind_max_kmh REAL,
                humidity_mean REAL,
                uv_index_max REAL,
                pm25 REAL,
                pm10 REAL,
                o3 REAL,
                no2 REAL,
                european_aqi REAL,
                pollen_alder REAL,
                pollen_birch REAL,
                pollen_grass REAL,
                pollen_mugwort REAL,
                pollen_olive REAL,
                pollen_ragweed REAL,
                fetched_at TEXT
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS menstrual_cycle (
                date TEXT PRIMARY KEY,
                time TEXT,
                device TEXT,
                cycle_start_date TEXT,
                current_day_of_cycle INTEGER,
                current_cycle_phase TEXT,
                cycle_length INTEGER,
                predicted_cycle_length INTEGER,
                period_length INTEGER,
                menstrual_flow TEXT,
                pregnancy_status TEXT,
                symptoms TEXT,
                mood TEXT,
                notes TEXT,
                raw_json TEXT
            )
        """)

        # Lightweight migrations for databases created before a column existed.
        # CREATE TABLE IF NOT EXISTS won't add columns to a pre-existing table,
        # so ALTER each new column in, ignoring "duplicate column name" errors.
        _added_columns = {
            "training_status": [
                ("heat_acclimation_percentage", "REAL"),
                ("altitude_acclimation_percentage", "REAL"),
                ("heat_trend", "TEXT"),
                ("altitude_trend", "TEXT"),
                ("current_altitude", "REAL"),
            ],
        }
        for _table, _cols in _added_columns.items():
            for _col, _type in _cols:
                try:
                    cursor.execute(f"ALTER TABLE {_table} ADD COLUMN {_col} {_type}")
                except sqlite3.OperationalError:
                    pass  # column already exists

        conn.commit()

        conn.close()

    def insert_points(self, points):
        """Insert a list of points (dicts) into appropriate tables."""
        if not points:
            return

        conn = self._get_conn()
        cursor = conn.cursor()

        try:
            for point in points:
                measurement = point.get('measurement')
                timestamp = point.get('time')
                tags = point.get('tags', {})
                fields = point.get('fields', {})
                device = tags.get('Device', 'Unknown')

                if measurement == 'DailyStats':
                    self._upsert(cursor, 'daily_stats', {
                        'date': timestamp[:10], # Assuming YYYY-MM-DD from the timestamp
                        'time': timestamp,
                        'device': device,
                        'active_kilocalories': fields.get('activeKilocalories'),
                        'bmr_kilocalories': fields.get('bmrKilocalories'),
                        'total_steps': fields.get('totalSteps'),
                        'total_distance_meters': fields.get('totalDistanceMeters'),
                        'highly_active_seconds': fields.get('highlyActiveSeconds'),
                        'active_seconds': fields.get('activeSeconds'),
                        'sedentary_seconds': fields.get('sedentarySeconds'),
                        'sleeping_seconds': fields.get('sleepingSeconds'),
                        'moderate_intensity_minutes': fields.get('moderateIntensityMinutes'),
                        'vigorous_intensity_minutes': fields.get('vigorousIntensityMinutes'),
                        'floors_ascended_meters': fields.get('floorsAscendedInMeters'),
                        'floors_descended_meters': fields.get('floorsDescendedInMeters'),
                        'floors_ascended': fields.get('floorsAscended'),
                        'floors_descended': fields.get('floorsDescended'),
                        'min_heart_rate': fields.get('minHeartRate'),
                        'max_heart_rate': fields.get('maxHeartRate'),
                        'resting_heart_rate': fields.get('restingHeartRate'),
                        'min_avg_heart_rate': fields.get('minAvgHeartRate'),
                        'max_avg_heart_rate': fields.get('maxAvgHeartRate'),
                        'stress_duration': fields.get('stressDuration'),
                        'rest_stress_duration': fields.get('restStressDuration'),
                        'activity_stress_duration': fields.get('activityStressDuration'),
                        'uncategorized_stress_duration': fields.get('uncategorizedStressDuration'),
                        'total_stress_duration': fields.get('totalStressDuration'),
                        'low_stress_duration': fields.get('lowStressDuration'),
                        'medium_stress_duration': fields.get('mediumStressDuration'),
                        'high_stress_duration': fields.get('highStressDuration'),
                        'stress_percentage': fields.get('stressPercentage'),
                        'rest_stress_percentage': fields.get('restStressPercentage'),
                        'activity_stress_percentage': fields.get('activityStressPercentage'),
                        'uncategorized_stress_percentage': fields.get('uncategorizedStressPercentage'),
                        'low_stress_percentage': fields.get('lowStressPercentage'),
                        'medium_stress_percentage': fields.get('mediumStressPercentage'),
                        'high_stress_percentage': fields.get('highStressPercentage'),
                        'body_battery_charged_value': fields.get('bodyBatteryChargedValue'),
                        'body_battery_drained_value': fields.get('bodyBatteryDrainedValue'),
                        'body_battery_highest_value': fields.get('bodyBatteryHighestValue'),
                        'body_battery_lowest_value': fields.get('bodyBatteryLowestValue'),
                        'body_battery_during_sleep': fields.get('bodyBatteryDuringSleep'),
                        'body_battery_at_wake_time': fields.get('bodyBatteryAtWakeTime'),
                        'average_spo2': fields.get('averageSpo2'),
                        'lowest_spo2': fields.get('lowestSpo2'),
                    }, ['date'])
                
                elif measurement == 'SleepSummary':
                    self._upsert(cursor, 'sleep_summary', {
                        'date': timestamp[:10],
                        'time': timestamp,
                        'sleep_start': fields.get('sleepStartTime'),
                        'sleep_end':   fields.get('sleepEndTime'),
                        'device': device,
                        'sleep_time_seconds': fields.get('sleepTimeSeconds'),
                        'deep_sleep_seconds': fields.get('deepSleepSeconds'),
                        'light_sleep_seconds': fields.get('lightSleepSeconds'),
                        'rem_sleep_seconds': fields.get('remSleepSeconds'),
                        'awake_sleep_seconds': fields.get('awakeSleepSeconds'),
                        'average_spo2_value': fields.get('averageSpO2Value'),
                        'lowest_spo2_value': fields.get('lowestSpO2Value'),
                        'highest_spo2_value': fields.get('highestSpO2Value'),
                        'average_respiration_value': fields.get('averageRespirationValue'),
                        'lowest_respiration_value': fields.get('lowestRespirationValue'),
                        'highest_respiration_value': fields.get('highestRespirationValue'),
                        'awake_count': fields.get('awakeCount'),
                        'avg_sleep_stress': fields.get('avgSleepStress'),
                        'sleep_score': fields.get('sleepScore'),
                        'restless_moments_count': fields.get('restlessMomentsCount'),
                        'avg_overnight_hrv': fields.get('avgOvernightHrv'),
                        'body_battery_change': fields.get('bodyBatteryChange'),
                        'resting_heart_rate': fields.get('restingHeartRate'),
                    }, ['date'])

                elif measurement == 'HeartRateIntraday':
                    self._upsert(cursor, 'heart_rate_intraday', {
                        'time': timestamp,
                        'device': device,
                        'heart_rate': fields.get('HeartRate')
                    }, ['time', 'device'])

                elif measurement == 'StepsIntraday':
                    self._upsert(cursor, 'steps_intraday', {
                        'time': timestamp,
                        'device': device,
                        'steps_count': fields.get('StepsCount')
                    }, ['time', 'device'])

                elif measurement == 'StressIntraday':
                    self._upsert(cursor, 'stress_intraday', {
                        'time': timestamp,
                        'device': device,
                        'stress_level': fields.get('stressLevel')
                    }, ['time', 'device'])

                elif measurement == 'BodyBatteryIntraday':
                    self._upsert(cursor, 'body_battery_intraday', {
                        'time': timestamp,
                        'device': device,
                        'body_battery_level': fields.get('BodyBatteryLevel')
                    }, ['time', 'device'])

                elif measurement == 'BreathingRateIntraday':
                    self._upsert(cursor, 'breathing_rate_intraday', {
                        'time': timestamp,
                        'device': device,
                        'breathing_rate': fields.get('BreathingRate')
                    }, ['time', 'device'])

                elif measurement == 'HRV_Intraday':
                    self._upsert(cursor, 'hrv_intraday', {
                        'time': timestamp,
                        'device': device,
                        'hrv_value': fields.get('hrvValue')
                    }, ['time', 'device'])

                elif measurement == 'BodyComposition':
                    self._upsert(cursor, 'body_composition', {
                        'time': timestamp,
                        'device': device,
                        'weight': fields.get('weight'),
                        'bmi': fields.get('bmi'),
                        'body_fat': fields.get('bodyFat'),
                        'body_water': fields.get('bodyWater'),
                        'bone_mass': fields.get('boneMass'),
                        'muscle_mass': fields.get('muscleMass'),
                        'physique_rating': fields.get('physiqueRating'),
                        'visceral_fat': fields.get('visceralFat'),
                    }, ['time', 'device'])

                elif measurement == 'ActivitySummary':
                    # The fetcher emits two points per activity: a START row with
                    # the real fields, and an END marker row carrying only
                    # activityName="END" / activityType="No Activity". In the
                    # original InfluxDB schema those are distinct time-series
                    # points; here they share activity_id, so writing the END
                    # marker would overwrite the real row via upsert.
                    if fields.get('activityName') == 'END':
                        continue
                    self._upsert(cursor, 'activity_summary', {
                        'activity_id': fields.get('Activity_ID'),
                        'time': timestamp,
                        'device': device,
                        'activity_name': fields.get('activityName'),
                        'activity_type': fields.get('activityType'),
                        'distance': fields.get('distance'),
                        'elapsed_duration': fields.get('elapsedDuration'),
                        'moving_duration': fields.get('movingDuration'),
                        'average_speed': fields.get('averageSpeed'),
                        'max_speed': fields.get('maxSpeed'),
                        'calories': fields.get('calories'),
                        'bmr_calories': fields.get('bmrCalories'),
                        'average_hr': fields.get('averageHR'),
                        'max_hr': fields.get('maxHR'),
                        'location_name': fields.get('locationName'),
                        'lap_count': fields.get('lapCount'),
                        'hr_time_in_zone_1': fields.get('hrTimeInZone_1'),
                        'hr_time_in_zone_2': fields.get('hrTimeInZone_2'),
                        'hr_time_in_zone_3': fields.get('hrTimeInZone_3'),
                        'hr_time_in_zone_4': fields.get('hrTimeInZone_4'),
                        'hr_time_in_zone_5': fields.get('hrTimeInZone_5'),
                    }, ['activity_id'])
                
                elif measurement == 'ActivityGPS':
                    # We usually don't update GPS points individually by key, simpler to just insert.
                    # But if re-fetching, we want to avoid dups. 
                    # For bulk GPS, might be better to check existence or delete-insert for session.
                    # Here we just do insert for now, assuming conflict resolution on PK if we had one.
                    # Since we don't have unique constraint on time+activity (multiple points same second?), just insert.
                    # Actually, we should probably rely on a composite index or let duplication happen if not strict.
                    # For efficiency, standard insert.
                    cursor.execute("""
                        INSERT INTO activity_gps (
                            time, activity_id, device, latitude, longitude, altitude, distance, 
                            duration_seconds, heart_rate, speed, grade_adjusted_speed, running_efficiency, 
                            cadence, fractional_cadence, temperature, accumulated_power, power, activity_name
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        timestamp, tags.get('ActivityID'), device, 
                        fields.get('Latitude'), fields.get('Longitude'), fields.get('Altitude'), fields.get('Distance'),
                        fields.get('DurationSeconds'), fields.get('HeartRate'), fields.get('Speed'), fields.get('GradeAdjustedSpeed'),
                        fields.get('RunningEfficiency'), fields.get('Cadence'), fields.get('Fractional_Cadence'),
                        fields.get('Temperature'), fields.get('Accumulated_Power'), fields.get('Power'), fields.get('ActivityName')
                    ))

                elif measurement == 'LifestyleJournal':
                   self._upsert(cursor, 'lifestyle_journal', {
                       'date': timestamp[:10],
                       'behavior': tags.get('behavior'),
                       'category': tags.get('category'),
                       'status': fields.get('status'),
                       'value': fields.get('value'),
                       'device': device
                   }, ['date', 'behavior'])

                elif measurement == 'TrainingStatus':
                    self._upsert(cursor, 'training_status', {
                        'time': timestamp,
                        'device': device,
                        'training_status': fields.get('trainingStatus'),
                        'training_status_feedback_phrase': fields.get('trainingStatusFeedbackPhrase'),
                        'weekly_training_load': fields.get('weeklyTrainingLoad'),
                        'fitness_trend': fields.get('fitnessTrend'),
                        'acwr_percent': fields.get('acwrPercent'),
                        'daily_training_load_acute': fields.get('dailyTrainingLoadAcute'),
                        'daily_training_load_chronic': fields.get('dailyTrainingLoadChronic'),
                        'max_training_load_chronic': fields.get('maxTrainingLoadChronic'),
                        'min_training_load_chronic': fields.get('minTrainingLoadChronic'),
                        'daily_acute_chronic_workload_ratio': fields.get('dailyAcuteChronicWorkloadRatio'),
                        'heat_acclimation_percentage': fields.get('heatAcclimationPercentage'),
                        'altitude_acclimation_percentage': fields.get('altitudeAcclimationPercentage'),
                        'heat_trend': fields.get('heatTrend'),
                        'altitude_trend': fields.get('altitudeTrend'),
                        'current_altitude': fields.get('currentAltitude'),
                    }, ['time', 'device'])

                elif measurement == 'TrainingReadiness':
                    self._upsert(cursor, 'training_readiness', {
                        'time': timestamp,
                        'device': device,
                        'level': fields.get('level'),
                        'score': fields.get('score'),
                        'sleep_score': fields.get('sleepScore'),
                        'sleep_score_factor_percent': fields.get('sleepScoreFactorPercent'),
                        'recovery_time': fields.get('recoveryTime'),
                        'recovery_time_factor_percent': fields.get('recoveryTimeFactorPercent'),
                        'acwr_factor_percent': fields.get('acwrFactorPercent'),
                        'acute_load': fields.get('acuteLoad'),
                        'stress_history_factor_percent': fields.get('stressHistoryFactorPercent'),
                        'hrv_factor_percent': fields.get('hrvFactorPercent'),
                    }, ['time', 'device'])

                elif measurement == 'HillScore':
                    self._upsert(cursor, 'hill_score', {
                        'time': timestamp,
                        'device': device,
                        'strength_score': fields.get('strengthScore'),
                        'endurance_score': fields.get('enduranceScore'),
                        'hill_score_classification_id': fields.get('hillScoreClassificationId'),
                        'overall_score': fields.get('overallScore'),
                        'hill_score_feedback_phrase_id': fields.get('hillScoreFeedbackPhraseId'),
                        'vo2_max_precise_value': fields.get('vo2MaxPreciseValue'),
                    }, ['time', 'device'])

                elif measurement == 'RacePredictions':
                    self._upsert(cursor, 'race_predictions', {
                        'time': timestamp,
                        'device': device,
                        'time_5k': fields.get('time5K'),
                        'time_10k': fields.get('time10K'),
                        'time_half_marathon': fields.get('timeHalfMarathon'),
                        'time_marathon': fields.get('timeMarathon'),
                    }, ['time', 'device'])

                elif measurement == 'FitnessAge':
                    self._upsert(cursor, 'fitness_age', {
                        'time': timestamp,
                        'device': device,
                        'chronological_age': fields.get('chronologicalAge'),
                        'fitness_age': fields.get('fitnessAge'),
                        'achievable_fitness_age': fields.get('achievableFitnessAge'),
                    }, ['time', 'device'])

                elif measurement == 'VO2_Max':
                    self._upsert(cursor, 'vo2_max', {
                        'time': timestamp,
                        'device': device,
                        'vo2_max_value': fields.get('VO2_max_value'),
                        'vo2_max_value_cycling': fields.get('VO2_max_value_cycling'),
                    }, ['time', 'device'])

                elif measurement == 'EnduranceScore':
                    self._upsert(cursor, 'endurance_score', {
                        'time': timestamp,
                        'device': device,
                        'endurance_score': fields.get('EnduranceScore'),
                    }, ['time', 'device'])

                elif measurement == 'BloodPressure':
                    self._upsert(cursor, 'blood_pressure', {
                        'time': timestamp,
                        'device': device,
                        'systolic': fields.get('Systolic'),
                        'diastolic': fields.get('Diastolic'),
                        'pulse': fields.get('Pulse'),
                        'source': tags.get('Source'),
                    }, ['time', 'device'])

                elif measurement == 'Hydration':
                    self._upsert(cursor, 'hydration', {
                        'time': timestamp,
                        'device': device,
                        'value_in_ml': fields.get('ValueInML'),
                        'sweat_loss_in_ml': fields.get('SweatLossInML'),
                        'goal_in_ml': fields.get('GoalInML'),
                        'activity_intake_in_ml': fields.get('ActivityIntakeInML'),
                    }, ['time', 'device'])

                elif measurement == 'SolarIntensity':
                    self._upsert(cursor, 'solar_intensity', {
                        'time': timestamp,
                        'device': device,
                        'solar_utilization': fields.get('solarUtilization'),
                        'activity_time_gain_ms': fields.get('activityTimeGainMs'),
                    }, ['time', 'device'])

                elif measurement == 'MenstrualCycle':
                    self._upsert(cursor, 'menstrual_cycle', {
                        'date': fields.get('date'),
                        'time': timestamp,
                        'device': device,
                        'cycle_start_date': fields.get('cycleStartDate'),
                        'current_day_of_cycle': fields.get('currentDayOfCycle'),
                        'current_cycle_phase': fields.get('currentCyclePhase'),
                        'cycle_length': fields.get('cycleLength'),
                        'predicted_cycle_length': fields.get('predictedCycleLength'),
                        'period_length': fields.get('periodLength'),
                        'menstrual_flow': fields.get('menstrualFlow'),
                        'pregnancy_status': fields.get('pregnancyStatus'),
                        'symptoms': fields.get('symptoms'),
                        'mood': fields.get('mood'),
                        'notes': fields.get('notes'),
                        'raw_json': fields.get('rawJson'),
                    }, ['date'])

                elif measurement == 'LactateThreshold':
                    self._upsert(cursor, 'lactate_threshold', {
                        'time': timestamp,
                        'device': device,
                        'speed_threshold': fields.get(f"SpeedThreshold_{point['fields'].keys().__iter__().__next__().split('_')[-1]}") if any(k.startswith('SpeedThreshold') for k in fields) else None, # Tricky dynamic field access, let's simplify
                        # The fetcher produces: fields: {"SpeedThreshold_RUNNING": value}
                        # We need to extract this.
                        # Actually the fetcher produces distinct points for Speed and HeartRate.
                        # Wait, get_lactate_threshold does: fields: {f"{label}": value} where label is "SpeedThreshold_RUNNING"
                        # My current inserts might struggle with dynamic field keys.
                        # Let's handle it by checking keys.
                        'sport': fields.keys().__iter__().__next__().split('_')[-1], # e.g. RUNNING from SpeedThreshold_RUNNING
                        'speed_threshold': next((v for k,v in fields.items() if k.startswith('SpeedThreshold')), None),
                        'heart_rate_threshold': next((v for k,v in fields.items() if k.startswith('HeartRateThreshold')), None),
                    }, ['time', 'sport', 'device'])

            conn.commit()

        except Exception as e:
            logger.error(f"Failed to insert points: {e}")
            conn.rollback()
        finally:
            conn.close()

    def _upsert(self, cursor, table, data, keys):
        """Helper to perform INSERT OR REPLACE style upsert."""
        columns = ', '.join(data.keys())
        placeholders = ', '.join(['?'] * len(data))
        updates = ', '.join([f"{k}=Excluded.{k}" for k in data.keys() if k not in keys])
        
        # SQLite's ON CONFLICT clause
        sql = f"INSERT INTO {table} ({columns}) VALUES ({placeholders}) ON CONFLICT({', '.join(keys)}) DO UPDATE SET {updates}"
        
        cursor.execute(sql, list(data.values()))

    def get_latest_heart_rate_time(self):
        """Get the timestamp of the last heart rate reading to determine sync state."""
        conn = self._get_conn()
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT time FROM heart_rate_intraday ORDER BY time DESC LIMIT 1")
            result = cursor.fetchone()
            if result:
                return result[0]
            return None
        except Exception as e:
            logger.error(f"Failed to get last sync time: {e}")
            return None
        finally:
            conn.close()
