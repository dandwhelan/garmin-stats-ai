"""Garmin JSON → points transforms.

Extracted from ``garmin_fetch`` (which cannot be imported without a populated
environment and a live DB connection) so this logic can be tested at all.

The timestamp conventions are the point. Garmin mixes three encodings across
endpoints, and each metric has its own keying rule — get one wrong and a whole
series silently shifts by a day, with nothing failing anywhere.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import pytz

pytest.importorskip("garmin_grafana", reason="garmin-grafana not installed")

from garmin_grafana import transforms as T  # noqa: E402

DEV = "testdev"


def _tags_ok(point):
    return point["tags"] == {"Device": DEV, "Database_Name": "GarminDB"}


# ==================================================================
# iter_days
# ==================================================================
def test_iter_days_walks_backwards():
    """Reverse chronological on purpose: the freshest days land first, so a
    mid-run failure still leaves the most-wanted data written."""
    assert list(T.iter_days("2026-07-01", "2026-07-04")) == [
        "2026-07-04", "2026-07-03", "2026-07-02", "2026-07-01"]


def test_iter_days_is_inclusive_at_both_ends():
    assert list(T.iter_days("2026-07-01", "2026-07-01")) == ["2026-07-01"]


def test_iter_days_yields_nothing_when_inverted():
    assert list(T.iter_days("2026-07-04", "2026-07-01")) == []


def test_iter_days_crosses_a_month_boundary():
    assert list(T.iter_days("2026-06-29", "2026-07-02")) == [
        "2026-07-02", "2026-07-01", "2026-06-30", "2026-06-29"]


def test_iter_days_handles_a_leap_day():
    assert "2028-02-29" in list(T.iter_days("2028-02-27", "2028-03-01"))


# ==================================================================
# Daily stats — the noon-UTC keying
# ==================================================================
def _stats(**over):
    payload = {"wellnessStartTimeGmt": "2026-06-30T23:00:00.0",
               "totalSteps": 9000, "restingHeartRate": 55}
    payload.update(over)
    return payload


def test_daily_stats_is_stamped_at_noon_utc_of_the_requested_date():
    """NOT the payload's own wellnessStartTimeGmt, which is local midnight in
    UTC — for a BST user that falls on the previous UTC day, so every row used
    to be filed under the wrong date."""
    points = T.daily_stats_points(_stats(), "2026-07-01", DEV)
    assert len(points) == 1
    assert points[0]["time"] == "2026-07-01T12:00:00+00:00"


def test_daily_stats_timestamp_prefix_always_equals_the_requested_date():
    """The SQLite writer keys the row on timestamp[:10]; noon is chosen so this
    holds at every UTC offset, not just for UK users."""
    for date_str in ("2020-01-01", "2024-06-30", "2025-12-31"):
        point = T.daily_stats_points(_stats(), date_str, DEV)[0]
        assert point["time"][:10] == date_str


def test_daily_stats_carries_the_expected_measurement_and_tags():
    point = T.daily_stats_points(_stats(), "2026-07-01", DEV)[0]
    assert point["measurement"] == "DailyStats"
    assert _tags_ok(point)


def test_daily_stats_maps_the_documented_fields():
    payload = _stats(totalSteps=12345, restingHeartRate=48, sedentarySeconds=33000,
                     bodyBatteryDuringSleep=40, averageSpo2=96.0, lowestSpo2=90.0)
    fields = T.daily_stats_points(payload, "2026-07-01", DEV)[0]["fields"]
    assert fields["totalSteps"] == 12345
    assert fields["restingHeartRate"] == 48
    # Keys the daily-summary cache and the KB rules depend on.
    assert fields["sedentarySeconds"] == 33000
    assert fields["bodyBatteryDuringSleep"] == 40
    assert fields["lowestSpo2"] == 90.0


def test_daily_stats_keeps_absent_fields_as_none_not_zero():
    """Zero reads as 'measured and zero'; None reads as 'not measured'."""
    fields = T.daily_stats_points(_stats(), "2026-07-01", DEV)[0]["fields"]
    assert fields["floorsAscended"] is None
    assert fields["highStressDuration"] is None


def test_daily_stats_skips_a_day_with_no_wellness_data():
    assert T.daily_stats_points(_stats(wellnessStartTimeGmt=None), "2026-07-01", DEV) == []


def test_daily_stats_still_writes_today():
    """The guard compares against datetime.today(), which carries the current
    TIME — so today's midnight is strictly less than "now" and today is
    written. Today's row is a partial snapshot; it is the daily-summary
    cache's is_complete flag, not this function, that keeps baselines from
    treating it as final.

    Pinned because the comparison reads at a glance like it excludes today.
    """
    today = datetime.today().strftime("%Y-%m-%d")
    assert len(T.daily_stats_points(_stats(), today, DEV)) == 1


def test_daily_stats_skips_future_dates():
    future = (datetime.today() + timedelta(days=3)).strftime("%Y-%m-%d")
    assert T.daily_stats_points(_stats(), future, DEV) == []


def test_daily_stats_accepts_yesterday():
    yesterday = (datetime.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    assert len(T.daily_stats_points(_stats(), yesterday, DEV)) == 1


def test_daily_stats_raises_on_a_payload_missing_the_wellness_key():
    """Pinned as-is, not "fixed". The transform indexes the key directly, so a
    malformed payload propagates to fetch_write_bulk's handler, which skips the
    whole date. Making it lenient would be a behaviour change, and this
    extraction is deliberately behaviour-preserving — the failure path is
    recorded so any future change to it is a visible decision."""
    with pytest.raises(KeyError):
        T.daily_stats_points({}, "2026-07-01", DEV)


# ==================================================================
# Sleep — keyed to the WAKE timestamp
# ==================================================================
WAKE_MS = 1751356800000     # 2026-07-01T08:00:00Z
BED_MS = 1751328000000      # 2026-06-30T00:00:00Z + ... (previous evening)


def _sleep(**over):
    dto = {
        "sleepEndTimestampGMT": WAKE_MS,
        "sleepStartTimestampGMT": BED_MS,
        "sleepTimeSeconds": 25200,
        "deepSleepSeconds": 5000,
        "sleepScores": {"overall": {"value": 82}},
    }
    dto.update(over.pop("dto", {}))
    payload = {"dailySleepDTO": dto, "avgOvernightHrv": 61.0, "restingHeartRate": 52}
    payload.update(over)
    return payload


def test_sleep_summary_is_stamped_at_the_wake_time():
    """A sleep record dated X is the night that ENDED on the morning of X —
    last night's sleep lives on today's date, not yesterday's."""
    summary = T.sleep_points(_sleep(), "2026-07-01", DEV)[0]
    assert summary["measurement"] == "SleepSummary"
    assert summary["time"] == datetime.fromtimestamp(WAKE_MS / 1000, tz=pytz.timezone("UTC")).isoformat()


