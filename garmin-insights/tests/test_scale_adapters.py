"""Tests for the server-side Bluetooth scale decoders + BIA maths.

The composition golden vectors below were produced by executing the exact
``computeBodyComposition()`` source from ``web/static/app.js`` (lines
5059-5120) under node, so they are proof that moving the maths out of the
browser preserved behaviour bit-for-bit.
"""

from __future__ import annotations

import json

import pytest

from garmin_insights.scales import registry
from garmin_insights.scales.base import AdapterDescriptor, parse_hex_frames
from garmin_insights.scales.composition import compute_body_composition
from garmin_insights.scales.lefu import LefuAdapter

TOL = 1e-6


# --------------------------------------------------------------------------
# frame builders
# --------------------------------------------------------------------------
def _ac02(weight_kg: float, status: int, d2: int = 0x00, d3: int = 0x00,
          cksum: int | None = None) -> bytes:
    """Build an ``AC 02 | D0 D1 D2 D3 | STATUS | CKSUM`` frame."""
    raw = int(round(weight_kg * 10))
    d0, d1 = raw >> 8, raw & 0xFF
    if cksum is None:
        cksum = (d0 + d1 + d2 + d3 + status) & 0xFF
    return bytes([0xAC, 0x02, d0, d1, d2, d3, status, cksum])


def _offset_frame(weight_kg: float) -> bytes:
    """E.volve/ESPHome layout: u24 at bytes 3..5, biased by 0x680000."""
    raw = 0x680000 + int(round(weight_kg * 1000))
    return bytes([0x00, 0x00, 0x00]) + raw.to_bytes(3, "big")


def _a2_frame(weight_kg: float, status: int, seq: int = 0x00, flag: int = 0x01) -> bytes:
    """``SEQ 00 07 00 A2 STATUS 00 FLAG W_HI W_LO 00 CKSUM`` layout.

    Checksum is not validated by the decoder, so it is left as a filler
    byte here rather than computed.
    """
    raw = int(round(weight_kg * 100))
    w_hi, w_lo = (raw >> 8) & 0xFF, raw & 0xFF
    return bytes([seq, 0x00, 0x07, 0x00, 0xA2, status, 0x00, flag, w_hi, w_lo, 0x00, 0x00])


def _impedance_frame(hi: int, lo: int, offset: int = 20) -> bytes:
    """40-byte 0xFF frame, all-0xFF filler with two bytes planted.

    Filler is 0xFF so every neighbouring u16 reads as 65535 and cannot be
    mistaken for an impedance candidate — the planted pair is the only
    thing the scanner can find.
    """
    frame = bytearray(b"\xff" * 40)
    frame[offset] = hi
    frame[offset + 1] = lo
    return bytes(frame)


@pytest.fixture()
def adapter() -> LefuAdapter:
    return LefuAdapter()


# --------------------------------------------------------------------------
# variant A — ac02
# --------------------------------------------------------------------------
def test_variant_a_stable_beats_live(adapter):
    frames = [
        _ac02(81.9, 0xCE),
        _ac02(82.3, 0xCE),
        _ac02(82.6, 0xCA),  # settled
        _ac02(82.5, 0xCE),  # someone stepping off again
    ]
    reading = adapter.decode(frames)
    assert reading is not None
    assert reading.stable is True
    assert reading.weight_kg == pytest.approx(82.6)
    assert reading.variant == "lefu_ac02"
    assert reading.impedance_ohm is None


def test_variant_a_live_only_is_unstable(adapter):
    reading = adapter.decode([_ac02(70.1, 0xCE)])
    assert reading is not None
    assert reading.stable is False
    assert reading.weight_kg == pytest.approx(70.1)


def test_variant_a_bad_checksum_rejected(adapter):
    good = _ac02(82.6, 0xCA)
    bad = _ac02(99.9, 0xCA, cksum=(good[7] + 1) & 0xFF)
    # bad frame alone -> nothing decodable
    assert adapter.decode([bad]) is None
    # and it must not out-vote the good one
    reading = adapter.decode([good, bad])
    assert reading is not None
    assert reading.weight_kg == pytest.approx(82.6)


def test_variant_a_unknown_status_ignored(adapter):
    assert adapter.decode([_ac02(82.6, 0x11)]) is None


# --------------------------------------------------------------------------
# variant B — offset
# --------------------------------------------------------------------------
def test_variant_b_offset_decode(adapter):
    reading = adapter.decode([_offset_frame(75.5)])
    assert reading is not None
    assert reading.weight_kg == pytest.approx(75.5)
    assert reading.variant == "lefu_offset"
    assert reading.stable is False


