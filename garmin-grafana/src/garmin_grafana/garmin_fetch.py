# %%
import traceback
import base64, requests, time, pytz, logging, os, sys, dotenv, io, zipfile, json
from fitparse import FitFile, FitParseError
from datetime import datetime, timedelta
from datetime import datetime, timedelta
try:
    from garmin_grafana.sqlite_manager import GarminDB
except ImportError:
    from sqlite_manager import GarminDB
import xml.etree.ElementTree as ET
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)
# GarthHTTPError is referenced in fetch_write_bulk's except clause but was never
# imported. Python evaluates the whole except tuple before matching, so a
# NameError fired on EVERY requests HTTPError and dropped the date into the
# generic handler — the 500 retry/backoff path never ran. garth ships with
# garminconnect; the fallback keeps the module importable if that ever changes.
try:
    from garth.exc import GarthHTTPError
except ImportError:  # pragma: no cover - garth is a garminconnect dependency
    class GarthHTTPError(Exception):
        """Placeholder so the except clause stays valid without garth."""
garmin_obj = None
banner_text = """

*****  █▀▀ ▄▀█ █▀█ █▀▄▀█ █ █▄ █    █▀▀ █▀█ ▄▀█ █▀▀ ▄▀█ █▄ █ ▄▀█  *****
*****  █▄█ █▀█ █▀▄ █ ▀ █ █ █ ▀█    █▄█ █▀▄ █▀█ █▀  █▀█ █ ▀█ █▀█  *****

______________________________________________________________________

By Arpan Ghosh | Please consider supporting the project if you love it
______________________________________________________________________

"""
print(banner_text)

dotenv.load_dotenv()
env_override = dotenv.load_dotenv("override-default-vars.env", override=True)
if env_override:
    logging.warning("System ENV variables are overridden with override-default-vars.env")

# %%
SQLITE_DB_PATH = os.getenv("SQLITE_DB_PATH", "garmin.db")
# InfluxDB settings removed
TOKEN_DIR = os.getenv("TOKEN_DIR", "~/.garminconnect") # optional
GARMINCONNECT_EMAIL = os.environ.get("GARMINCONNECT_EMAIL", None) # optional, asks in prompt on run if not provided
GARMINCONNECT_PASSWORD = base64.b64decode(os.getenv("GARMINCONNECT_BASE64_PASSWORD")).decode("utf-8") if os.getenv("GARMINCONNECT_BASE64_PASSWORD") != None else os.environ.get("GARMINCONNECT_PASSWORD", None) # optional (plain or base64 variant), asks in prompt on run if not provided
GARMINCONNECT_IS_CN = True if os.getenv("GARMINCONNECT_IS_CN") in ['True', 'true', 'TRUE','t', 'T', 'yes', 'Yes', 'YES', '1'] else False # optional if you are using a Chinese account
GARMIN_DEVICENAME = os.getenv("GARMIN_DEVICENAME", "Unknown")  # optional, attempts to set the name automatically if not given
GARMIN_DEVICEID = os.getenv("GARMIN_DEVICEID", None)  # optional, attempts to set the id automatically if not given
AUTO_DATE_RANGE = False if os.getenv("AUTO_DATE_RANGE") in ['False','false','FALSE','f','F','no','No','NO','0'] else True # optional
MANUAL_START_DATE = os.getenv("MANUAL_START_DATE", None) # optional, in YYYY-MM-DD format, if you want to bulk update only from specific date
MANUAL_END_DATE = os.getenv("MANUAL_END_DATE", datetime.today().strftime('%Y-%m-%d')) # optional, in YYYY-MM-DD format, if you want to bulk update until a specific date
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO") # optional
FETCH_FAILED_WAIT_SECONDS = int(os.getenv("FETCH_FAILED_WAIT_SECONDS", 1800)) # optional
RATE_LIMIT_CALLS_SECONDS = int(os.getenv("RATE_LIMIT_CALLS_SECONDS", 5)) # optional
MAX_CONSECUTIVE_500_ERRORS = int(os.getenv("MAX_CONSECUTIVE_500_ERRORS", 10)) # optional, maximum consecutive HTTP 500 errors before continuing without retrying
GARMIN_DEVICENAME_AUTOMATIC = False if GARMIN_DEVICENAME != "Unknown" else True # optional
UPDATE_INTERVAL_SECONDS = int(os.getenv("UPDATE_INTERVAL_SECONDS", 300)) # optional
RESYNC_WINDOW_DAYS = int(os.getenv("RESYNC_WINDOW_DAYS", 7)) # optional, on every auto-fetch tick the start date is clamped to at least N days back so retroactive Garmin Connect edits (lifestyle, sleep notes) are picked up. Set to 0 to disable.
FETCH_SELECTION = os.getenv("FETCH_SELECTION", "daily_avg,sleep,steps,heartrate,stress,breathing,hrv,fitness_age,vo2,activity,race_prediction,body_composition,lifestyle,menstrual,environment,training_readiness,training_status,hill_score,endurance_score") # additional available values are lactate_threshold,blood_pressure,hydration,solar_intensity which you can add to the list seperated by , without any space. The `environment` key pulls Open-Meteo weather + air-quality + pollen once per cycle when HOME_LAT/HOME_LON are set.
LACTATE_THRESHOLD_SPORTS = os.getenv("LACTATE_THRESHOLD_SPORTS", "RUNNING").upper().split(",") # Garmin currently implements RUNNING, but has provisions for CYCLING, and SWIMMING
KEEP_FIT_FILES = True if os.getenv("KEEP_FIT_FILES") in ['True', 'true', 'TRUE','t', 'T', 'yes', 'Yes', 'YES', '1'] else False # optional
FIT_FILE_STORAGE_LOCATION = os.getenv("FIT_FILE_STORAGE_LOCATION", os.path.join(os.path.expanduser("~"), "fit_filestore"))
ALWAYS_PROCESS_FIT_FILES = True if os.getenv("ALWAYS_PROCESS_FIT_FILES") in ['True', 'true', 'TRUE','t', 'T', 'yes', 'Yes', 'YES', '1'] else False # optional, will process all FIT files for all activities including indoor ones lacking GPS data
REQUEST_INTRADAY_DATA_REFRESH = True if os.getenv("REQUEST_INTRADAY_DATA_REFRESH") in ['True', 'true', 'TRUE','t', 'T', 'yes', 'Yes', 'YES', '1'] else False # optional, This requests data refresh for the intraday data (older than 6 months) - see issue #77. Pauses the script for 24 hours when the daily limit is reached.
IGNORE_INTRADAY_DATA_REFRESH_DAYS = int(os.getenv("IGNORE_INTRADAY_DATA_REFRESH_DAYS", 30)) # optional, ignores the REQUEST_INTRADAY_DATA_REFRESH for the specified number of days from current date. 
TAG_MEASUREMENTS_WITH_USER_EMAIL = True if os.getenv("TAG_MEASUREMENTS_WITH_USER_EMAIL") in ['True', 'true', 'TRUE','t', 'T', 'yes', 'Yes', 'YES', '1'] else False # Adds an additional "User_ID" tag in each measurement for multi user database support - see #96
FORCE_REPROCESS_ACTIVITIES = False if os.getenv("FORCE_REPROCESS_ACTIVITIES") in ['False','false','FALSE','f','F','no','No','NO','0'] else True # optional, will enable re-processing of fit files when set to true, may skip activities if set to false (issue #30)
USER_TIMEZONE = os.getenv("USER_TIMEZONE", "") # optional, fetches timezone info from last activity automatically if left blank
PARSED_ACTIVITY_ID_LIST = []
IGNORE_ERRORS = True if os.getenv("IGNORE_ERRORS") in ['True', 'true', 'TRUE','t', 'T', 'yes', 'Yes', 'YES', '1'] else False