def test_sleep_summary_start_and_end_are_both_recorded():
    fields = T.sleep_points(_sleep(), "2026-07-01", DEV)[0]["fields"]
    assert fields["sleepEndTime"] == datetime.fromtimestamp(WAKE_MS / 1000, tz=pytz.timezone("UTC")).isoformat()
    assert fields["sleepStartTime"] == datetime.fromtimestamp(BED_MS / 1000, tz=pytz.timezone("UTC")).isoformat()


def test_sleep_start_may_be_missing():
    fields = T.sleep_points(_sleep(dto={"sleepStartTimestampGMT": None}), "2026-07-01", DEV)[0]["fields"]
    assert fields["sleepStartTime"] is None
    assert fields["sleepEndTime"] is not None


def test_sleep_score_is_dug_out_of_the_nested_structure():
    assert T.sleep_points(_sleep(), "2026-07-01", DEV)[0]["fields"]["sleepScore"] == 82


@pytest.mark.parametrize("scores", [{}, {"overall": None}, {"overall": {}}])
def test_missing_sleep_score_is_none_not_an_error(scores):
    fields = T.sleep_points(_sleep(dto={"sleepScores": scores}), "2026-07-01", DEV)[0]["fields"]
    assert fields["sleepScore"] is None


def test_top_level_sleep_fields_are_merged_into_the_summary():
    """avgOvernightHrv / restingHeartRate sit outside dailySleepDTO."""
    fields = T.sleep_points(_sleep(), "2026-07-01", DEV)[0]["fields"]
    assert fields["avgOvernightHrv"] == 61.0
    assert fields["restingHeartRate"] == 52


