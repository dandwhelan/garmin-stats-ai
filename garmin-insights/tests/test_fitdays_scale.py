"""Fitdays/Lefu full-BIA protocol: codec, decode and write planning.

Every frame below is a real capture from the JEETIF2421 unit (see
``docs/scale-protocol/``), so these pin the protocol, not a guess at it.
"""

from __future__ import annotations

import json

import pytest

from garmin_insights.scales import registry
from garmin_insights.scales.fitdays import (
    FitdaysAdapter,
    ScaleProfile,
    build_c0,
    build_c1,
    checksum,
    make_frame,
    plan_writes,
    split_frame,
)

H = bytes.fromhex
HELLO = H("07001d00aab3711e1a0025010a00010000000000000000000000540000000340002e")
LIVE_72_4 = H("68000700a20100011ad0000e")       # status 01 live, 72.400 kg
SETTLED_72_1 = H("65000700a203000119a40003")    # status 03 settled, 72.100 kg
FRESH_RESULT = H("52002300a76aa9a5d3250119a4000a011b0bf40c520ab90af500da0a5e0ad10959098e124de8bf36")
DAN = ScaleProfile(height_cm=185, sex="male", name="Dan")


def test_checksum_matches_captured_frames():
    for frame in (HELLO, LIVE_72_4, SETTLED_72_1, FRESH_RESULT,
                  H("04000600b6000003400039"), H("0a000300b03a002a")):
        assert frame[-1] == checksum(frame[4], frame[5:-1])


def test_split_frame_rejects_bad_checksum_and_length():
    assert split_frame(LIVE_72_4) is not None
    assert split_frame(LIVE_72_4[:-1] + bytes([LIVE_72_4[-1] ^ 1])) is None
    assert split_frame(LIVE_72_4[:-2]) is None
    assert split_frame(b"") is None and split_frame("nope") is None


def test_profile_frames_reproduce_the_app_byte_for_byte():
    weight = (0x10000 + 0x16A6) / 1000  # the C0 weight field the app sent
    assert build_c0(1, DAN, weight, 0x6AA9A5CD, 60).hex() == (
        "01001b00c06aa9a5cd003c01b91c16a61c251d6a0f124de8bf01010344616e28")
    assert build_c1(2, DAN, weight).hex() == (
        "02001600c10101b91c16a61c251d6a0f124de8bf01010344616e29")
    assert make_frame(4, 0xB6, H("0000034000")).hex() == "04000600b6000003400039"


def test_live_weight_is_unstable():
    reading = FitdaysAdapter().decode([HELLO, LIVE_72_4])
    assert reading.weight_kg == pytest.approx(72.4) and not reading.stable


def test_result_replayed_before_settling_is_ignored():
    # The scale replays its last stored result on connect; it must not count.
    reading = FitdaysAdapter().decode([HELLO, FRESH_RESULT, LIVE_72_4])
    assert not reading.stable


def test_result_after_settling_is_final_with_segments():
    reading = FitdaysAdapter().decode([HELLO, LIVE_72_4, SETTLED_72_1, FRESH_RESULT])
    assert reading.stable and reading.weight_kg == pytest.approx(72.1)
    segs = reading.extras["impedance_segments_ohm"]
    assert segs["trunk"] == {"f1": 28.3, "f2": 21.8}
    assert segs["left_arm"]["f1"] < segs["right_arm"]["f1"]
    json.dumps(reading.extras)


def test_all_zero_result_is_final_weight_only():
    zero = bytearray(FRESH_RESULT)
    zero[15:35] = bytes(20)
    zero[-1] = checksum(zero[4], bytes(zero[5:-1]))
    reading = FitdaysAdapter().decode([SETTLED_72_1, bytes(zero)])
    assert reading.stable and reading.extras.get("no_impedance") is True
    assert "impedance_raw" not in reading.extras


def test_planner_waits_until_someone_stands_on_the_scale():
    state: dict = {}
    assert plan_writes([HELLO], state, DAN, now=0x6AA9A5CD) == []
    assert state["phase"] == "waiting"


def test_planner_arms_once_then_finalises_once():
    state: dict = {}
    frames = [HELLO, LIVE_72_4]
    armed = plan_writes(frames, state, DAN, now=0x6AA9A5CD)
    assert [f[4] for f in armed] == [0xB0, 0xC0, 0xC1, 0xC0, 0xB6, 0xC0, 0xC1, 0xC0, 0xB0, 0xB0]
    assert [f[0] for f in armed] == list(range(10))
    assert armed[4] == make_frame(4, 0xB6, H("0000034000"))  # token echoed from the hello
    assert all(split_frame(f) for f in armed)
    assert plan_writes(frames, state, DAN) == []  # polling again re-sends nothing

    frames += [SETTLED_72_1, FRESH_RESULT]
    final = plan_writes(frames, state, DAN, now=0x6AA9A5CD)
    assert [f[4] for f in final] == [0xB0, 0xC0, 0xC1] and final[0][5] == 0x3A
    assert [f[0] for f in final] == [10, 11, 12]
    assert state["phase"] == "done" and plan_writes(frames, state, DAN) == []


def test_planner_without_profile_sends_nothing():
    assert plan_writes([HELLO, LIVE_72_4], {}, None) == []


def test_fitdays_is_the_default_adapter_and_json_safe():
    assert next(iter(registry.ADAPTERS)) == "fitdays"
    d = registry.get_adapter("fitdays").descriptor.to_dict()
    assert d["server_writes"] is True and d["indicate_uuid"].startswith("0000ffb3")
    json.dumps(d)


def test_wla37_reproduces_the_paired_app_weigh_in():
    from garmin_insights.scales.wla37 import compute_wla37, engine_status

    if not engine_status()["available"]:
        pytest.skip("vendor WLA37 library not installed (it is never committed)")
    block = H("00de0bc80c5b0ada0b2a00c30a4a0ade097b09c8")
    raw = [int.from_bytes(block[i:i + 2], "big") for i in range(0, 20, 2)]
    out = compute_wla37(72.0, 185, 38, "male", raw)
    m, x = out["metrics"], out["extras"]
    # Figures the Fitdays app displayed for this weigh-in.
    assert (m["body_fat_pct"], m["body_water_pct"], m["visceral_fat"], m["metabolic_age"]) == (9.4, 66.5, 1.0, 35)
    assert (x["bmr_kcal"], x["protein_pct"], x["subcutaneous_fat_pct"], x["skeletal_muscle_pct"]) == (1780, 18.1, 6.8, 51.8)
    assert round(m["muscle_mass_kg"] * 2.20462, 1) == 134.3
    assert round(x["segments"]["left_arm"]["fat_pct"], 1) == 15.9
    assert round(x["segments"]["right_arm"]["fat_pct"], 1) == 19.4
    # Shown in the UI; deliberately NOT uploaded as Garmin's physique_rating,
    # which is a different 1-9 scale (this enum was observed at 0-8).
    assert isinstance(x["body_type"], int) and 0 <= x["body_type"] <= 12
    assert 0 <= x["body_score"] <= 100
    # body_type is for display only. It tracks body fat, while Garmin's
    # physique_rating means 9 = very muscular, so it is never uploaded as one.
    assert "physique_rating" not in m