# %%
for handler in logging.root.handlers[:]:
    logging.root.removeHandler(handler)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)

# %%
# %%
try:
    garmin_db = GarminDB(SQLITE_DB_PATH)
    logging.info(f"Connected to SQLite database at {SQLITE_DB_PATH}")
except Exception as err:
    logging.error("Unable to connect with SQLite database! Aborted")
    raise Exception("SQLite connection failed:" + str(err))

# %%
# Pure JSON→points transforms live in transforms.py so they can be tested
# without importing this module (which reads its whole config into globals
# and opens a DB connection at import time).
try:
    from garmin_grafana import transforms
except ImportError:
    import transforms

iter_days = transforms.iter_days


# %%
# Token-store ownership guard. The implementation lives in token_owner.py so it
# can be tested without importing this module (which reads its whole config into
# globals and opens a DB connection at import time). These wrappers bind the
# module globals; behaviour is unchanged.
try:
    from garmin_grafana.token_owner import (
        TOKEN_OWNER_FILE as _TOKEN_OWNER_FILE,
        read_token_owner,
        token_owner_path,
        verify_token_owner,
        write_token_owner,
    )
except ImportError:
    from token_owner import (
        TOKEN_OWNER_FILE as _TOKEN_OWNER_FILE,
        read_token_owner,
        token_owner_path,
        verify_token_owner,
        write_token_owner,
    )


def _token_owner_path():
    return token_owner_path(TOKEN_DIR)


def _read_token_owner():
    return read_token_owner(TOKEN_DIR)


def _write_token_owner(email):
    write_token_owner(TOKEN_DIR, email)


def _verify_token_owner(garmin):
    verify_token_owner(garmin, TOKEN_DIR, GARMINCONNECT_EMAIL)


def _prompt_interactive(prompt):
    """input() that fails with a clear error when there is no terminal.

    A background fetcher (nohup / cron) has no stdin — a bare input() there
    dies with an EOFError traceback and the process crash-loops. Failing with
    an explicit message tells the operator what to put in the env instead."""
    if not sys.stdin or not sys.stdin.isatty():
        raise GarminConnectAuthenticationError(
            f"Cannot prompt for '{prompt.strip()}' — no interactive terminal. "
            "Set GARMINCONNECT_EMAIL and GARMINCONNECT_PASSWORD (or "
            "GARMINCONNECT_BASE64_PASSWORD) in the env for unattended re-login."
        )
    return input(prompt)


def garmin_login():
    try:
        logging.info(f"Trying to login to Garmin Connect using token data from directory '{TOKEN_DIR}'...")
        garmin = Garmin()
        garmin.login(TOKEN_DIR)
        _verify_token_owner(garmin)
        logging.info("login to Garmin Connect successful using stored session tokens.")

    except (FileNotFoundError, GarminConnectConnectionError, GarminConnectAuthenticationError):
        logging.warning("Session is expired or login information not present/incorrect. You'll need to log in again...login with your Garmin Connect credentials to generate them.")
        try:
            user_email = GARMINCONNECT_EMAIL or _prompt_interactive("Enter Garminconnect Login e-mail: ")
            user_password = GARMINCONNECT_PASSWORD or _prompt_interactive("Enter Garminconnect password (characters will be visible): ")
            garmin = Garmin(
                email=user_email, password=user_password, is_cn=GARMINCONNECT_IS_CN, return_on_mfa=True
            )
            result1, result2 = garmin.login()
            if result1 == "needs_mfa":  # MFA is required
                mfa_code = _prompt_interactive("MFA one-time code (via email or SMS): ")
                garmin.resume_login(result2, mfa_code)

            garmin.client.dump(TOKEN_DIR)
            _write_token_owner(user_email)
            logging.info(f"Oauth tokens stored in '{TOKEN_DIR}' directory for future use")

            garmin.login(TOKEN_DIR)
            logging.info("login to Garmin Connect successful using freshly stored session tokens — continuing.")

        except (
            FileNotFoundError,
            GarminConnectConnectionError,
            GarminConnectAuthenticationError,
            requests.exceptions.HTTPError,
        ) as err:
            logging.error(str(err))
            raise Exception("Session is expired : please login again and restart the script")

    return garmin

# %%
def write_points_to_db(points):
    # Deliberately does NOT swallow exceptions: a failed insert must propagate
    # so fetch_write_bulk's per-date error handling runs and the main loop
    # never advances last_influxdb_sync_time_UTC past unpersisted data.
    if len(points) != 0:
        if TAG_MEASUREMENTS_WITH_USER_EMAIL:
            for item in points:
                item['tags'].update({'User_ID': getattr(garmin_obj, 'display_name', None) or 'Unknown'})

        garmin_db.insert_points(points)
        logging.info("Success : updated SQLite database with new points")

# %%
def get_daily_stats(date_str):
    return transforms.daily_stats_points(garmin_obj.get_stats(date_str), date_str, GARMIN_DEVICENAME)


# %%
def get_last_sync():
    global GARMIN_DEVICENAME
    global GARMIN_DEVICEID
    points_list = []
    sync_data = garmin_obj.get_device_last_used()
    if GARMIN_DEVICENAME_AUTOMATIC:
        GARMIN_DEVICENAME = sync_data.get('lastUsedDeviceName') or "Unknown"
        GARMIN_DEVICEID = sync_data.get('userDeviceId') or None
    points_list.append({
        "measurement":  "DeviceSync",
        "time": datetime.fromtimestamp(sync_data['lastUsedDeviceUploadTime']/1000, tz=pytz.timezone("UTC")).isoformat(),
        "tags": {
            "Device": GARMIN_DEVICENAME,
            "Database_Name": "GarminDB"
        },
        "fields": {
            "imageUrl": sync_data.get('imageUrl'),
            "Device_Name": GARMIN_DEVICENAME
        }
    })
    if points_list:
        logging.info(f"Success : Updated device last sync time")
    else:
        logging.warning("No associated/synced Garmin device found with your account")
    return points_list