def test_no_summary_when_the_night_never_ended():
    """An in-progress or absent sleep has no end timestamp."""
    points = T.sleep_points(_sleep(dto={"sleepEndTimestampGMT": None}), "2026-07-01", DEV)
    assert all(p["measurement"] != "SleepSummary" for p in points)


def test_sleep_raises_when_the_payload_has_no_dto():
    """Same as daily stats: the failure path is recorded, not changed."""
    with pytest.raises((TypeError, AttributeError)):
        T.sleep_points({}, "2026-07-01", DEV)


# ------------------------------------------------------------------
# Sleep intraday series
# ------------------------------------------------------------------
def _gmt(hour):
    return f"2026-07-01T{hour:02d}:00:00.0"


def test_sleep_movement_records_its_duration():
    payload = _sleep(sleepMovement=[
        {"startGMT": _gmt(1), "endGMT": _gmt(2), "activityLevel": 0.4}])
    movement = [p for p in T.sleep_points(payload, "2026-07-01", DEV)
                if "SleepMovementActivityLevel" in p["fields"]]
    assert len(movement) == 1
    assert movement[0]["fields"]["SleepMovementActivitySeconds"] == 3600
    assert movement[0]["time"].startswith("2026-07-01T01:00:00")


def test_missing_movement_level_defaults_to_minus_one():
    payload = _sleep(sleepMovement=[{"startGMT": _gmt(1), "endGMT": _gmt(2)}])
    movement = [p for p in T.sleep_points(payload, "2026-07-01", DEV)
                if "SleepMovementActivityLevel" in p["fields"]]
    assert movement[0]["fields"]["SleepMovementActivityLevel"] == -1


def test_sleep_stage_level_zero_is_kept():
    """Level 0 is Deep sleep — dropping falsy values would silently delete the
    single most interesting stage."""
    payload = _sleep(sleepLevels=[{"startGMT": _gmt(1), "endGMT": _gmt(2), "activityLevel": 0}])
    stages = [p for p in T.sleep_points(payload, "2026-07-01", DEV) if "SleepStageSeconds" in p["fields"]]
    assert len(stages) == 1
    assert stages[0]["fields"]["SleepStageLevel"] == 0


def test_sleep_stage_none_is_dropped():
    payload = _sleep(sleepLevels=[{"startGMT": _gmt(1), "endGMT": _gmt(2), "activityLevel": None}])
    assert [p for p in T.sleep_points(payload, "2026-07-01", DEV) if "SleepStageSeconds" in p["fields"]] == []


def test_a_terminal_stage_point_is_duplicated_at_the_end_time():
    """Without it Grafana draws the final stage as a zero-width sliver at its
    start instead of across its real span."""
    payload = _sleep(sleepLevels=[
        {"startGMT": _gmt(1), "endGMT": _gmt(2), "activityLevel": 0},
        {"startGMT": _gmt(2), "endGMT": _gmt(5), "activityLevel": 2},
    ])
    stage_points = [p for p in T.sleep_points(payload, "2026-07-01", DEV)
                    if "SleepStageLevel" in p["fields"]]
    terminal = stage_points[-1]
    assert terminal["time"].startswith("2026-07-01T05:00:00")
    assert "SleepStageSeconds" not in terminal["fields"]