def test_variant_b_out_of_range_rejected(adapter):
    # u24 far below the 0x680000 bias -> negative kg
    assert adapter.decode([bytes([0x00, 0x00, 0x00, 0x00, 0x00, 0x01])]) is None
    # and far above -> >300 kg
    assert adapter.decode([bytes([0x00, 0x00, 0x00, 0xFF, 0xFF, 0xFF])]) is None


def test_variant_b_stable_after_three_identical(adapter):
    two = adapter.decode([_offset_frame(75.5), _offset_frame(75.5)])
    assert two is not None and two.stable is False
    three = adapter.decode([_offset_frame(75.5)] * 3)
    assert three is not None
    assert three.stable is True
    assert three.weight_kg == pytest.approx(75.5)
    assert three.variant == "lefu_offset"


def test_variant_b_run_resets_on_change(adapter):
    frames = [_offset_frame(75.5), _offset_frame(75.6), _offset_frame(75.5)]
    reading = adapter.decode(frames)
    assert reading is not None
    assert reading.stable is False


# --------------------------------------------------------------------------
# variant C — a2 (real JEETIFxxxx capture)
# --------------------------------------------------------------------------
def test_variant_a2_live_frame(adapter):
    reading = adapter.decode([_a2_frame(103.0, 0x01)])
    assert reading is not None
    assert reading.stable is False
    assert reading.weight_kg == pytest.approx(103.0, abs=TOL)
    assert reading.variant == "lefu_a2"


def test_variant_a2_stable_frame(adapter):
    reading = adapter.decode([_a2_frame(71.14, 0x03)])
    assert reading is not None
    assert reading.stable is True
    assert reading.weight_kg == pytest.approx(71.14, abs=TOL)


def test_variant_a2_unknown_status_ignored(adapter):
    assert adapter.decode([_a2_frame(71.14, 0x02)]) is None


def test_variant_a2_stable_survives_later_reversion(adapter):
    """Once stable, a later live frame (of any weight) must not erase it.

    Mirrors the ac02 semantics: the caller stops scanning the instant a
    stable reading comes back, so a reversion after the fact is moot.
    """
    frames = [
        _a2_frame(71.14, 0x01),
        _a2_frame(71.14, 0x03),
        _a2_frame(165.5, 0x01),  # a later, unrelated live blip
    ]
    reading = adapter.decode(frames)
    assert reading is not None
    assert reading.stable is True
    assert reading.weight_kg == pytest.approx(71.14, abs=TOL)


def test_variant_a2_real_capture_regression(adapter):
    """Verbatim (trimmed) frames from a real Fitdays-paired JEETIF2421 scan.

    The full session climbs from noise (108.5, 103.0 kg as a foot lands),
    converges on 71.14 kg, briefly reports STATUS=0x03 (stable), reverts to
    a couple of unrelated live spikes, then settles on the same 71.14 kg
    for good. This is a regression lock for the lefu_a2 layout discovered
    against physical hardware, not a synthetic construction.
    """
    hex_frames = [
        "47000700a20100002a62000f",  # live, 108.50 kg (noise)
        "48000700a2010000283c0007",  # live, 103.00 kg (noise)
        "9c000700a20100011bca0009",  # live, converged to 71.14 kg
        "a2000700a20300011bca000b",  # stable, 71.14 kg
        "a3000700a20300011bca000b",  # stable, 71.14 kg
        "a7000700a201000040a60009",  # live again, 165.50 kg (unrelated blip)
        "a8000700a2010000a1220006",  # live again, 412.50 kg (unrelated blip)
        "ea000700a20300011bca000b",  # stable, 71.14 kg (final, sustained)
        "66000700a20300011bca000b",  # stable, 71.14 kg (final, sustained)
    ]
    reading = adapter.decode(parse_hex_frames(hex_frames))
    assert reading is not None
    assert reading.stable is True
    assert reading.variant == "lefu_a2"
    assert reading.weight_kg == pytest.approx(71.14, abs=TOL)
    assert reading.impedance_ohm is None  # no impedance frame ever appeared


def test_ac02_family_never_falls_through_to_offset(adapter):
    """A corrupt 8-byte AC02 frame is dropped, not re-read as an offset frame."""
    frame = bytearray(_ac02(82.6, 0xCA))
    frame[3:6] = (0x680000 + 75500).to_bytes(3, "big")  # a valid offset payload
    frame[7] = (frame[7] + 1) & 0xFF  # ...but break the checksum
    assert adapter.decode([bytes(frame)]) is None