# %%
def get_sleep_data(date_str):
    return transforms.sleep_points(garmin_obj.get_sleep_data(date_str), date_str, GARMIN_DEVICENAME)


# %%
def get_intraday_hr(date_str):
    return transforms.intraday_hr_points(garmin_obj.get_heart_rates(date_str), date_str, GARMIN_DEVICENAME)


# %%
def get_intraday_steps(date_str):
    return transforms.intraday_steps_points(garmin_obj.get_steps_data(date_str), date_str, GARMIN_DEVICENAME)


# %%
def get_intraday_stress(date_str):
    return transforms.intraday_stress_points(garmin_obj.get_stress_data(date_str), date_str, GARMIN_DEVICENAME)


# %%
def get_intraday_br(date_str):
    return transforms.intraday_br_points(garmin_obj.get_respiration_data(date_str), date_str, GARMIN_DEVICENAME)


# %%
def get_intraday_hrv(date_str):
    return transforms.intraday_hrv_points(garmin_obj.get_hrv_data(date_str), date_str, GARMIN_DEVICENAME)


# %%
def get_body_composition(date_str):
    return transforms.body_composition_points(garmin_obj.get_weigh_ins(date_str, date_str), date_str, GARMIN_DEVICENAME)


# %%
def get_activity_summary(date_str):
    points_list = []
    activity_with_gps_id_dict = {}
    activity_list = garmin_obj.get_activities_by_date(date_str, date_str)
    for activity in activity_list:
        if activity.get('hasPolyline') or ALWAYS_PROCESS_FIT_FILES: # will process FIT files lacking GPS data if ALWAYS_PROCESS_FIT_FILES is set to True
            if not activity.get('hasPolyline'):
                logging.warning(f"Activity ID {activity.get('activityId')} got no GPS data - yet, activity FIT file data will be processed as ALWAYS_PROCESS_FIT_FILES is on")
            activity_with_gps_id_dict[activity.get('activityId')] = (activity.get('activityType') or {}).get('typeKey', "Unknown")
        if "startTimeGMT" in activity: # "startTimeGMT" should be available for all activities (fix #13)
            points_list.append({
                "measurement":  "ActivitySummary",
                "time": datetime.strptime(activity["startTimeGMT"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=pytz.UTC).isoformat(),
                "tags": {
                    "Device": GARMIN_DEVICENAME,
                    "Database_Name": "GarminDB",
                    "ActivityID": activity.get('activityId'),
                    "ActivitySelector": datetime.strptime(activity["startTimeGMT"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=pytz.UTC).strftime('%Y%m%dT%H%M%SUTC-') + (activity.get('activityType') or {}).get('typeKey', "Unknown")
                },
                "fields": {
                    "Activity_ID": activity.get('activityId'),
                    'Device_ID': activity.get('deviceId'),
                    'activityName': activity.get('activityName'),
                    'activityType': (activity.get('activityType') or {}).get('typeKey',None),
                    'distance': activity.get('distance'),
                    'elapsedDuration': activity.get('elapsedDuration'),
                    'movingDuration': activity.get('movingDuration'),
                    'averageSpeed': activity.get('averageSpeed'),
                    'maxSpeed': activity.get('maxSpeed'),
                    'calories': activity.get('calories'),
                    'bmrCalories': activity.get('bmrCalories'),
                    'averageHR': activity.get('averageHR'),
                    'maxHR': activity.get('maxHR'),
                    'locationName': activity.get('locationName'),
                    'lapCount': activity.get('lapCount'),
                    'hrTimeInZone_1': activity.get('hrTimeInZone_1'),
                    'hrTimeInZone_2': activity.get('hrTimeInZone_2'),
                    'hrTimeInZone_3': activity.get('hrTimeInZone_3'),
                    'hrTimeInZone_4': activity.get('hrTimeInZone_4'),
                    'hrTimeInZone_5': activity.get('hrTimeInZone_5'),
                    # Running dynamics (only populated for runs) + power
                    'averageRunningCadenceInStepsPerMinute': activity.get('averageRunningCadenceInStepsPerMinute'),
                    'maxRunningCadenceInStepsPerMinute': activity.get('maxRunningCadenceInStepsPerMinute'),
                    'avgStrideLength': activity.get('avgStrideLength'),
                    'avgVerticalOscillation': activity.get('avgVerticalOscillation'),
                    'avgVerticalRatio': activity.get('avgVerticalRatio'),
                    'avgGroundContactTime': activity.get('avgGroundContactTime'),
                    'avgPower': activity.get('avgPower'),
                    'maxPower': activity.get('maxPower'),
                    'normPower': activity.get('normPower'),
                }
            })
            points_list.append({
                "measurement":  "ActivitySummary",
                "time": (datetime.strptime(activity["startTimeGMT"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=pytz.UTC) + timedelta(seconds=int(activity.get('elapsedDuration', 0)))).isoformat(),
                "tags": {
                    "Device": GARMIN_DEVICENAME,
                    "Database_Name": "GarminDB",
                    "ActivityID": activity.get('activityId'),
                    "ActivitySelector": datetime.strptime(activity["startTimeGMT"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=pytz.UTC).strftime('%Y%m%dT%H%M%SUTC-') + (activity.get('activityType') or {}).get('typeKey', "Unknown")
                },
                "fields": {
                    "Activity_ID": activity.get('activityId'),
                    'Device_ID': activity.get('deviceId'),
                    'activityName': "END",
                    'activityType': "No Activity",
                }
            })
            logging.info(f"Success : Fetching Activity summary with id {activity.get('activityId')} for date {date_str}")
        else:
            logging.warning(f"Skipped : Start Timestamp missing for activity id {activity.get('activityId')} for date {date_str}")
    return points_list, activity_with_gps_id_dict

# %%
def fetch_activity_GPS(activityIDdict): # Uses FIT file by default, falls back to TCX
    points_list = []
    for activityID in activityIDdict.keys():
        activity_type = activityIDdict[activityID]
        if (activityID in PARSED_ACTIVITY_ID_LIST) and (not FORCE_REPROCESS_ACTIVITIES):
            logging.info(f"Skipping : Activity ID {activityID} has already been processed within current runtime")
            continue  # skip just this activity — a return here dropped every later activity of the day
        if (activityID in PARSED_ACTIVITY_ID_LIST) and (FORCE_REPROCESS_ACTIVITIES):
            logging.info(f"Re-processing : Activity ID {activityID} (FORCE_REPROCESS_ACTIVITIES is on)")
        try:
            zip_data = garmin_obj.download_activity(activityID, dl_fmt=garmin_obj.ActivityDownloadFormat.ORIGINAL)
            logging.info(f"Processing : Activity ID {activityID} FIT file data - this may take a while...")
            zip_buffer = io.BytesIO(zip_data)
            with zipfile.ZipFile(zip_buffer) as zip_ref:
                fit_filename = next((f for f in zip_ref.namelist() if f.endswith('.fit')), None)
                if not fit_filename:
                    raise FileNotFoundError(f"No FIT file found in the downloaded zip archive for Activity ID {activityID}")
                else:
                    fit_data = zip_ref.read(fit_filename)
                    fit_file_buffer = io.BytesIO(fit_data)
                    fitfile = FitFile(fit_file_buffer)
                    fitfile.parse()
                    all_records_list = [record.get_values() for record in fitfile.get_messages('record')]
                    all_sessions_list = [record.get_values() for record in fitfile.get_messages('session')]
                    all_lengths_list = [record.get_values() for record in fitfile.get_messages('length')]
                    all_laps_list = [record.get_values() for record in fitfile.get_messages('lap')]
                    if len(all_records_list) == 0:
                        raise FileNotFoundError(f"No records found in FIT file for Activity ID {activityID} - Discarding FIT file")
                    else:
                        activity_start_time = all_records_list[0]['timestamp'].replace(tzinfo=pytz.UTC)
                    for parsed_record in all_records_list:
                        if parsed_record.get('timestamp'):
                            point = {
                                "measurement": "ActivityGPS",
                                "time": parsed_record['timestamp'].replace(tzinfo=pytz.UTC).isoformat(), 
                                "tags": {
                                    "Device": GARMIN_DEVICENAME,
                                    "Database_Name": "GarminDB",
                                    "ActivityID": activityID,
                                    "ActivitySelector": activity_start_time.strftime('%Y%m%dT%H%M%SUTC-') + activity_type
                                },
                                "fields": {
                                    "ActivityName": activity_type,
                                    "Activity_ID": activityID,
                                    "Latitude": int(parsed_record['position_lat']) * ( 180 / 2**31 ) if parsed_record.get('position_lat') else None,
                                    "Longitude": int(parsed_record['position_long']) * ( 180 / 2**31 ) if parsed_record.get('position_long') else None,
                                    "Altitude": parsed_record.get('enhanced_altitude', None) or parsed_record.get('altitude', None),
                                    "Distance": parsed_record.get('distance', None),
                                    "DurationSeconds": (parsed_record['timestamp'].replace(tzinfo=pytz.UTC) - activity_start_time).total_seconds(),
                                    "HeartRate": float(parsed_record.get('heart_rate', None)) if parsed_record.get('heart_rate', None) else None,
                                    "Speed": parsed_record.get('enhanced_speed', None) or parsed_record.get('speed', None),
                                    "GradeAdjustedSpeed": (parsed_record.get("unknown_140") / 1000.0) if parsed_record.get("unknown_140") else None,
                                    "RunningEfficiency": ((parsed_record.get("unknown_140") / 1000.0)/parsed_record.get('heart_rate')) if (parsed_record.get("unknown_140") and parsed_record.get('heart_rate')) else None,
                                    "Cadence": parsed_record.get('cadence', None),
                                    "Fractional_Cadence": parsed_record.get('fractional_cadence', None),
                                    "Temperature": parsed_record.get('temperature', None),
                                    "Accumulated_Power": parsed_record.get('accumulated_power', None),
                                    "Power": parsed_record.get('power', None)
                                }
                            }
                            points_list.append(point)
                    for session_record in all_sessions_list:
                        if session_record.get('start_time') or session_record.get('timestamp'):
                            point = {
                                "measurement": "ActivitySession",
                                "time": session_record['start_time'].replace(tzinfo=pytz.UTC).isoformat() or session_record['timestamp'].replace(tzinfo=pytz.UTC).isoformat(), 
                                "tags": {
                                    "Device": GARMIN_DEVICENAME,
                                    "Database_Name": "GarminDB",
                                    "ActivityID": activityID,
                                    "ActivitySelector": activity_start_time.strftime('%Y%m%dT%H%M%SUTC-') + activity_type
                                },
                                "fields": {
                                    "Index": int(session_record.get('message_index', -1)) + 1,
                                    "ActivityName": activity_type,
                                    "Activity_ID": activityID,
                                    "Sport": str(session_record.get('sport', None)), # Avoid partial write error 400 see #152#issuecomment-3084539416
                                    "Sub_Sport": session_record.get('sub_sport', None),
                                    "Pool_Length": session_record.get('pool_length', None),
                                    "Pool_Length_Unit": session_record.get('pool_length_unit', None),
                                    "Lengths": session_record.get('num_laps', None),
                                    "Laps": session_record.get('num_lengths', None),
                                    "Aerobic_Training": session_record.get('total_training_effect', None),
                                    "Anaerobic_Training": session_record.get('total_anaerobic_training_effect', None),
                                    "Primary_Benefit": session_record.get('primary_benefit', None),
                                    "Recovery_Time": session_record.get('recovery_time', None)
                                }
                            }
                            points_list.append(point)
                    for length_record in all_lengths_list:
                        if length_record.get('start_time') or length_record.get('timestamp'):
                            point = {
                                "measurement": "ActivityLength",
                                "time": length_record['start_time'].replace(tzinfo=pytz.UTC).isoformat() or length_record['timestamp'].replace(tzinfo=pytz.UTC).isoformat(), 
                                "tags": {
                                    "Device": GARMIN_DEVICENAME,
                                    "Database_Name": "GarminDB",
                                    "ActivityID": activityID,
                                    "ActivitySelector": activity_start_time.strftime('%Y%m%dT%H%M%SUTC-') + activity_type
                                },
                                "fields": {
                                    "Index": int(length_record.get('message_index', -1)) + 1,
                                    "ActivityName": activity_type,
                                    "Activity_ID": activityID,
                                    "Elapsed_Time": length_record.get('total_elapsed_time', None),
                                    "Strokes": length_record.get('total_strokes', None),
                                    "Swim_Stroke": length_record.get('swim_stroke', None),
                                    "Avg_Speed": length_record.get('avg_speed', None),
                                    "Calories": length_record.get('total_calories', None),
                                    "Avg_Cadence": length_record.get('avg_swimming_cadence', None)
                                }
                            }
                            points_list.append(point)
                    for lap_record in all_laps_list:
                        if lap_record.get('start_time') or lap_record.get('timestamp'):
                            point = {
                                "measurement": "ActivityLap",
                                "time": lap_record['start_time'].replace(tzinfo=pytz.UTC).isoformat() or lap_record['timestamp'].replace(tzinfo=pytz.UTC).isoformat(), 
                                "tags": {
                                    "Device": GARMIN_DEVICENAME,
                                    "Database_Name": "GarminDB",
                                    "ActivityID": activityID,
                                    "ActivitySelector": activity_start_time.strftime('%Y%m%dT%H%M%SUTC-') + activity_type
                                },
                                "fields": {
                                    "Index": int(lap_record.get('message_index', -1)) + 1,
                                    "ActivityName": activity_type,
                                    "Activity_ID": activityID,
                                    "Elapsed_Time": lap_record.get('total_elapsed_time', None),
                                    "Sport": lap_record.get('sport', None),
                                    "Lengths": lap_record.get('num_lengths', None),
                                    "Length_Index": lap_record.get('first_length_index', None),
                                    "Distance": lap_record.get('total_distance', None),
                                    "Cycles": lap_record.get('total_cycles', None),
                                    "Avg_Stroke_Distance": lap_record.get('avg_stroke_distance', None),
                                    "Moving_Duration": lap_record.get('total_moving_time', None),
                                    "Standing_Duration": lap_record.get('time_standing', None),
                                    "Avg_Speed": lap_record.get('enhanced_avg_speed', None),
                                    "Max_Speed": lap_record.get('enhanced_max_speed', None),
                                    "Calories": lap_record.get('total_calories', None),
                                    "Avg_Power": lap_record.get('avg_power', None),
                                    "Avg_HR": lap_record.get('avg_heart_rate', None),
                                    "Max_HR": lap_record.get('max_heart_rate', None),
                                    "Avg_Cadence": lap_record.get('avg_cadence', None),
                                    "Avg_Temperature": lap_record.get('avg_temperature', None)
                                }
                            }
                            points_list.append(point)
                    if KEEP_FIT_FILES:
                        os.makedirs(FIT_FILE_STORAGE_LOCATION, exist_ok=True)
                        fit_path = os.path.join(FIT_FILE_STORAGE_LOCATION, activity_start_time.strftime('%Y%m%dT%H%M%SUTC-') + activity_type + ".fit")
                        with open(fit_path, "wb") as f:
                            f.write(fit_data)
                        logging.info(f"Success : Activity ID {activityID} stored in output file {fit_path}")
        except (FileNotFoundError, FitParseError) as err:
            logging.error(err)
            logging.warning(f"Fallback : Failed to use FIT file for activityID {activityID} - Trying TCX file...")
            
            ns = {"tcx": "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2", "ns3": "http://www.garmin.com/xmlschemas/ActivityExtension/v2"}
            try:
                tcx_file_data = garmin_obj.download_activity(activityID, dl_fmt=garmin_obj.ActivityDownloadFormat.TCX).decode("UTF-8")
                root = ET.fromstring(tcx_file_data)
                if KEEP_FIT_FILES:
                    os.makedirs(FIT_FILE_STORAGE_LOCATION, exist_ok=True)
                    activity_start_time = datetime.fromisoformat(root.findall("tcx:Activities/tcx:Activity", ns)[0].find("tcx:Id", ns).text.strip("Z"))
                    tcx_path = os.path.join(FIT_FILE_STORAGE_LOCATION, activity_start_time.strftime('%Y%m%dT%H%M%SUTC-') + activity_type + ".tcx")
                    with open(tcx_path, "w") as f:
                        f.write(tcx_file_data)
                    logging.info(f"Success : Activity ID {activityID} stored in output file {tcx_path}")
            except requests.exceptions.Timeout as err:
                logging.warning(f"Request timeout for fetching large activity record {activityID} - skipping record")
                continue  # a return here also threw away points already parsed for earlier activities
            except Exception as err:
                logging.exception(f"Unable to fetch TCX for activity record {activityID} : skipping record")
                continue

            for activity in root.findall("tcx:Activities/tcx:Activity", ns):
                activity_start_time = datetime.fromisoformat(activity.find("tcx:Id", ns).text.strip("Z"))
                lap_index = 1
                for lap in activity.findall("tcx:Lap", ns):
                    lap_start_time = datetime.fromisoformat(lap.attrib.get("StartTime").strip("Z"))
                    for tp in lap.findall(".//tcx:Trackpoint", ns):
                        time_obj = datetime.fromisoformat(tp.findtext("tcx:Time", default=None, namespaces=ns).strip("Z"))
                        lat = tp.findtext("tcx:Position/tcx:LatitudeDegrees", default=None, namespaces=ns)
                        lon = tp.findtext("tcx:Position/tcx:LongitudeDegrees", default=None, namespaces=ns)
                        alt = tp.findtext("tcx:AltitudeMeters", default=None, namespaces=ns)
                        dist = tp.findtext("tcx:DistanceMeters", default=None, namespaces=ns)
                        hr = tp.findtext("tcx:HeartRateBpm/tcx:Value", default=None, namespaces=ns)
                        speed = tp.findtext("tcx:Extensions/ns3:TPX/ns3:Speed", default=None, namespaces=ns)

                        try: lat = float(lat)
                        except: lat = None
                        try: lon = float(lon)
                        except: lon = None
                        try: alt = float(alt)
                        except: alt = None
                        try: dist = float(dist)
                        except: dist = None
                        try: hr = float(hr)
                        except: hr = None
                        try: speed = float(speed)
                        except: speed = None

                        point = {
                            "measurement": "ActivityGPS",
                            "time": time_obj.isoformat(), 
                            "tags": {
                                "Device": GARMIN_DEVICENAME,
                                "Database_Name": "GarminDB",
                                "ActivityID": activityID,
                                "ActivitySelector": activity_start_time.strftime('%Y%m%dT%H%M%SUTC-') + activity_type
                            },
                            "fields": {
                                "ActivityName": activity_type,
                                "Activity_ID": activityID,
                                "Latitude": lat,
                                "Longitude": lon,
                                "Altitude": alt,
                                "Distance": dist,
                                "DurationSeconds": (time_obj - activity_start_time).total_seconds(),
                                "HeartRate": hr,
                                "Speed": speed,
                                "lap": lap_index
                            }
                        }
                        points_list.append(point)
                    
                    lap_index += 1
        logging.info(f"Success : Fetching detailed activity for Activity ID {activityID}")
        PARSED_ACTIVITY_ID_LIST.append(activityID)
    return points_list

def get_lactate_threshold(date_str):
    points_list = []
    endpoints = {}
    
    for ltsport in LACTATE_THRESHOLD_SPORTS:
        endpoints[f"SpeedThreshold_{ltsport}"] = f"/biometric-service/stats/lactateThresholdSpeed/range/{date_str}/{date_str}?aggregation=daily&sport={ltsport}"
        endpoints[f"HeartRateThreshold_{ltsport}"] = f"/biometric-service/stats/lactateThresholdHeartRate/range/{date_str}/{date_str}?aggregation=daily&sport={ltsport}"

    for label, endpoint in endpoints.items():
        lt_list_all = garmin_obj.connectapi(endpoint)
        if lt_list_all:
            for lt_dict in lt_list_all:
                value = lt_dict.get("value")
                if value is not None:
                    points_list.append({
                        "measurement": "LactateThreshold",
                        "time": datetime.fromtimestamp(datetime.strptime(date_str, "%Y-%m-%d").timestamp(), tz=pytz.timezone("UTC")).isoformat(),
                        "tags": {
                            "Device": GARMIN_DEVICENAME,
                            "Database_Name": "GarminDB"
                        },
                        "fields": {f"{label}": value}
                    })
                    logging.info(f"Success : Fetching {label} for date {date_str}")

    return points_list
    
def get_training_status(date_str):
    return transforms.training_status_points(garmin_obj.get_training_status(date_str), date_str, GARMIN_DEVICENAME)


# Contribution from PR #17 by @arturgoms 
def get_training_readiness(date_str):
    return transforms.training_readiness_points(garmin_obj.get_training_readiness(date_str), date_str, GARMIN_DEVICENAME)


# Contribution from PR #17 by @arturgoms 
def get_hillscore(date_str):
    return transforms.hill_score_points(garmin_obj.get_hill_score(date_str), date_str, GARMIN_DEVICENAME)


# Contribution from PR #17 by @arturgoms 
def get_race_predictions(date_str):
    return transforms.race_prediction_points(garmin_obj.get_race_predictions(startdate=date_str, enddate=date_str, _type="daily"), date_str, GARMIN_DEVICENAME)


def get_fitness_age(date_str):
    return transforms.fitness_age_points(garmin_obj.get_fitnessage_data(date_str), date_str, GARMIN_DEVICENAME)


def get_vo2_max(date_str):
    return transforms.vo2_max_points(garmin_obj.get_max_metrics(date_str), date_str, GARMIN_DEVICENAME)


def get_endurance_score(date_str):
    return transforms.endurance_score_points(garmin_obj.get_endurance_score(date_str), date_str, GARMIN_DEVICENAME)


def get_blood_pressure(date_str):
    return transforms.blood_pressure_points(garmin_obj.get_blood_pressure(date_str, date_str), date_str, GARMIN_DEVICENAME)


def get_hydration(date_str):
    return transforms.hydration_points(garmin_obj.get_hydration_data(date_str), date_str, GARMIN_DEVICENAME)


def get_solar_intensity(date_str):
    points_list = []

    if not GARMIN_DEVICEID:
        logging.warning("Skipping Solar Intensity data fetch as GARMIN_DEVICEID is not set.")
        return points_list

    si_all = garmin_obj.get_device_solar_data(GARMIN_DEVICEID, date_str) or {}
    if len(si_all.get('solarDailyDataDTOs', [])) > 0:
        si_list = si_all['solarDailyDataDTOs'][0].get('solarInputReadings', [])
        for si_measurement in si_list:
            data_fields = {
                'solarUtilization': si_measurement.get('solarUtilization', None),
                'activityTimeGainMs': si_measurement.get('activityTimeGainMs', None),
            }
            if not all(value is None for value in data_fields.values()) and 'readingTimestampGmt' in si_measurement:
                points_list.append({
                    "measurement":  "SolarIntensity",
                    "time": pytz.UTC.localize(datetime.strptime(si_measurement['readingTimestampGmt'], '%Y-%m-%dT%H:%M:%S.%f')),
                    "tags": {
                        "Device": GARMIN_DEVICENAME,
                        "Database_Name": "GarminDB"
                    },
                    "fields": data_fields
                })
        logging.info(f"Success : Fetching Solar Intensity data for date {date_str}")
    if len(points_list) == 0:
        logging.warning(f"No Solar Intensity data available for date {date_str}")
    return points_list

# %%
def get_lifestyle_data(date_str):
    return transforms.lifestyle_points(garmin_obj.get_lifestyle_logging_data(date_str), date_str, GARMIN_DEVICENAME)


# %%
# Garmin renamed the menstrual response keys around May 2026: summary/dayData
# became daySummary/dayLog, and the cycle phase changed from a string
# ('LUTEAL') to an integer enum. Mapping verified empirically against the
# fertile window + period days of a live cycle.
_MENSTRUAL_PHASE_NAMES = {1: "MENSTRUAL", 2: "FOLLICULAR", 3: "OVULATORY", 4: "LUTEAL"}
_menstrual_shape_warned = False


def get_menstrual_data(date_str):
    return transforms.menstrual_points(garmin_obj.get_menstrual_data_for_date(date_str), date_str, GARMIN_DEVICENAME)


# %%
def daily_fetch_write(date_str):
    if REQUEST_INTRADAY_DATA_REFRESH and (datetime.strptime(date_str, "%Y-%m-%d") <= (datetime.today() - timedelta(days=IGNORE_INTRADAY_DATA_REFRESH_DAYS))):
        data_refresh_response = garmin_obj.connectapi(f"wellness-service/wellness/epoch/request/{date_str}", method="POST").get("status", "Unknown")
        logging.info(f"Intraday data refresh request status: {data_refresh_response}")
        if data_refresh_response == "SUBMITTED":
            logging.info(f"Waiting 10 seconds for refresh request to process...")
            time.sleep(10)
        elif data_refresh_response == "COMPLETE":
            logging.info(f"Data for date {date_str} is already available")
        elif data_refresh_response == "NO_FILES_FOUND":
            logging.info(f"No Data is available for date {date_str} to refresh")
            return None
        elif data_refresh_response == "DENIED":
            # Daily refresh limit reached. Do NOT sleep 24h here — that froze
            # ALL fetching (every metric, every day) for a whole day. Skip the
            # refresh request and carry on with normal fetching; already-synced
            # intraday data still comes through.
            logging.warning(f"Intraday refresh request DENIED for {date_str} (daily refresh limit reached) - skipping refresh and continuing with normal fetching. Disable REQUEST_INTRADAY_DATA_REFRESH to avoid this!")
        else:
            logging.info(f"Refresh response is unknown!")
            time.sleep(5)
    if 'daily_avg' in FETCH_SELECTION:
        write_points_to_db(get_daily_stats(date_str))
    if 'sleep' in FETCH_SELECTION:
        write_points_to_db(get_sleep_data(date_str))
    if 'steps' in FETCH_SELECTION:
        write_points_to_db(get_intraday_steps(date_str))
    if 'heartrate' in FETCH_SELECTION:
        write_points_to_db(get_intraday_hr(date_str))
    if 'stress' in FETCH_SELECTION:
        write_points_to_db(get_intraday_stress(date_str))
    if 'breathing' in FETCH_SELECTION:
        write_points_to_db(get_intraday_br(date_str))
    if 'hrv' in FETCH_SELECTION:
        write_points_to_db(get_intraday_hrv(date_str))
    if 'fitness_age' in FETCH_SELECTION:
        write_points_to_db(get_fitness_age(date_str))
    if 'vo2' in FETCH_SELECTION:
        write_points_to_db(get_vo2_max(date_str))
    if 'race_prediction' in FETCH_SELECTION:
        write_points_to_db(get_race_predictions(date_str))
    if 'body_composition' in FETCH_SELECTION:
        write_points_to_db(get_body_composition(date_str))
    if 'lactate_threshold' in FETCH_SELECTION:
        write_points_to_db(get_lactate_threshold(date_str))
    if 'training_status' in FETCH_SELECTION:
        write_points_to_db(get_training_status(date_str))
    if 'training_readiness' in FETCH_SELECTION:
        write_points_to_db(get_training_readiness(date_str))
    if 'hill_score' in FETCH_SELECTION:
        write_points_to_db(get_hillscore(date_str))
    if 'endurance_score' in FETCH_SELECTION:
        write_points_to_db(get_endurance_score(date_str))
    if 'blood_pressure' in FETCH_SELECTION:
        write_points_to_db(get_blood_pressure(date_str))
    if 'hydration' in FETCH_SELECTION:
        write_points_to_db(get_hydration(date_str))
    if 'activity' in FETCH_SELECTION:
        activity_summary_points_list, activity_with_gps_id_dict = get_activity_summary(date_str)
        write_points_to_db(activity_summary_points_list)
        write_points_to_db(fetch_activity_GPS(activity_with_gps_id_dict))
    if 'solar_intensity' in FETCH_SELECTION:
        write_points_to_db(get_solar_intensity(date_str))
    if 'lifestyle' in FETCH_SELECTION:
        write_points_to_db(get_lifestyle_data(date_str))
    if 'menstrual' in FETCH_SELECTION:
        write_points_to_db(get_menstrual_data(date_str))


# %%
def fetch_write_bulk(start_date_str, end_date_str):
    global garmin_obj
    consecutive_500_errors = 0
    logging.info("Fetching data for the given period in reverse chronological order")
    time.sleep(3)
    write_points_to_db(get_last_sync())
    for current_date in iter_days(start_date_str, end_date_str):
        repeat_loop = True
        while repeat_loop:
            try:
                daily_fetch_write(current_date)
                # Reset consecutive 500 error counter on successful fetch
                if consecutive_500_errors > 0:
                    logging.info(f"Successfully fetched data after {consecutive_500_errors} consecutive 500 errors - resetting error counter")
                    consecutive_500_errors = 0
                logging.info(f"Success : Fetched all available health metrics for date {current_date} (skipped any if unavailable)")
                if RATE_LIMIT_CALLS_SECONDS > 0:
                    logging.info(f"Waiting : for {RATE_LIMIT_CALLS_SECONDS} seconds")
                    time.sleep(RATE_LIMIT_CALLS_SECONDS)
                repeat_loop = False
            except GarminConnectTooManyRequestsError as err:
                logging.error(err)
                logging.info(f"Too many requests (429) : Failed to fetch one or more metrics - will retry for date {current_date}")
                logging.info(f"Waiting : for {FETCH_FAILED_WAIT_SECONDS} seconds")
                time.sleep(FETCH_FAILED_WAIT_SECONDS)
                repeat_loop = True
            except (requests.exceptions.HTTPError, GarthHTTPError) as err:
                if transforms.is_http_500(err):
                    consecutive_500_errors += 1
                    logging.error(f"HTTP 500 error ({consecutive_500_errors}/{MAX_CONSECUTIVE_500_ERRORS}) for date {current_date}: {err}")
                    if consecutive_500_errors >= MAX_CONSECUTIVE_500_ERRORS:
                        logging.warning(f"Received {consecutive_500_errors} consecutive HTTP 500 errors. Logging error and continuing backward in time to fetch remaining data.")
                        logging.warning(f"Skipping date {current_date} due to persistent 500 errors from Garmin API")
                        logging.info(f"Waiting : for {RATE_LIMIT_CALLS_SECONDS} seconds before continuing")
                        time.sleep(RATE_LIMIT_CALLS_SECONDS)
                        repeat_loop = False
                    else:
                        logging.info(f"HTTP 500 error encountered - will retry for date {current_date} (attempt {consecutive_500_errors}/{MAX_CONSECUTIVE_500_ERRORS})")
                        logging.info(f"Waiting : for {RATE_LIMIT_CALLS_SECONDS} seconds before retry")
                        time.sleep(RATE_LIMIT_CALLS_SECONDS)
                        repeat_loop = True
                else:
                    # Non-500 HTTP errors - handle as before
                    logging.error(err)
                    logging.info(f"HTTP Error (non-500) : Failed to fetch one or more metrics - skipping date {current_date}")
                    logging.info(f"Waiting : for {RATE_LIMIT_CALLS_SECONDS} seconds")
                    time.sleep(RATE_LIMIT_CALLS_SECONDS)
                    repeat_loop = False
            except (
                    GarminConnectConnectionError,
                    requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout
                    ) as err:
                logging.error(err)
                logging.info(f"Connection Error : Failed to fetch one or more metrics - skipping date {current_date}")
                logging.info(f"Waiting : for {RATE_LIMIT_CALLS_SECONDS} seconds")
                time.sleep(RATE_LIMIT_CALLS_SECONDS)
                repeat_loop = False
            except GarminConnectAuthenticationError as err:
                logging.error(err)
                logging.info(f"Authentication Failed : Retrying login with given credentials (won't work automatically for MFA/2FA enabled accounts)")
                garmin_obj = garmin_login()
                time.sleep(5)
                repeat_loop = True
            except Exception as err:
                if IGNORE_ERRORS:
                    logging.warning("IGNORE_ERRORS Enabled >> Failed to process %s:", current_date)
                    logging.exception(err)
                    repeat_loop = False
                else:
                    raise err

    # Pull weather / air-quality / pollen context once per bulk cycle.
    # No-op when HOME_LAT / HOME_LON are not set; failures are non-fatal.
    if "environment" in FETCH_SELECTION:
        try:
            from garmin_grafana.environment_fetch import fetch_from_env
            fetch_from_env()
        except Exception as env_err:
            logging.warning(f"environment_daily fetch failed (non-fatal): {env_err}")

    # No-op when HA_URL / HA_TOKEN / HA_ENTITIES are not set; failures are non-fatal.
    try:
        from garmin_grafana.ha_fetch import fetch_from_env as ha_fetch_from_env
        ha_fetch_from_env()
    except Exception as ha_err:
        logging.warning(f"ha_sensor fetch failed (non-fatal): {ha_err}")


if __name__ == "__main__":
    garmin_obj = garmin_login()

    # %%
    if MANUAL_START_DATE:
        logging.warning(f"MANUAL_START_DATE is set to '{MANUAL_START_DATE}' — overrides automatic date detection. Set in .env or override-default-vars.env. Remove to use auto mode.")
        logging.warning(f"MANUAL_END_DATE is '{MANUAL_END_DATE}' (defaults to today if not explicitly set).")
        fetch_write_bulk(MANUAL_START_DATE, MANUAL_END_DATE)
        logging.info(f"Bulk update success : Fetched all available health metrics for date range {MANUAL_START_DATE} to {MANUAL_END_DATE}")
        exit(0)
    else:
        try:
            latest_hr_time_str = garmin_db.get_latest_heart_rate_time()
            if latest_hr_time_str:
                # fromisoformat() returns an aware datetime when the string
                # contains "+00:00" — pytz.utc.localize() raises ValueError on
                # already-aware datetimes.  Use astimezone() instead, which works
                # on both naive and aware inputs.
                _dt = datetime.fromisoformat(latest_hr_time_str)
                last_influxdb_sync_time_UTC = _dt.astimezone(pytz.utc) if _dt.tzinfo else pytz.utc.localize(_dt)
            else:
                raise Exception("No data found")
        except Exception as err:
            logging.warning(f"No previously synced data found in local SQLite database ({err}), defaulting to 7 day initial fetching. Use specific start date ENV variable to bulk update past data")
            last_influxdb_sync_time_UTC = (datetime.today() - timedelta(days=7)).astimezone(pytz.timezone("UTC"))
        try:
            if USER_TIMEZONE: # If provided by user, using that. 
                local_timediff = datetime.now(tz=pytz.timezone(USER_TIMEZONE)).utcoffset()
            else: # otherwise try to set automatically
                last_activity_dict = garmin_obj.get_last_activity() # (very unlineky event that this will be empty given Garmin's userbase, everyone should have at least one activity)
                local_timediff = datetime.strptime(last_activity_dict['startTimeLocal'], '%Y-%m-%d %H:%M:%S') - datetime.strptime(last_activity_dict['startTimeGMT'], '%Y-%m-%d %H:%M:%S')
            if local_timediff >= timedelta(0):
                logging.info("Using user's local timezone as UTC+" + str(local_timediff))
            else:
                logging.info("Using user's local timezone as UTC-" + str(-local_timediff))
        except (KeyError, TypeError) as err:
            logging.warning(f"Unable to determine user's timezone - Defaulting to UTC. Consider providing TZ identifier with USER_TIMEZONE environment variable")
            local_timediff = timedelta(hours=0)
        
        last_trailing_resync_day = ""  # forces a trailing-window pass on first iteration
        while True:
            # The loop body is guarded so a transient network / Garmin API /
            # auth error can't kill the long-running fetcher process — before
            # this, an exception from get_device_last_used() (which runs
            # outside fetch_write_bulk's own error handling) crashed the
            # process and left a data gap until the cron self-heal relaunched it.
            try:
                last_watch_sync_time_UTC = datetime.fromtimestamp(int(garmin_obj.get_device_last_used().get('lastUsedDeviceUploadTime')/1000)).astimezone(pytz.timezone("UTC"))
                # Use today's local date as the end date rather than lastUsedDeviceUploadTime.
                # The watch API upload timestamp can be stale (e.g. still showing yesterday) even when
                # today's data is already available on Garmin servers, causing the fetch to miss
                # recent days. Fetching up to today is safe — empty dates simply return no records.
                today_local_str = datetime.today().strftime('%Y-%m-%d')
                start_local_str = (last_influxdb_sync_time_UTC + local_timediff).strftime('%Y-%m-%d')
                # Once per calendar day, widen the start back by RESYNC_WINDOW_DAYS
                # so retroactive Garmin Connect edits (lifestyle entries, sleep
                # notes, etc.) get picked up. All upserts are idempotent on natural
                # keys, so re-fetching is safe; we gate it to once/day to avoid
                # re-pulling N days every UPDATE_INTERVAL_SECONDS tick.
                if RESYNC_WINDOW_DAYS > 0 and last_trailing_resync_day != today_local_str:
                    resync_floor_str = (datetime.today() - timedelta(days=RESYNC_WINDOW_DAYS)).strftime('%Y-%m-%d')
                    if resync_floor_str < start_local_str:
                        logging.info(f"Trailing resync : widening start to {resync_floor_str} (was {start_local_str}) to catch retroactive edits over past {RESYNC_WINDOW_DAYS} days")
                        start_local_str = resync_floor_str
                    last_trailing_resync_day = today_local_str
                if last_influxdb_sync_time_UTC < last_watch_sync_time_UTC or start_local_str < today_local_str:
                    logging.info(f"Update found : fetching {start_local_str} → {today_local_str} (watch last upload: {last_watch_sync_time_UTC} UTC)")
                    fetch_write_bulk(start_local_str, today_local_str)
                    last_influxdb_sync_time_UTC = last_watch_sync_time_UTC
                else:
                    logging.info(f"No new data found : DB sync={last_influxdb_sync_time_UTC} UTC, watch upload={last_watch_sync_time_UTC} UTC")
            except GarminConnectAuthenticationError as err:
                logging.error(f"Authentication error in main loop ({err}) - attempting re-login")
                try:
                    garmin_obj = garmin_login()
                except Exception as login_err:
                    logging.error(f"Re-login failed ({login_err}) - will retry next cycle")
            except Exception as err:
                logging.exception(f"Main loop iteration failed (will retry next cycle): {err}")
            logging.info(f"waiting for {UPDATE_INTERVAL_SECONDS} seconds before next automatic update calls")
            time.sleep(UPDATE_INTERVAL_SECONDS)