@pytest.mark.parametrize("key,field,ts_key,ms", [
    ("sleepRestlessMoments", "sleepRestlessValue", "startGMT", True),
    ("sleepHeartRate", "heartRate", "startGMT", True),
    ("sleepStress", "stressValue", "startGMT", True),
    ("sleepBodyBattery", "bodyBattery", "startGMT", True),
    ("hrvData", "hrvData", "startGMT", True),
])
def test_epoch_ms_sleep_series_are_mapped(key, field, ts_key, ms):
    payload = _sleep(**{key: [{ts_key: WAKE_MS, "value": 42}]})
    matching = [p for p in T.sleep_points(payload, "2026-07-01", DEV) if field in p["fields"]]
    assert len(matching) == 1
    assert matching[0]["fields"][field] == 42
    assert matching[0]["measurement"] == "SleepIntraday"


def test_spo2_series_uses_a_naive_gmt_string_not_epoch_ms():
    payload = _sleep(wellnessEpochSPO2DataDTOList=[
        {"epochTimestamp": _gmt(3), "spo2Reading": 94}])
    matching = [p for p in T.sleep_points(payload, "2026-07-01", DEV) if "spo2Reading" in p["fields"]]
    assert matching[0]["time"].startswith("2026-07-01T03:00:00")


def test_respiration_series_uses_epoch_ms_under_a_different_key():
    payload = _sleep(wellnessEpochRespirationDataDTOList=[
        {"startTimeGMT": WAKE_MS, "respirationValue": 13.5}])
    matching = [p for p in T.sleep_points(payload, "2026-07-01", DEV) if "respirationValue" in p["fields"]]
    assert matching[0]["time"] == datetime.fromtimestamp(WAKE_MS / 1000, tz=pytz.timezone("UTC")).isoformat()


def test_all_sleep_intraday_points_share_the_measurement_name():
    """They share (time, device) as a primary key and are merged with COALESCE,
    so they must all land in SleepIntraday or they clobber each other."""
    payload = _sleep(
        sleepHeartRate=[{"startGMT": WAKE_MS, "value": 50}],
        sleepStress=[{"startGMT": WAKE_MS, "value": 20}],
        sleepBodyBattery=[{"startGMT": WAKE_MS, "value": 70}],
    )
    intraday = [p for p in T.sleep_points(payload, "2026-07-01", DEV) if p["measurement"] != "SleepSummary"]
    assert intraday
    assert all(p["measurement"] == "SleepIntraday" for p in intraday)
    # Same instant, three different fields — the merge case.
    assert len({p["time"] for p in intraday}) == 1


# ==================================================================
# Intraday series
# ==================================================================
def test_heart_rate_pairs_are_epoch_ms_and_value():
    hr = {"heartRateValues": [[WAKE_MS, 62], [WAKE_MS + 60000, 65]]}
    points = T.intraday_hr_points(hr, "2026-07-01", DEV)
    assert len(points) == 2
    assert points[0]["measurement"] == "HeartRateIntraday"
    assert points[0]["fields"]["HeartRate"] == 62
    assert _tags_ok(points[0])


def test_null_heart_rate_samples_are_dropped():
    hr = {"heartRateValues": [[WAKE_MS, None], [WAKE_MS + 60000, 65]]}
    assert len(T.intraday_hr_points(hr, "2026-07-01", DEV)) == 1


@pytest.mark.parametrize("payload", [{}, {"heartRateValues": None}])
def test_empty_heart_rate_payloads_yield_nothing(payload):
    assert T.intraday_hr_points(payload, "2026-07-01", DEV) == []


def test_zero_step_buckets_are_kept():
    """A zero-step hour is real data — the user was still. Dropping falsy
    values would erase every sedentary hour from the record."""
    steps = [{"startGMT": _gmt(1), "steps": 0}, {"startGMT": _gmt(2), "steps": 250}]
    points = T.intraday_steps_points(steps, "2026-07-01", DEV)
    assert [p["fields"]["StepsCount"] for p in points] == [0, 250]


def test_missing_step_values_are_dropped():
    steps = [{"startGMT": _gmt(1), "steps": None}]
    assert T.intraday_steps_points(steps, "2026-07-01", DEV) == []


def test_steps_use_a_naive_gmt_string():
    points = T.intraday_steps_points([{"startGMT": _gmt(6), "steps": 10}], "2026-07-01", DEV)
    assert points[0]["time"].startswith("2026-07-01T06:00:00")
    assert points[0]["time"].endswith("+00:00"), "must be tz-aware UTC"