# --------------------------------------------------------------------------
# robustness
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "frames",
    [
        [],
        [b""],
        [b"\x00"],
        [b"\xac"],
        [b"\xac\x02\x03"],                       # truncated ac02
        [b"\xde\xad\xbe\xef"],                   # garbage
        [b"\x01" * 64],                          # unknown length
        [None, 123, "not bytes"],                # wrong types entirely
    ],
)
def test_decode_never_raises_and_returns_none(adapter, frames):
    assert adapter.decode(frames) is None


# --------------------------------------------------------------------------
# impedance gate
# --------------------------------------------------------------------------
def test_impedance_implausible_value_not_accepted(adapter):
    # planted u16 reads 10000 (BE) / 4135 (LE) — both outside 150..1200
    frames = [_ac02(82.6, 0xCA), _impedance_frame(0x27, 0x10)]
    reading = adapter.decode(frames)
    assert reading is not None
    assert reading.weight_kg == pytest.approx(82.6)
    assert reading.impedance_ohm is None
    # near-miss candidates are still recorded for diagnosing the unit
    assert reading.extras["impedance_candidates"]
    assert all(
        not (150 <= c["value"] <= 1200) for c in reading.extras["impedance_candidates"]
    )


def test_impedance_plausible_value_accepted(adapter):
    # planted little-endian 0x01F4 = 500 ohm
    frames = [_ac02(82.6, 0xCA), _impedance_frame(0xF4, 0x01)]
    reading = adapter.decode(frames)
    assert reading is not None
    assert reading.impedance_ohm == pytest.approx(500.0)
    candidates = reading.extras["impedance_candidates"]
    assert {"offset": 20, "endian": "le", "value": 500} in candidates
    for c in candidates:
        assert set(c) == {"offset", "endian", "value"}
        assert c["endian"] in ("be", "le")


def test_impedance_frame_alone_yields_no_reading(adapter):
    """Impedance without a weight is not a measurement."""
    assert adapter.decode([_impedance_frame(0xF4, 0x01)]) is None


def test_wrong_length_impedance_frame_ignored(adapter):
    frames = [_ac02(82.6, 0xCA), b"\xff" * 39]
    reading = adapter.decode(frames)
    assert reading is not None
    assert reading.impedance_ohm is None
    assert "impedance_candidates" not in reading.extras


# --------------------------------------------------------------------------
# parse_hex_frames
# --------------------------------------------------------------------------
def test_parse_hex_frames_tolerates_junk():
    frames = parse_hex_frames(
        [
            "ac02033a0000ca07",
            "0xAC02",
            "ac 02 03 3a",
            "ac:02-03",
            "",
            "zzzz",       # not hex
            "abc",        # odd length
            None,         # not a string
            12345,        # not a string
            b"\x01\x02",  # already bytes
        ]
    )
    assert frames == [
        bytes.fromhex("ac02033a0000ca07"),
        b"\xac\x02",
        bytes.fromhex("ac02033a"),
        b"\xac\x02\x03",
        b"\x01\x02",
    ]


def test_parse_hex_frames_empty():
    assert parse_hex_frames([]) == []
    assert parse_hex_frames(None) == []


def test_parse_then_decode_round_trip(adapter):
    hex_frames = [_ac02(82.6, 0xCA).hex(), "junk", _ac02(82.5, 0xCE).hex()]
    reading = adapter.decode(parse_hex_frames(hex_frames))
    assert reading is not None
    assert reading.stable is True
    assert reading.weight_kg == pytest.approx(82.6)


# --------------------------------------------------------------------------
# composition — golden vectors from the JS source at app.js:5059-5120
# --------------------------------------------------------------------------
def test_composition_golden_male():
    out = compute_body_composition(80.0, 500.0, 180.0, 35.0, "male")
    assert out["bmi"] == pytest.approx(24.691358024691358, abs=TOL)
    assert out["body_fat_pct"] == pytest.approx(23.653849999999977, abs=TOL)
    assert out["body_water_pct"] == pytest.approx(52.37345890000001, abs=TOL)
    assert out["muscle_mass_kg"] == pytest.approx(57.96547740640001, abs=TOL)
    assert out["bone_mass_kg"] == pytest.approx(3.1114425936000005, abs=TOL)
    assert out["visceral_fat"] == pytest.approx(14.110000000000003, abs=TOL)
    assert out["metabolic_age"] == pytest.approx(33.53069999999999, abs=TOL)


