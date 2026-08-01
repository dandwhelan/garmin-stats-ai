"""Pure Garmin-JSON → points transforms.

Split out of ``garmin_fetch`` so the field mapping and — more importantly —
the timestamp handling can be tested. ``garmin_fetch`` cannot be imported at
all without a populated environment: it reads ~30 env vars into module globals
and opens a SQLite connection at import time. These functions take the API
payload and the device name as arguments instead, so they need neither.

The timestamp conventions encoded here are the part worth guarding. They are
not arbitrary, and getting one wrong shifts a whole metric by a day without
anything failing:

* ``DailyStats`` is stamped at **noon UTC of the requested date**, not at the
  payload's own ``wellnessStartTimeGmt``. That field is local midnight
  expressed in UTC, which for a UTC+1 (BST) user falls on the *previous* UTC
  day — so every daily row landed under the wrong date.
* ``SleepSummary`` is stamped at ``sleepEndTimestampGMT`` — the **wake** time.
  A sleep record dated X is therefore the night that *ended* on the morning of
  X, i.e. last night's sleep lives on today's date.
* Garmin mixes three timestamp encodings across endpoints: epoch milliseconds,
  ``"%Y-%m-%dT%H:%M:%S.%f"`` naive-GMT strings, and ``[epoch_ms, value]``
  pairs. Each helper below states which it consumes.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

import pytz

UTC = pytz.timezone("UTC")

_GMT_FORMAT = "%Y-%m-%dT%H:%M:%S.%f"

# Garmin encodes cycle phase as an int in the newer payload shape.
_MENSTRUAL_PHASE_NAMES = {1: "MENSTRUAL", 2: "FOLLICULAR", 3: "OVULATORY", 4: "LUTEAL"}

# One-shot warning latch for an unrecognised menstrual payload shape
# (moved with menstrual_points, which declares it global).
_menstrual_shape_warned = False


def iter_days(start_date: str, end_date: str):
    """Yield YYYY-MM-DD from ``end_date`` back to ``start_date`` inclusive.

    Reverse chronological on purpose: the fetcher walks backwards so the most
    recent (and most-wanted) days land first, and a mid-run failure leaves the
    freshest data already written.
    """
    start = datetime.strptime(start_date, '%Y-%m-%d')
    end = datetime.strptime(end_date, '%Y-%m-%d')
    current = end

    while current >= start:
        yield current.strftime('%Y-%m-%d')
        current -= timedelta(days=1)


def daily_stats_points(stats_json, date_str, device_name):
    points_list = []
    if stats_json['wellnessStartTimeGmt'] and datetime.strptime(date_str, "%Y-%m-%d") < datetime.today():
        points_list.append({
            "measurement":  "DailyStats",
            # Use noon UTC of the *requested* date so that timestamp[:10] in the
            # SQLite writer always equals date_str regardless of the user's timezone.
            # Previously we used wellnessStartTimeGmt (midnight local time in UTC)
            # which for BST users (UTC+1) falls on the *previous* UTC day, causing
            # every daily_stats row to be stored under the wrong date.
            "time": datetime.strptime(date_str, "%Y-%m-%d").replace(hour=12, tzinfo=pytz.UTC).isoformat(),
            "tags": {
                "Device": device_name,
                "Database_Name": "GarminDB"
            },
            "fields": {
                "activeKilocalories": stats_json.get('activeKilocalories'),
                "bmrKilocalories": stats_json.get('bmrKilocalories'),

                'totalSteps': stats_json.get('totalSteps'),
                'totalDistanceMeters': stats_json.get('totalDistanceMeters'),

                "highlyActiveSeconds": stats_json.get("highlyActiveSeconds"),
                "activeSeconds": stats_json.get("activeSeconds"),
                "sedentarySeconds": stats_json.get("sedentarySeconds"),
                "sleepingSeconds": stats_json.get("sleepingSeconds"),
                "moderateIntensityMinutes": stats_json.get("moderateIntensityMinutes"),
                "vigorousIntensityMinutes": stats_json.get("vigorousIntensityMinutes"),

                "floorsAscendedInMeters": stats_json.get("floorsAscendedInMeters"),
                "floorsDescendedInMeters": stats_json.get("floorsDescendedInMeters"),
                "floorsAscended": stats_json.get("floorsAscended"),
                "floorsDescended": stats_json.get("floorsDescended"),

                "minHeartRate": stats_json.get("minHeartRate"),
                "maxHeartRate": stats_json.get("maxHeartRate"),
                "restingHeartRate": stats_json.get("restingHeartRate"),
                "minAvgHeartRate": stats_json.get("minAvgHeartRate"),
                "maxAvgHeartRate": stats_json.get("maxAvgHeartRate"),
                
                "stressDuration": stats_json.get("stressDuration"),
                "restStressDuration": stats_json.get("restStressDuration"),
                "activityStressDuration": stats_json.get("activityStressDuration"),
                "uncategorizedStressDuration": stats_json.get("uncategorizedStressDuration"),
                "totalStressDuration": stats_json.get("totalStressDuration"),
                "lowStressDuration": stats_json.get("lowStressDuration"),
                "mediumStressDuration": stats_json.get("mediumStressDuration"),
                "highStressDuration": stats_json.get("highStressDuration"),
                
                "stressPercentage": stats_json.get("stressPercentage"),
                "restStressPercentage": stats_json.get("restStressPercentage"),
                "activityStressPercentage": stats_json.get("activityStressPercentage"),
                "uncategorizedStressPercentage": stats_json.get("uncategorizedStressPercentage"),
                "lowStressPercentage": stats_json.get("lowStressPercentage"),
                "mediumStressPercentage": stats_json.get("mediumStressPercentage"),
                "highStressPercentage": stats_json.get("highStressPercentage"),
                
                "bodyBatteryChargedValue": stats_json.get("bodyBatteryChargedValue"),
                "bodyBatteryDrainedValue": stats_json.get("bodyBatteryDrainedValue"),
                "bodyBatteryHighestValue": stats_json.get("bodyBatteryHighestValue"),
                "bodyBatteryLowestValue": stats_json.get("bodyBatteryLowestValue"),
                "bodyBatteryDuringSleep": stats_json.get("bodyBatteryDuringSleep"),
                "bodyBatteryAtWakeTime": stats_json.get("bodyBatteryAtWakeTime"),
                
                "averageSpo2": stats_json.get("averageSpo2"),
                "lowestSpo2": stats_json.get("lowestSpo2"),
            }
        })
        if points_list:
            logging.info(f"Success : Fetching daily metrics for date {date_str}")
        return points_list
    else:
        logging.debug("No daily stat data available for the give date " + date_str)
        return []

def sleep_points(all_sleep_data, date_str, device_name):
    points_list = []
    sleep_json = all_sleep_data.get("dailySleepDTO", None)
    if sleep_json["sleepEndTimestampGMT"]:
        _end_ts   = sleep_json["sleepEndTimestampGMT"]
        _start_ts = sleep_json.get("sleepStartTimestampGMT")
        points_list.append({
        "measurement":  "SleepSummary",
        "time": datetime.fromtimestamp(_end_ts/1000, tz=pytz.timezone("UTC")).isoformat(),
        "tags": {
            "Device": device_name,
            "Database_Name": "GarminDB"
            },
        "fields": {
            "sleepStartTime": datetime.fromtimestamp(_start_ts/1000, tz=pytz.timezone("UTC")).isoformat() if _start_ts else None,
            "sleepEndTime":   datetime.fromtimestamp(_end_ts/1000,   tz=pytz.timezone("UTC")).isoformat(),
            "sleepTimeSeconds": sleep_json.get("sleepTimeSeconds"),
            "deepSleepSeconds": sleep_json.get("deepSleepSeconds"),
            "lightSleepSeconds": sleep_json.get("lightSleepSeconds"),
            "remSleepSeconds": sleep_json.get("remSleepSeconds"),
            "awakeSleepSeconds": sleep_json.get("awakeSleepSeconds"),
            "averageSpO2Value": sleep_json.get("averageSpO2Value"),
            "lowestSpO2Value": sleep_json.get("lowestSpO2Value"),
            "highestSpO2Value": sleep_json.get("highestSpO2Value"),
            "averageRespirationValue": sleep_json.get("averageRespirationValue"),
            "lowestRespirationValue": sleep_json.get("lowestRespirationValue"),
            "highestRespirationValue": sleep_json.get("highestRespirationValue"),
            "awakeCount": sleep_json.get("awakeCount"),
            "avgSleepStress": sleep_json.get("avgSleepStress"),
            "sleepScore": ((sleep_json.get("sleepScores") or {}).get("overall") or {}).get("value"),
            "restlessMomentsCount": all_sleep_data.get("restlessMomentsCount"),
            "avgOvernightHrv": all_sleep_data.get("avgOvernightHrv"),
            "bodyBatteryChange": all_sleep_data.get("bodyBatteryChange"),
            "restingHeartRate": all_sleep_data.get("restingHeartRate")
            }
        })
    sleep_movement_intraday = all_sleep_data.get("sleepMovement")
    if sleep_movement_intraday:
        for entry in sleep_movement_intraday:
            points_list.append({
                "measurement":  "SleepIntraday",
                "time": pytz.timezone("UTC").localize(datetime.strptime(entry["startGMT"], "%Y-%m-%dT%H:%M:%S.%f")).isoformat(),
                "tags": {
                    "Device": device_name,
                    "Database_Name": "GarminDB"
                },
                "fields": {
                    "SleepMovementActivityLevel": entry.get("activityLevel",-1),
                    "SleepMovementActivitySeconds": int((datetime.strptime(entry["endGMT"], "%Y-%m-%dT%H:%M:%S.%f") - datetime.strptime(entry["startGMT"], "%Y-%m-%dT%H:%M:%S.%f")).total_seconds())
                }
            })
    sleep_levels_intraday = all_sleep_data.get("sleepLevels")
    if sleep_levels_intraday:
        for entry in sleep_levels_intraday:
            if entry.get("activityLevel") or entry.get("activityLevel") == 0: # Include 0 for Deepsleep but not None - Refer to issue #43
                points_list.append({
                    "measurement":  "SleepIntraday",
                    "time": pytz.timezone("UTC").localize(datetime.strptime(entry["startGMT"], "%Y-%m-%dT%H:%M:%S.%f")).isoformat(),
                    "tags": {
                        "Device": device_name,
                        "Database_Name": "GarminDB"
                    },
                    "fields": {
                        "SleepStageLevel": entry.get("activityLevel"),
                        "SleepStageSeconds": int((datetime.strptime(entry["endGMT"], "%Y-%m-%dT%H:%M:%S.%f") - datetime.strptime(entry["startGMT"], "%Y-%m-%dT%H:%M:%S.%f")).total_seconds())
                    }
                })
        # Add additional duplicate terminal data point (see issue #127)
        if entry.get("endGMT"):
            points_list.append({
                "measurement":  "SleepIntraday",
                "time": pytz.timezone("UTC").localize(datetime.strptime(entry["endGMT"], "%Y-%m-%dT%H:%M:%S.%f")).isoformat(),
                "tags": {
                    "Device": device_name,
                    "Database_Name": "GarminDB"
                },
                "fields": {"SleepStageLevel": entry.get("activityLevel")} # Duplicating last entry for visualization in Grafana
            })
    sleep_restlessness_intraday = all_sleep_data.get("sleepRestlessMoments")
    if sleep_restlessness_intraday:
        for entry in sleep_restlessness_intraday:
            if entry.get("value"):
                points_list.append({
                    "measurement":  "SleepIntraday",
                    "time": datetime.fromtimestamp(entry["startGMT"]/1000, tz=pytz.timezone("UTC")).isoformat(),
                    "tags": {
                        "Device": device_name,
                        "Database_Name": "GarminDB"
                    },
                    "fields": {
                        "sleepRestlessValue": entry.get("value")
                    }
                })
    sleep_spo2_intraday = all_sleep_data.get("wellnessEpochSPO2DataDTOList")
    if sleep_spo2_intraday:
        for entry in sleep_spo2_intraday:
            if entry.get("spo2Reading"):
                points_list.append({
                    "measurement":  "SleepIntraday",
                    "time": pytz.timezone("UTC").localize(datetime.strptime(entry["epochTimestamp"], "%Y-%m-%dT%H:%M:%S.%f")).isoformat(),
                    "tags": {
                        "Device": device_name,
                        "Database_Name": "GarminDB"
                    },
                    "fields": {
                        "spo2Reading": entry.get("spo2Reading")
                    }
                })
    sleep_respiration_intraday = all_sleep_data.get("wellnessEpochRespirationDataDTOList")
    if sleep_respiration_intraday:
        for entry in sleep_respiration_intraday:
            if entry.get("respirationValue"):
                points_list.append({
                    "measurement":  "SleepIntraday",
                    "time": datetime.fromtimestamp(entry["startTimeGMT"]/1000, tz=pytz.timezone("UTC")).isoformat(),
                    "tags": {
                        "Device": device_name,
                        "Database_Name": "GarminDB"
                    },
                    "fields": {
                        "respirationValue": entry.get("respirationValue")
                    }
                })
    sleep_heart_rate_intraday = all_sleep_data.get("sleepHeartRate")
    if sleep_heart_rate_intraday:
        for entry in sleep_heart_rate_intraday:
            if entry.get("value"):
                points_list.append({
                    "measurement":  "SleepIntraday",
                    "time": datetime.fromtimestamp(entry["startGMT"]/1000, tz=pytz.timezone("UTC")).isoformat(),
                    "tags": {
                        "Device": device_name,
                        "Database_Name": "GarminDB"
                    },
                    "fields": {
                        "heartRate": entry.get("value")
                    }
                })
    sleep_stress_intraday = all_sleep_data.get("sleepStress")
    if sleep_stress_intraday:
        for entry in sleep_stress_intraday:
            if entry.get("value"):
                points_list.append({
                    "measurement":  "SleepIntraday",
                    "time": datetime.fromtimestamp(entry["startGMT"]/1000, tz=pytz.timezone("UTC")).isoformat(),
                    "tags": {
                        "Device": device_name,
                        "Database_Name": "GarminDB"
                    },
                    "fields": {
                        "stressValue": entry.get("value")
                    }
                })
    sleep_bb_intraday = all_sleep_data.get("sleepBodyBattery")
    if sleep_bb_intraday:
        for entry in sleep_bb_intraday:
            if entry.get("value"):
                points_list.append({
                    "measurement":  "SleepIntraday",
                    "time": datetime.fromtimestamp(entry["startGMT"]/1000, tz=pytz.timezone("UTC")).isoformat(),
                    "tags": {
                        "Device": device_name,
                        "Database_Name": "GarminDB"
                    },
                    "fields": {
                        "bodyBattery": entry.get("value")
                    }
                })
    sleep_hrv_intraday = all_sleep_data.get("hrvData")
    if sleep_hrv_intraday:
        for entry in sleep_hrv_intraday:
            if entry.get("value"):
                points_list.append({
                    "measurement":  "SleepIntraday",
                    "time": datetime.fromtimestamp(entry["startGMT"]/1000, tz=pytz.timezone("UTC")).isoformat(),
                    "tags": {
                        "Device": device_name,
                        "Database_Name": "GarminDB"
                    },
                    "fields": {
                        "hrvData": entry.get("value")
                    }
                })
    if points_list:
        logging.info(f"Success : Fetching intraday sleep metrics for date {date_str}")
    return points_list

def intraday_hr_points(hr_json, date_str, device_name):
    points_list = []
    hr_list = hr_json.get("heartRateValues") or []
    for entry in hr_list:
        if entry[1]:
            points_list.append({
                    "measurement":  "HeartRateIntraday",
                    "time": datetime.fromtimestamp(entry[0]/1000, tz=pytz.timezone("UTC")).isoformat(),
                    "tags": {
                        "Device": device_name,
                        "Database_Name": "GarminDB"
                    },
                    "fields": {
                        "HeartRate": entry[1]
                    }
                })
    if points_list:
        logging.info(f"Success : Fetching intraday Heart Rate for date {date_str}")
    return points_list

def intraday_steps_points(steps_list, date_str, device_name):
    points_list = []
    for entry in steps_list:
        if entry["steps"] or entry["steps"] == 0:
            points_list.append({
                    "measurement":  "StepsIntraday",
                    "time": pytz.timezone("UTC").localize(datetime.strptime(entry['startGMT'], "%Y-%m-%dT%H:%M:%S.%f")).isoformat(),
                    "tags": {
                        "Device": device_name,
                        "Database_Name": "GarminDB"
                    },
                    "fields": {
                        "StepsCount": entry["steps"]
                    }
                })
    if points_list:
        logging.info(f"Success : Fetching intraday steps for date {date_str}")
    return points_list

def intraday_stress_points(stress_json, date_str, device_name):
    points_list = []
    stress_list = stress_json.get('stressValuesArray') or []
    for entry in stress_list:
        if entry[1] or entry[1] == 0:
            points_list.append({
                    "measurement":  "StressIntraday",
                    "time": datetime.fromtimestamp(entry[0]/1000, tz=pytz.timezone("UTC")).isoformat(),
                    "tags": {
                        "Device": device_name,
                        "Database_Name": "GarminDB"
                    },
                    "fields": {
                        "stressLevel": entry[1]
                    }
                })
    bb_list = stress_json.get('bodyBatteryValuesArray') or []
    for entry in bb_list:
        if entry[2] or entry[2] == 0:
            points_list.append({
                    "measurement":  "BodyBatteryIntraday",
                    "time": datetime.fromtimestamp(entry[0]/1000, tz=pytz.timezone("UTC")).isoformat(),
                    "tags": {
                        "Device": device_name,
                        "Database_Name": "GarminDB"
                    },
                    "fields": {
                        "BodyBatteryLevel": entry[2]
                    }
                })
    if points_list:
        logging.info(f"Success : Fetching intraday stress and Body Battery values for date {date_str}")
    return points_list

def intraday_br_points(br_json, date_str, device_name):
    points_list = []
    br_list = br_json.get('respirationValuesArray') or []
    for entry in br_list:
        if entry[1]:
            points_list.append({
                    "measurement":  "BreathingRateIntraday",
                    "time": datetime.fromtimestamp(entry[0]/1000, tz=pytz.timezone("UTC")).isoformat(),
                    "tags": {
                        "Device": device_name,
                        "Database_Name": "GarminDB"
                    },
                    "fields": {
                        "BreathingRate": entry[1]
                    }
                })
    if points_list:
        logging.info(f"Success : Fetching intraday Breathing Rate for date {date_str}")
    return points_list

def intraday_hrv_points(hrv_json, date_str, device_name):
    points_list = []
    hrv_list = hrv_json.get('hrvReadings') or []
    for entry in hrv_list:
        if entry.get('hrvValue'):
            points_list.append({
                    "measurement":  "HRV_Intraday",
                    "time": pytz.timezone("UTC").localize(datetime.strptime(entry['readingTimeGMT'],"%Y-%m-%dT%H:%M:%S.%f")).isoformat(),
                    "tags": {
                        "Device": device_name,
                        "Database_Name": "GarminDB"
                    },
                    "fields": {
                        "hrvValue": entry.get('hrvValue')
                    }
                })
    if points_list:
        logging.info(f"Success : Fetching intraday HRV for date {date_str}")
    return points_list

def body_composition_points(weigh_ins, date_str, device_name):
    points_list = []
    weight_list_all = weigh_ins.get('dailyWeightSummaries', [])
    if weight_list_all:
        weight_list = weight_list_all[0].get('allWeightMetrics', [])
        for weight_dict in weight_list:
            data_fields = {
                    "weight": weight_dict.get("weight"),
                    "bmi": weight_dict.get("bmi"),
                    "bodyFat": weight_dict.get("bodyFat"),
                    "bodyWater": weight_dict.get("bodyWater"),
                    "boneMass": weight_dict.get("boneMass"),
                    "muscleMass": weight_dict.get("muscleMass"),
                    "physiqueRating": weight_dict.get("physiqueRating"),
                    "visceralFat": weight_dict.get("visceralFat"),
                    # Garmin returns metabolicAge as a millisecond DURATION
                    # (e.g. ~7.26e11 ms ≈ 23 years) — convert to years here so
                    # the DB stores sane values. The >1000 guard (no metabolic
                    # age exceeds a millennium) passes through values already
                    # in years; 31,556,952,000 ms = one Gregorian year.
                    "metabolicAge": (round(weight_dict["metabolicAge"] / 31_556_952_000, 1)
                                     if (weight_dict.get("metabolicAge") or 0) > 1000
                                     else weight_dict.get("metabolicAge")),
                }
            if not all(value is None for value in data_fields.values()):
                points_list.append({
                    "measurement":  "BodyComposition",
                    "time": datetime.fromtimestamp((weight_dict['timestampGMT']/1000) , tz=pytz.timezone("UTC")).isoformat() if weight_dict['timestampGMT'] else datetime.strptime(date_str, "%Y-%m-%d").replace(hour=0, tzinfo=pytz.UTC).isoformat(), # Use GMT 00:00 is timestamp is not available (issue #15)
                    "tags": {
                        "Device": device_name,
                        "Database_Name": "GarminDB",
                        "Frequency" : "Intraday",
                        "SourceType" : weight_dict.get('sourceType', "Unknown")
                    },
                    "fields": data_fields
                })
        logging.info(f"Success : Fetching intraday Body Composition (Weight, BMI etc) for date {date_str}")
    return points_list

def training_status_points(ts_list_all, date_str, device_name):
    points_list = []
    ts_training_data_all = (ts_list_all.get("mostRecentTrainingStatus") or {}).get("latestTrainingStatusData", {})

    # Heat & altitude acclimation lives in the same training-status payload, but
    # Garmin returns it at the TOP LEVEL of the response (verified 2026-06-01
    # against live responses for both accounts) — not nested inside each device's
    # dict. Read it from the top level, with a per-device fallback in case Garmin
    # moves it, and check both historical key spellings.
    top_acclim = (
        ts_list_all.get("heatAltitudeAcclimationDTO")
        or ts_list_all.get("heatAltitudeAcclimation")
        or {}
    )

    if ts_training_data_all:
        for device_id, ts_dict in ts_training_data_all.items():
            logging.info(f"Success : Processing Training Status for Device {device_id}")
            acclim = (
                top_acclim
                or ts_dict.get("heatAltitudeAcclimationDTO")
                or ts_dict.get("heatAltitudeAcclimation")
                or {}
            )
            data_fields = {
                "trainingStatus": ts_dict.get("trainingStatus"),
                "trainingStatusFeedbackPhrase": ts_dict.get("trainingStatusFeedbackPhrase"),
                "weeklyTrainingLoad": ts_dict.get("weeklyTrainingLoad"),
                "fitnessTrend": ts_dict.get("fitnessTrend"),
                "acwrPercent": (ts_dict.get("acuteTrainingLoadDTO") or {}).get("acwrPercent"),
                "dailyTrainingLoadAcute": (ts_dict.get("acuteTrainingLoadDTO") or {}).get("dailyTrainingLoadAcute"),
                "dailyTrainingLoadChronic": (ts_dict.get("acuteTrainingLoadDTO") or {}).get("dailyTrainingLoadChronic"),
                "maxTrainingLoadChronic": (ts_dict.get("acuteTrainingLoadDTO") or {}).get("maxTrainingLoadChronic"),
                "minTrainingLoadChronic": (ts_dict.get("acuteTrainingLoadDTO") or {}).get("minTrainingLoadChronic"),
                "dailyAcuteChronicWorkloadRatio": (ts_dict.get("acuteTrainingLoadDTO") or {}).get("dailyAcuteChronicWorkloadRatio"),
                "heatAcclimationPercentage": acclim.get("heatAcclimationPercentage"),
                "altitudeAcclimationPercentage": acclim.get("altitudeAcclimationPercentage"),
                "heatTrend": acclim.get("heatTrend"),
                "altitudeTrend": acclim.get("altitudeTrend"),
                "currentAltitude": acclim.get("currentAltitude"),
            }
            if ts_dict.get("timestamp") and any(value is not None for value in data_fields.values()):
                points_list.append({
                    "measurement": "TrainingStatus",
                    "time": datetime.fromtimestamp(ts_dict["timestamp"]/1000, tz=pytz.timezone("UTC")).isoformat(),
                    "tags": {
                        "Device": device_name,
                        "Database_Name": "GarminDB"
                    },
                    "fields": data_fields
                })
                logging.info(f"Success : Fetching Training Status for date {date_str}")
    return points_list

def training_readiness_points(tr_list_all, date_str, device_name):
    points_list = []
    if tr_list_all:
        for tr_dict in tr_list_all:
            data_fields = {
                    "level": tr_dict.get("level"),
                    "score": tr_dict.get("score"),
                    "sleepScore": tr_dict.get("sleepScore"),
                    "sleepScoreFactorPercent": tr_dict.get("sleepScoreFactorPercent"),
                    "recoveryTime": tr_dict.get("recoveryTime"),
                    "recoveryTimeFactorPercent": tr_dict.get("recoveryTimeFactorPercent"),
                    "acwrFactorPercent": tr_dict.get("acwrFactorPercent"),
                    "acuteLoad": tr_dict.get("acuteLoad"),
                    "stressHistoryFactorPercent": tr_dict.get("stressHistoryFactorPercent"),
                    "hrvFactorPercent": tr_dict.get("hrvFactorPercent"),
                }
            if (not all(value is None for value in data_fields.values())) and tr_dict.get('timestamp'):
                points_list.append({
                    "measurement":  "TrainingReadiness",
                    "time": pytz.timezone("UTC").localize(datetime.strptime(tr_dict['timestamp'],"%Y-%m-%dT%H:%M:%S.%f")).isoformat(),
                    "tags": {
                        "Device": device_name,
                        "Database_Name": "GarminDB"
                    },
                    "fields": data_fields
                })
                logging.info(f"Success : Fetching Training Readiness for date {date_str}")
    return points_list

def hill_score_points(hill, date_str, device_name):
    points_list = []
    if hill:
        data_fields = {
            "strengthScore": hill.get("strengthScore"),
            "enduranceScore": hill.get("enduranceScore"),
            "hillScoreClassificationId": hill.get("hillScoreClassificationId"),
            "overallScore": hill.get("overallScore"),
            "hillScoreFeedbackPhraseId": hill.get("hillScoreFeedbackPhraseId"),
            "vo2MaxPreciseValue": hill.get("vo2MaxPreciseValue")
        }
        if not all(value is None for value in data_fields.values()):
            points_list.append({
                "measurement":  "HillScore",
                "time": datetime.strptime(date_str,"%Y-%m-%d").replace(hour=0, tzinfo=pytz.UTC).isoformat(), # Use GMT 00:00 for daily record
                "tags": {
                    "Device": device_name,
                    "Database_Name": "GarminDB"
                },
                "fields": data_fields
            })
            logging.info(f"Success : Fetching Hill Score for date {date_str}")
    return points_list

def race_prediction_points(rp_all_list, date_str, device_name):
    points_list = []
    rp_all = rp_all_list[0] if len(rp_all_list) > 0 else {}
    if rp_all:
        data_fields = {
            "time5K": rp_all.get("time5K"),
            "time10K": rp_all.get("time10K"),
            "timeHalfMarathon": rp_all.get("timeHalfMarathon"),
            "timeMarathon": rp_all.get("timeMarathon"),
        }
        if not all(value is None for value in data_fields.values()):
            points_list.append({
                "measurement":  "RacePredictions",
                "time": datetime.strptime(date_str,"%Y-%m-%d").replace(hour=0, tzinfo=pytz.UTC).isoformat(), # Use GMT 00:00 for daily record
                "tags": {
                    "Device": device_name,
                    "Database_Name": "GarminDB"
                },
                "fields": data_fields
            })
            logging.info(f"Success : Fetching Race Predictions for date {date_str}")
    return points_list

def fitness_age_points(fitness_age, date_str, device_name):
    points_list = []

    if fitness_age:
            data_fields = {
                "chronologicalAge": float(fitness_age.get("chronologicalAge")) if fitness_age.get("chronologicalAge") else None,
                "fitnessAge": fitness_age.get("fitnessAge"),
                "achievableFitnessAge": fitness_age.get("achievableFitnessAge"),
            }

            if not all(value is None for value in data_fields.values()):
                points_list.append({
                    "measurement": "FitnessAge",
                    "time": datetime.strptime(date_str,"%Y-%m-%d").replace(hour=0, tzinfo=pytz.UTC).isoformat(), # Use GMT 00:00 for daily record
                    "tags": {
                        "Device": device_name,
                        "Database_Name": "GarminDB"
                    },
                    "fields": data_fields
                })
                logging.info(f"Success : Fetching Fitness Age for date {date_str}")
    return points_list

def vo2_max_points(max_metrics, date_str, device_name):
    points_list = []
    try:
        if max_metrics:
            vo2_max_value = (max_metrics[0].get("generic") or {}).get("vo2MaxPreciseValue", None)
            vo2_max_value_cycling = (max_metrics[0].get("cycling") or {}).get("vo2MaxPreciseValue", None)
            if vo2_max_value or vo2_max_value_cycling:
                points_list.append({
                    "measurement":  "VO2_Max",
                    "time": datetime.strptime(date_str,"%Y-%m-%d").replace(hour=0, tzinfo=pytz.UTC).isoformat(), # Use GMT 00:00 for daily record
                    "tags": {
                        "Device": device_name,
                        "Database_Name": "GarminDB"
                    },
                    "fields": {"VO2_max_value" : vo2_max_value, "VO2_max_value_cycling" : vo2_max_value_cycling}
                })
                logging.info(f"Success : Fetching VO2-max for date {date_str}")
        return points_list
    except AttributeError as err:
        return []

def endurance_score_points(endurance_dict, date_str, device_name):
    points_list = []
    if endurance_dict:
        if endurance_dict.get("overallScore"):
            points_list.append({
                "measurement":  "EnduranceScore",
                "time": pytz.timezone("UTC").localize(datetime.strptime(date_str,"%Y-%m-%d")).isoformat(), # Use GMT 00:00 is timestamp is not available
                "tags": {
                    "Device": device_name,
                    "Database_Name": "GarminDB"
                },
                "fields": {
                    "EnduranceScore": endurance_dict.get("overallScore")
                    }
            })
            logging.info(f"Success : Fetching Endurance Score for date {date_str}")
    return points_list

def blood_pressure_points(bp_payload, date_str, device_name):
    points_list = []
    bp_all = bp_payload.get('measurementSummaries',[])
    if len(bp_all) > 0:
        bp_list = bp_all[0].get('measurements',[])
        for bp_measurement in bp_list:
            data_fields = {
                'Systolic': bp_measurement.get('systolic', None),
                "Diastolic": bp_measurement.get('diastolic', None),
                "Pulse": bp_measurement.get('pulse', None)
            }
            if not all(value is None for value in data_fields.values()) and 'measurementTimestampGMT' in bp_measurement:
                points_list.append({
                    "measurement":  "BloodPressure",
                    "time": pytz.UTC.localize(datetime.strptime(bp_measurement['measurementTimestampGMT'], '%Y-%m-%dT%H:%M:%S.%f')),
                    "tags": {
                        "Device": device_name,
                        "Database_Name": "GarminDB",
                        "Source": bp_measurement.get('sourceType', None)
                    },
                    "fields": data_fields
                })
        logging.info(f"Success : Fetching Blood Pressure for date {date_str}")
    return points_list

def hydration_points(hydration_dict, date_str, device_name):
    points_list = []
    data_fields = {
        'ValueInML': hydration_dict.get('valueInML', None),
        "SweatLossInML": hydration_dict.get('sweatLossInML', None),
        "GoalInML": hydration_dict.get('goalInML', None),
        "ActivityIntakeInML": hydration_dict.get('activityIntakeInML', None)
    }
    if not all(value is None for value in data_fields.values()):
        points_list.append({
            "measurement":  "Hydration",
            "time": datetime.strptime(date_str,"%Y-%m-%d").replace(hour=0, tzinfo=pytz.UTC).isoformat(), # Use GMT 00:00 for daily record
            "tags": {
                "Device": device_name,
                "Database_Name": "GarminDB"
            },
            "fields": data_fields
        })
        logging.info(f"Success : Fetching Hydration data for date {date_str}")
    return points_list

def lifestyle_points(journal_data, date_str, device_name):
    points_list = []
    try:
        logging.info(f"Fetching Lifestyle Journaling data for date {date_str}")
        
        daily_logs = journal_data.get('dailyLogsReport', [])
        
        for log in daily_logs:
            behavior_name = log.get('name') or log.get('behavior')
            if not behavior_name:
                continue

            category = log.get('category', 'UNKNOWN')
            log_status = log.get('logStatus')
            details = log.get('details', [])
            
            # status: 1 for YES, 0 for NO
            status = 1 if log_status == "YES" else 0
            
            # value: sum of detail amounts if available, else 0.0
            value = 0.0
            if details:
                for detail in details:
                    amount = detail.get('amount')
                    if amount is not None:
                        value += float(amount)

            fields = {
                "status": status,
                "value": value
            }

            points_list.append({
                "measurement": "LifestyleJournal",
                "time": pytz.timezone("UTC").localize(datetime.strptime(date_str, "%Y-%m-%d")).isoformat(),
                "tags": {
                    "Device": device_name,
                    "Database_Name": "GarminDB",
                    "behavior": behavior_name,
                    "category": category
                },
                "fields": fields
            })
            
        logging.info(f"Success : Fetching Lifestyle Journaling data for date {date_str}")

    except Exception as e:
        logging.warning(f"Failed to fetch Lifestyle Journaling data for date {date_str}: {e}")
    
    return points_list

def menstrual_points(data, date_str, device_name):
    global _menstrual_shape_warned
    points_list = []
    try:
        if not data or not isinstance(data, dict):
            logging.debug(f"No menstrual data available for date {date_str}")
            return []

        # Parse both the pre- and post-May-2026 shapes — the old parser
        # silently skipped every date for ~8 weeks after the rename.
        summary = data.get('summary') or data.get('daySummary') or {}
        day = data.get('dayData') or data.get('day') or data.get('dayLog') or {}
        if not isinstance(summary, dict):
            summary = {}
        if not isinstance(day, dict):
            day = {}

        symptoms = day.get('symptoms')
        if isinstance(symptoms, list):
            symptoms = ','.join(str(s.get('name', s) if isinstance(s, dict) else s) for s in symptoms)

        cycle_start = summary.get('cycleStartDate') or summary.get('startDate')
        day_of_cycle = (summary.get('currentDayOfCycle') or day.get('currentDayOfCycle')
                        or summary.get('dayInCycle'))
        cycle_phase = summary.get('currentCyclePhase') or day.get('cyclePhase')
        if not cycle_phase and summary.get('currentPhase') is not None:
            cycle_phase = _MENSTRUAL_PHASE_NAMES.get(summary['currentPhase'],
                                                     str(summary['currentPhase']))
        flow = day.get('menstrualFlow') or day.get('flow')

        # Skip silently if there's no meaningful menstrual data for the day.
        if not any([cycle_start, cycle_phase, flow, day_of_cycle]):
            # A payload with real content we failed to parse means the shape
            # changed AGAIN — surface it once per run instead of going
            # silently dark like last time. Non-tracking users get {} or
            # all-None values, which stays quiet.
            if any(v for v in data.values() if v) and not _menstrual_shape_warned:
                _menstrual_shape_warned = True
                logging.warning(f"Menstrual response for {date_str} has keys {list(data.keys())} "
                                "but no recognisable cycle fields — Garmin may have changed the "
                                "response shape again")
            logging.debug(f"No menstrual data tracked for date {date_str}")
            return []

        points_list.append({
            "measurement": "MenstrualCycle",
            "time": datetime.strptime(date_str, "%Y-%m-%d").replace(hour=12, tzinfo=pytz.UTC).isoformat(),
            "tags": {
                "Device": device_name,
                "Database_Name": "GarminDB",
            },
            "fields": {
                "date": date_str,
                "cycleStartDate": cycle_start,
                "currentDayOfCycle": day_of_cycle,
                "currentCyclePhase": cycle_phase,
                "cycleLength": summary.get('cycleLength'),
                "predictedCycleLength": summary.get('predictedCycleLength') or summary.get('predictedMenstrualCycleLength'),
                "periodLength": summary.get('periodLength') or summary.get('predictedPeriodLength'),
                "menstrualFlow": flow,
                "pregnancyStatus": summary.get('pregnancyStatus') or day.get('pregnancyStatus'),
                "symptoms": symptoms,
                "mood": day.get('mood'),
                "notes": day.get('notes'),
                "rawJson": json.dumps(data),
            }
        })
        logging.info(f"Success : Fetching menstrual cycle data for date {date_str} (phase={cycle_phase}, day {day_of_cycle})")
    except Exception as e:
        # Silent skip on error - user may not have cycle tracking enabled.
        logging.debug(f"Skipping menstrual data for date {date_str}: {e}")
    return points_list


def is_http_500(err: Any) -> bool:
    """True when ``err`` represents an HTTP 500 from Garmin.

    Two shapes reach the caller: ``requests.exceptions.HTTPError``, which
    carries ``.response.status_code``, and ``GarthHTTPError``, which may carry
    either its own ``.status_code`` or a wrapped response. 500s are retried a
    bounded number of times (Garmin returns them transiently); every other HTTP
    error skips the date immediately, so misclassifying one stalls the backfill
    or silently drops a day.
    """
    if getattr(err, "status_code", None) == 500:
        return True
    response = getattr(err, "response", None)
    return getattr(response, "status_code", None) == 500