def test_stress_and_body_battery_come_from_one_payload():
    """They arrive together; fetching twice doubled the request count against a
    rate-limited endpoint for identical data."""
    payload = {
        "stressValuesArray": [[WAKE_MS, 30]],
        "bodyBatteryValuesArray": [[WAKE_MS, "CHARGED", 70]],
    }
    points = T.intraday_stress_points(payload, "2026-07-01", DEV)
    measurements = {p["measurement"] for p in points}
    assert measurements == {"StressIntraday", "BodyBatteryIntraday"}


def test_body_battery_level_is_the_third_element():
    """The array is [epoch_ms, status, level] — reading index 1 would store the
    status string as the level."""
    payload = {"bodyBatteryValuesArray": [[WAKE_MS, "CHARGED", 70]]}
    point = T.intraday_stress_points(payload, "2026-07-01", DEV)[0]
    assert point["fields"]["BodyBatteryLevel"] == 70


def test_zero_stress_and_zero_battery_are_kept():
    payload = {"stressValuesArray": [[WAKE_MS, 0]],
               "bodyBatteryValuesArray": [[WAKE_MS, "DRAINED", 0]]}
    points = T.intraday_stress_points(payload, "2026-07-01", DEV)
    assert len(points) == 2


def test_null_stress_samples_are_dropped():
    payload = {"stressValuesArray": [[WAKE_MS, None]], "bodyBatteryValuesArray": []}
    assert T.intraday_stress_points(payload, "2026-07-01", DEV) == []


def test_breathing_rate_maps_from_epoch_pairs():
    br = {"respirationValuesArray": [[WAKE_MS, 14.2]]}
    point = T.intraday_br_points(br, "2026-07-01", DEV)[0]
    assert point["measurement"] == "BreathingRateIntraday"
    assert point["fields"]["BreathingRate"] == 14.2


def test_hrv_uses_a_naive_gmt_reading_time():
    hrv = {"hrvReadings": [{"readingTimeGMT": _gmt(4), "hrvValue": 58}]}
    point = T.intraday_hrv_points(hrv, "2026-07-01", DEV)[0]
    assert point["measurement"] == "HRV_Intraday"
    assert point["time"].startswith("2026-07-01T04:00:00")
    assert point["fields"]["hrvValue"] == 58


@pytest.mark.parametrize("fn", [
    T.intraday_hr_points, T.intraday_stress_points,
    T.intraday_br_points, T.intraday_hrv_points,
])
def test_intraday_transforms_handle_an_empty_payload(fn):
    """An endpoint returning {} (no data that day) is routine and must yield no
    points. A None payload is NOT handled — see the daily-stats note above."""
    assert fn({}, "2026-07-01", DEV) == []


def test_all_transforms_tag_with_the_configured_device_name():
    """The device tag is part of every primary key; a wrong or missing one
    silently forks the series into a second device's history."""
    assert T.intraday_hr_points({"heartRateValues": [[WAKE_MS, 60]]}, "2026-07-01", "watch-A")[0]["tags"]["Device"] == "watch-A"
    assert T.daily_stats_points(_stats(), "2026-07-01", "watch-B")[0]["tags"]["Device"] == "watch-B"


# ==================================================================
# HTTP 500 classification
# ==================================================================
class _Resp:
    def __init__(self, status_code):
        self.status_code = status_code


class _HttpErr(Exception):
    def __init__(self, status_code):
        super().__init__("http error")
        self.response = _Resp(status_code)


class _GarthErr(Exception):
    def __init__(self, status_code=None, response_status=None):
        super().__init__("garth error")
        if status_code is not None:
            self.status_code = status_code
        if response_status is not None:
            self.response = _Resp(response_status)


def test_requests_style_500_is_detected():
    assert T.is_http_500(_HttpErr(500)) is True


def test_garth_style_500_on_the_exception_itself():
    assert T.is_http_500(_GarthErr(status_code=500)) is True