def test_composition_golden_female():
    out = compute_body_composition(65.0, 500.0, 165.0, 30.0, "female")
    assert out["bmi"] == pytest.approx(23.875114784205696, abs=TOL)
    assert out["body_fat_pct"] == pytest.approx(31.211204384615378, abs=TOL)
    assert out["body_water_pct"] == pytest.approx(49.11520006938462, abs=TOL)
    assert out["muscle_mass_kg"] == pytest.approx(42.142184474100006, abs=TOL)
    assert out["bone_mass_kg"] == pytest.approx(2.5705326759, abs=TOL)
    assert out["visceral_fat"] == pytest.approx(1.0, abs=TOL)
    assert out["metabolic_age"] == pytest.approx(36.22330000000001, abs=TOL)


def test_composition_case_insensitive_sex():
    a = compute_body_composition(80.0, 500.0, 180.0, 35.0, "male")
    b = compute_body_composition(80.0, 500.0, 180.0, 35.0, "  MaLe ")
    assert a == b


def test_composition_extras_are_derived_not_invented():
    out = compute_body_composition(80.0, 500.0, 180.0, 35.0, "male")
    extras = out["extras"]
    fat_mass = 80.0 * out["body_fat_pct"] / 100.0
    assert extras["fat_mass_kg"] == pytest.approx(fat_mass, abs=TOL)
    assert extras["fat_free_mass_kg"] == pytest.approx(80.0 - fat_mass, abs=TOL)
    # Katch-McArdle: 370 + 21.6 * FFM
    assert extras["bmr_kcal"] == pytest.approx(370 + 21.6 * (80.0 - fat_mass), abs=TOL)
    # no fabricated segmental figures
    assert not any("arm" in k or "leg" in k or "trunk" in k for k in extras)


def test_composition_without_impedance_is_bmi_only():
    out = compute_body_composition(80.0, None, 180.0, 35.0, "male")
    assert out == {"bmi": pytest.approx(24.691358024691358, abs=TOL)}


@pytest.mark.parametrize("sex", ["", "unknown", "other", None, 7])
def test_composition_unknown_sex_is_bmi_only(sex):
    out = compute_body_composition(80.0, 500.0, 180.0, 35.0, sex)
    assert list(out) == ["bmi"]


@pytest.mark.parametrize("age", [None, -5, 0, 500, "abc"])
def test_composition_bad_age_is_bmi_only(age):
    out = compute_body_composition(80.0, 500.0, 180.0, age, "male")
    assert list(out) == ["bmi"]


@pytest.mark.parametrize("height", [None, 0, -170, 5, 900, "tall", float("nan")])
def test_composition_bad_height_returns_empty(height):
    assert compute_body_composition(80.0, 500.0, height, 35.0, "male") == {}


@pytest.mark.parametrize("weight", [None, 0, -80, 1200, "heavy"])
def test_composition_bad_weight_returns_empty(weight):
    assert compute_body_composition(weight, 500.0, 180.0, 35.0, "male") == {}


def test_composition_clamps_extreme_impedance_out():
    # 9999 ohm is outside the accepted band -> degrade to BMI, not garbage
    assert list(compute_body_composition(80.0, 9999.0, 180.0, 35.0, "male")) == ["bmi"]


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------
def test_registry_has_lefu():
    adapter = registry.get_adapter("lefu")
    assert adapter is not None
    assert isinstance(adapter.descriptor, AdapterDescriptor)
    assert adapter.descriptor.id == "lefu"


def test_get_adapter_unknown_returns_none():
    assert registry.get_adapter("nope") is None
    assert registry.get_adapter("") is None
    assert registry.get_adapter(None) is None


def test_descriptors_are_json_serialisable():
    payload = registry.descriptors()
    round_tripped = json.loads(json.dumps(payload))
    assert round_tripped == payload
    lefu = next(d for d in round_tripped if d["id"] == "lefu")
    assert lefu["service_uuid"] == "0000ffb0-0000-1000-8000-00805f9b34fb"
    assert lefu["write_uuid"] == "0000ffb1-0000-1000-8000-00805f9b34fb"
    assert lefu["notify_uuid"] == "0000ffb2-0000-1000-8000-00805f9b34fb"
    assert len(lefu["handshake"]) == 5
    assert lefu["poll"] == {"frame": "ac02fe060000ccd0", "interval_ms": 1000}
    assert "Health Scale" in lefu["name_prefixes"]
    for frame in lefu["handshake"] + [lefu["poll"]["frame"]]:
        bytes.fromhex(frame)  # every handshake frame must be valid hex