def test_garth_style_500_on_a_wrapped_response():
    assert T.is_http_500(_GarthErr(response_status=500)) is True


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 502, 503])
def test_other_statuses_are_not_500(status):
    """500s are retried; everything else skips the date immediately.
    Misclassifying stalls the backfill or silently drops a day."""
    assert T.is_http_500(_HttpErr(status)) is False
    assert T.is_http_500(_GarthErr(status_code=status)) is False


def test_an_error_with_no_status_information_is_not_500():
    assert T.is_http_500(Exception("something went wrong")) is False


def test_a_none_response_does_not_crash_the_check():
    err = _GarthErr()
    err.response = None
    assert T.is_http_500(err) is False


# ==================================================================
# Body composition
# ==================================================================
def _weigh_ins(**over):
    metric = {"weight": 78000, "bmi": 24.1, "bodyFat": 19.0, "bodyWater": 55.0,
              "boneMass": 3200, "muscleMass": 58000, "physiqueRating": 5,
              "visceralFat": 7, "metabolicAge": 34, "timestampGMT": WAKE_MS,
              "sourceType": "INDEX_SCALE"}
    metric.update(over)
    return {"dailyWeightSummaries": [{"allWeightMetrics": [metric]}]}


def test_metabolic_age_in_milliseconds_is_converted_to_years():
    """Garmin returns metabolicAge as a millisecond DURATION (~7.26e11 ms ≈ 23
    years). Stored raw it would read as an age of 726 billion."""
    payload = _weigh_ins(metabolicAge=726_000_000_000)
    fields = T.body_composition_points(payload, "2026-07-01", DEV)[0]["fields"]
    assert fields["metabolicAge"] == pytest.approx(23.0, abs=0.1)


def test_metabolic_age_already_in_years_passes_through():
    """The >1000 guard: no metabolic age exceeds a millennium, so a small
    number is already in years and must not be divided again."""
    fields = T.body_composition_points(_weigh_ins(metabolicAge=34), "2026-07-01", DEV)[0]["fields"]
    assert fields["metabolicAge"] == 34


@pytest.mark.parametrize("value,expected", [(999, 999), (1001, pytest.approx(0.0, abs=0.1))])
def test_metabolic_age_guard_boundary(value, expected):
    fields = T.body_composition_points(_weigh_ins(metabolicAge=value),
                                       "2026-07-01", DEV)[0]["fields"]
    assert fields["metabolicAge"] == expected


def test_body_composition_falls_back_to_midnight_without_a_timestamp():
    """Some scale uploads carry no timestampGMT; the reading still belongs to
    the requested day rather than being dropped."""
    point = T.body_composition_points(_weigh_ins(timestampGMT=None), "2026-07-01", DEV)[0]
    assert point["time"] == "2026-07-01T00:00:00+00:00"


def test_body_composition_tags_carry_the_source_type():
    """A manual weigh-in and a smart-scale reading are distinguishable."""
    point = T.body_composition_points(_weigh_ins(), "2026-07-01", DEV)[0]
    assert point["tags"]["SourceType"] == "INDEX_SCALE"
    assert point["tags"]["Frequency"] == "Intraday"


def test_an_all_null_weigh_in_is_dropped():
    """An empty reading would otherwise write a row of nulls that looks like a
    real weigh-in to every downstream trend."""
    empty = {k: None for k in ("weight", "bmi", "bodyFat", "bodyWater", "boneMass",
                               "muscleMass", "physiqueRating", "visceralFat",
                               "metabolicAge")}
    empty["timestampGMT"] = WAKE_MS
    payload = {"dailyWeightSummaries": [{"allWeightMetrics": [empty]}]}
    assert T.body_composition_points(payload, "2026-07-01", DEV) == []


def test_no_weigh_ins_yields_nothing():
    assert T.body_composition_points({"dailyWeightSummaries": []}, "2026-07-01", DEV) == []


# ==================================================================
# Training status — heat / altitude acclimation
# ==================================================================
def _training_status(top_acclim=None, device_acclim=None, **over):
    data = {"timestamp": WAKE_MS, "trainingStatus": "MAINTAINING",
            "trainingStatusFeedbackPhrase": "ok", "weeklyTrainingLoad": 700,
            "fitnessTrend": "STABLE",
            "acuteTrainingLoadDTO": {"acwrPercent": 105, "dailyTrainingLoadAcute": 320,
                                     "dailyTrainingLoadChronic": 300,
                                     "maxTrainingLoadChronic": 400,
                                     "minTrainingLoadChronic": 200,
                                     "dailyAcuteChronicWorkloadRatio": 1.06}}
    data.update(over)
    if device_acclim is not None:
        data["heatAltitudeAcclimation"] = device_acclim
    payload = {"mostRecentTrainingStatus": {"latestTrainingStatusData": {"dev1": data}}}
    if top_acclim is not None:
        payload["heatAltitudeAcclimationDTO"] = top_acclim
    return payload


def test_acclimation_is_read_from_the_top_level_of_the_payload():
    """Verified against live responses: Garmin returns it at the top level, not
    nested per device."""
    payload = _training_status(top_acclim={"heatAcclimationPercentage": 55,
                                           "altitudeAcclimationPercentage": 12,
                                           "heatTrend": "STABLE",
                                           "altitudeTrend": "STABLE",
                                           "currentAltitude": 35})
    fields = T.training_status_points(payload, "2026-07-01", DEV)[0]["fields"]
    assert fields["heatAcclimationPercentage"] == 55
    assert fields["altitudeAcclimationPercentage"] == 12
    assert fields["currentAltitude"] == 35


def test_acclimation_falls_back_to_the_per_device_dict():
    """Kept in case Garmin moves it back."""
    payload = _training_status(device_acclim={"heatAcclimationPercentage": 41})
    fields = T.training_status_points(payload, "2026-07-01", DEV)[0]["fields"]
    assert fields["heatAcclimationPercentage"] == 41


def test_top_level_acclimation_wins_over_the_per_device_copy():
    payload = _training_status(top_acclim={"heatAcclimationPercentage": 55},
                               device_acclim={"heatAcclimationPercentage": 41})
    fields = T.training_status_points(payload, "2026-07-01", DEV)[0]["fields"]
    assert fields["heatAcclimationPercentage"] == 55


def test_acwr_is_dug_out_of_the_acute_load_dto():
    fields = T.training_status_points(_training_status(), "2026-07-01", DEV)[0]["fields"]
    assert fields["acwrPercent"] == 105
    assert fields["dailyTrainingLoadAcute"] == 320


def test_training_status_without_an_acute_load_dto_is_all_none():
    payload = _training_status(acuteTrainingLoadDTO=None)
    fields = T.training_status_points(payload, "2026-07-01", DEV)[0]["fields"]
    assert fields["acwrPercent"] is None


def test_training_status_needs_a_timestamp():
    """No timestamp means no point can be keyed."""
    assert T.training_status_points(_training_status(timestamp=None), "2026-07-01", DEV) == []


def test_training_status_empty_payload():
    assert T.training_status_points({}, "2026-07-01", DEV) == []


# ==================================================================
# Remaining slow-moving markers
# ==================================================================
def test_vo2_max_reads_running_and_cycling():
    payload = [{"generic": {"vo2MaxPreciseValue": 48.2, "calendarDate": "2026-07-01"},
                "cycling": {"vo2MaxPreciseValue": 43.0}}]
    fields = T.vo2_max_points(payload, "2026-07-01", DEV)[0]["fields"]
    assert fields["VO2_max_value"] == 48.2
    assert fields["VO2_max_value_cycling"] == 43.0


def test_vo2_max_without_cycling_data():
    payload = [{"generic": {"vo2MaxPreciseValue": 48.2, "calendarDate": "2026-07-01"},
                "cycling": None}]
    fields = T.vo2_max_points(payload, "2026-07-01", DEV)[0]["fields"]
    assert fields["VO2_max_value_cycling"] is None


def test_fitness_age_maps_its_fields():
    payload = {"chronologicalAge": 40, "fitnessAge": 35, "achievableFitnessAge": 33,
               "previousFitnessAge": 36, "lastUpdated": "2026-07-01T00:00:00.0"}
    fields = T.fitness_age_points(payload, "2026-07-01", DEV)[0]["fields"]
    assert fields["fitnessAge"] == 35
    assert fields["chronologicalAge"] == 40


def test_race_predictions_map_all_four_distances():
    payload = [{"calendarDate": "2026-07-01", "time5K": 1500, "time10K": 3100,
                "timeHalfMarathon": 6900, "timeMarathon": 14400}]
    fields = T.race_prediction_points(payload, "2026-07-01", DEV)[0]["fields"]
    assert (fields["time5K"], fields["timeMarathon"]) == (1500, 14400)


def test_hill_score_maps_its_components():
    """The endpoint returns the score dict directly, not wrapped in a list."""
    payload = {"strengthScore": 55, "enduranceScore": 52, "overallScore": 56,
               "hillScoreClassificationId": 1, "hillScoreFeedbackPhraseId": 2,
               "vo2MaxPreciseValue": 48.0}
    point = T.hill_score_points(payload, "2026-07-01", DEV)[0]
    assert point["fields"]["strengthScore"] == 55
    assert point["fields"]["overallScore"] == 56
    # Daily markers are keyed at GMT midnight of the requested day.
    assert point["time"] == "2026-07-01T00:00:00+00:00"


def test_an_all_null_hill_score_is_dropped():
    empty = {k: None for k in ("strengthScore", "enduranceScore", "overallScore",
                               "hillScoreClassificationId", "hillScoreFeedbackPhraseId",
                               "vo2MaxPreciseValue")}
    assert T.hill_score_points(empty, "2026-07-01", DEV) == []


@pytest.mark.parametrize("fn,payload", [
    (T.vo2_max_points, []),
    (T.race_prediction_points, []),
    (T.hill_score_points, {}),
    (T.endurance_score_points, {}),
    (T.blood_pressure_points, {"measurementSummaries": []}),
])
def test_marker_transforms_on_empty_payloads(fn, payload):
    assert fn(payload, "2026-07-01", DEV) == []


# ==================================================================
# Menstrual cycle
# ==================================================================
def test_menstrual_data_parses_the_post_may_2026_shape():
    """Garmin renamed the wrapper keys; the old parser silently skipped every
    date for ~8 weeks, so both shapes are accepted."""
    payload = {"summary": {"cycleLength": 28, "periodLength": 5,
                           "currentDayOfCycle": 5, "currentCyclePhase": "MENSTRUAL"},
               "dayData": {"menstrualFlow": "MEDIUM"}}
    points = T.menstrual_points(payload, "2026-07-01", DEV)
    assert points, "the current payload shape must parse"
    point = points[0]
    assert point["measurement"] == "MenstrualCycle"
    assert point["fields"]["currentCyclePhase"] == "MENSTRUAL"
    assert point["fields"]["currentDayOfCycle"] == 5
    assert point["fields"]["menstrualFlow"] == "MEDIUM"
    # Cycle rows are keyed at noon UTC, like daily stats.
    assert point["time"] == "2026-07-01T12:00:00+00:00"


def test_menstrual_data_parses_the_legacy_shape():
    payload = {"daySummary": {"cycleLength": 28, "periodLength": 5,
                              "currentDayOfCycle": 5},
               "dayLog": {"cyclePhase": "MENSTRUAL"}}
    points = T.menstrual_points(payload, "2026-07-01", DEV)
    assert points, "the pre-rename shape must still parse"
    assert points[0]["fields"]["currentCyclePhase"] == "MENSTRUAL"


def test_a_payload_with_no_cycle_fields_yields_nothing():
    """Non-tracking users get empty or all-None values; that must stay quiet."""
    assert T.menstrual_points({"summary": {}, "dayData": {}}, "2026-07-01", DEV) == []


def test_menstrual_data_ignores_a_non_dict_payload():
    assert T.menstrual_points([], "2026-07-01", DEV) == []
    assert T.menstrual_points(None, "2026-07-01", DEV) == []


def test_menstrual_empty_payload_yields_nothing():
    assert T.menstrual_points({}, "2026-07-01", DEV) == []
