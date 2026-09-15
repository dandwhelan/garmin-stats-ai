"""Fitdays / Lefu 8-electrode scale (``JEETIFxxxx``) — full BIA protocol.

Reverse-engineered end to end in ``docs/scale-protocol/``. Unlike the older
``lefu`` adapter (weight-only layouts that do not match this unit), this
speaks the scale's real command protocol, so it gets the ten segmental
impedances rather than just weight.

The browser is still a dumb BLE pipe, but this protocol is conversational:
the scale only runs its impedance sweep after a timestamped user-profile
push arrives **while the user is already standing on it**, and the frames
carry a checksum, so the handshake cannot be a fixed descriptor list. The
server therefore plans the writes (:func:`plan_writes`) and hands them back
to the browser in the ``/api/scale/frames`` response.

Frame envelope (every frame, both directions)::

    [0] SEQ  [1:3] LEN (u16 BE)  [3] PART  [4] TYPE  [5:-1] payload  [-1] CK

``len(frame) == LEN + 5``; multi-byte fields are big-endian.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Sequence

from .base import AdapterDescriptor, ScaleReading

__all__ = [
    "FITDAYS_DESCRIPTOR",
    "FitdaysAdapter",
    "ScaleProfile",
    "checksum",
    "make_frame",
    "split_frame",
    "build_c0",
    "build_c1",
    "plan_writes",
    "SEGMENTS",
]

SERVICE_UUID = "0000ffb0-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000ffb1-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000ffb2-0000-1000-8000-00805f9b34fb"
INDICATE_UUID = "0000ffb3-0000-1000-8000-00805f9b34fb"

T_ACK, T_WEIGHT, T_STORED, T_RESULT, T_HELLO = 0xA0, 0xA2, 0xA5, 0xA7, 0xAA
T_CTRL, T_TRIGGER, T_PROFILE, T_PROFILE_SHORT = 0xB0, 0xB6, 0xC0, 0xC1

STATUS_SETTLED = 0x03

#: Order of the ten impedance words: slots 0-4 at frequency 1, slots 5-9 the
#: same segments at frequency 2. Arms confirmed against the vendor app; the
#: vendor engine uses the same left/right labels for the legs.
SEGMENTS = ("trunk", "left_arm", "right_arm", "left_leg", "right_leg")

#: Echoed back in the ``B6`` trigger when the scale's ``AA`` hello is missed.
_DEFAULT_TOKEN = bytes.fromhex("0000034000")
#: Profile id this unit expects (appears in every profile and result frame).
_DEFAULT_USER_ID = bytes.fromhex("124de8bf")
#: Below this the platform is not carrying a person yet — don't arm the sweep.
_ARM_MIN_KG = 20.0


def checksum(ftype: int, payload: bytes) -> int:
    """5-bit additive checksum; bit 5 is set for every type but ``A2``.

    Verified against 602 captured frames. Bit 5 is confounded with channel in
    the captures, but this rule is correct for every frame this unit sends.
    """
    low = (ftype + sum(payload)) & 0x1F
    return low if ftype == T_WEIGHT else low | 0x20


def make_frame(seq: int, ftype: int, payload: bytes) -> bytes:
    """Wrap ``payload`` in the envelope with a valid checksum."""
    length = len(payload) + 1
    return (
        bytes([seq & 0xFF]) + length.to_bytes(2, "big") + bytes([0x00, ftype & 0xFF])
        + bytes(payload) + bytes([checksum(ftype, payload)])
    )


def split_frame(frame: bytes) -> tuple[int, int, bytes] | None:
    """Validate a frame and return ``(seq, type, payload)``, else ``None``.

    Never raises: this is untrusted input relayed by a browser.
    """
    if not isinstance(frame, (bytes, bytearray)) or len(frame) < 6:
        return None
    frame = bytes(frame)
    if len(frame) != int.from_bytes(frame[1:3], "big") + 5:
        return None
    ftype, payload = frame[4], frame[5:-1]
    if frame[-1] != checksum(ftype, payload):
        return None
    return frame[0], ftype, payload


@dataclass(frozen=True)
class ScaleProfile:
    """What the scale needs to recognise the user and run the sweep."""

    height_cm: int
    sex: str = "male"
    name: str = ""
    user_id: bytes = _DEFAULT_USER_ID

    @property
    def gender_code(self) -> int:
        return 2 if str(self.sex).strip().lower() in ("female", "f", "2") else 1

    @property
    def name_bytes(self) -> bytes:
        # The frame has a fixed 3-byte name field (the scale shows it as a
        # greeting), so longer names are truncated and short ones padded.
        clean = "".join(c for c in str(self.name) if 32 <= ord(c) < 127)
        return clean.encode("ascii")[:3].ljust(3, b"\x00")


def _weight_u16(weight_kg: float) -> bytes:
    # Low 16 bits of grams; the scale assumes the 0x01 high byte (65-130 kg).
    return (int(round(weight_kg * 1000)) & 0xFFFF).to_bytes(2, "big")


def _profile_tail(p: ScaleProfile, weight_kg: float) -> bytes:
    return (
        bytes([p.gender_code, int(p.height_cm) & 0xFF, 0x1C]) + _weight_u16(weight_kg)
        + bytes.fromhex("1c251d6a0f") + p.user_id + bytes([0x01, 0x01, 0x03]) + p.name_bytes
    )


def build_c0(seq: int, p: ScaleProfile, weight_kg: float, now: float, tz_offset_min: int) -> bytes:
    """Timestamped profile push."""
    head = int(now).to_bytes(4, "big") + (tz_offset_min & 0xFFFF).to_bytes(2, "big")
    return make_frame(seq, T_PROFILE, head + _profile_tail(p, weight_kg))


def build_c1(seq: int, p: ScaleProfile, weight_kg: float) -> bytes:
    """Compact (untimestamped) profile push."""
    return make_frame(seq, T_PROFILE_SHORT, bytes([0x01]) + _profile_tail(p, weight_kg))


def _ctrl(seq: int, cmd: int) -> bytes:
    return make_frame(seq, T_CTRL, bytes([cmd, 0x00]))


# ---------------------------------------------------------------------------
# frame decoding
# ---------------------------------------------------------------------------
def _weight_sample(payload: bytes) -> tuple[int, float] | None:
    if len(payload) < 5:
        return None
    return payload[0], int.from_bytes(payload[2:5], "big") / 1000.0


def _result(payload: bytes) -> dict[str, Any] | None:
    if len(payload) < 34:
        return None
    raw = [int.from_bytes(payload[10 + 2 * i:12 + 2 * i], "big") for i in range(10)]
    return {
        "timestamp": int.from_bytes(payload[0:4], "big"),
        "weight_kg": int.from_bytes(payload[5:8], "big") / 1000.0,
        "impedance_raw": raw,
        "device_id": payload[30:34].hex(),
    }


def _impedance_extras(raw: list[int]) -> dict[str, Any]:
    ohms = [v / 10.0 for v in raw]
    return {
        "impedance_raw": raw,
        "impedance_segments_ohm": {
            seg: {"f1": ohms[i], "f2": ohms[i + 5]} for i, seg in enumerate(SEGMENTS)
        },
    }


class FitdaysAdapter:
    """Decoder + write planner for the Fitdays/Lefu ``JEETIF`` unit."""

    descriptor = None  # set below (needs the class defined for the type checker)

    def decode(self, frames: Sequence[bytes]) -> ScaleReading | None:
        """Fold a session into a reading.

        A result frame only counts once the weight has **settled**: the scale
        also replays its last stored result on connect, which must never be
        mistaken for this weigh-in. An all-zero impedance block after settling
        means no electrode contact — that is still a final, weight-only reading.
        """
        settled = False
        last_weight: float | None = None
        for frame in frames or ():
            parts = split_frame(frame)
            if parts is None:
                continue
            _, ftype, payload = parts
            if ftype == T_WEIGHT:
                sample = _weight_sample(payload)
                if sample is None:
                    continue
                status, kg = sample
                if kg > 0:
                    last_weight = kg
                if status == STATUS_SETTLED and kg >= _ARM_MIN_KG:
                    settled = True
            elif ftype == T_RESULT and settled:
                res = _result(payload)
                if res is None or not (_ARM_MIN_KG <= res["weight_kg"] <= 300):
                    continue
                extras: dict[str, Any] = {
                    "result_timestamp": res["timestamp"],
                    "device_id": res["device_id"],
                }
                if any(res["impedance_raw"]):
                    extras.update(_impedance_extras(res["impedance_raw"]))
                else:
                    extras["no_impedance"] = True
                return ScaleReading(
                    weight_kg=res["weight_kg"], stable=True, impedance_ohm=None,
                    variant="fitdays_wla37", extras=extras,
                )
        if last_weight is None:
            return None
        return ScaleReading(weight_kg=last_weight, stable=False, variant="fitdays_wla37")

    def plan_writes(
        self,
        frames: Sequence[bytes],
        state: dict[str, Any],
        profile: ScaleProfile | None,
        now: float | None = None,
    ) -> list[bytes]:
        return plan_writes(frames, state, profile, now)


FitdaysAdapter.descriptor = FITDAYS_DESCRIPTOR = AdapterDescriptor(
    id="fitdays",
    label="Fitdays / Lefu 8-electrode scale (full BIA)",
    service_uuid=SERVICE_UUID,
    notify_uuid=NOTIFY_UUID,
    write_uuid=WRITE_UUID,
    indicate_uuid=INDICATE_UUID,
    server_writes=True,
    name_prefixes=["JEETI"],
)


def plan_writes(
    frames: Sequence[bytes],
    state: dict[str, Any],
    profile: ScaleProfile | None,
    now: float | None = None,
) -> list[bytes]:
    """Decide what the browser should write next; mutates ``state``.

    Phases: ``waiting`` (no one on the scale) → ``armed`` (handshake sent once a
    person is standing on it) → ``done`` (result acknowledged). Each phase's
    writes are emitted exactly once, however often the browser polls.
    """
    if profile is None:
        return []
    now = time.time() if now is None else now
    tz = int(time.localtime(now).tm_gmtoff // 60)
    phase = state.setdefault("phase", "waiting")
    seq = state.setdefault("seq", 0)

    token, live_kg, settled, result = _DEFAULT_TOKEN, None, False, None
    for frame in frames or ():
        parts = split_frame(frame)
        if parts is None:
            continue
        _, ftype, payload = parts
        if ftype == T_HELLO and len(payload) >= 5:
            token = payload[-5:]
        elif ftype == T_WEIGHT:
            sample = _weight_sample(payload)
            if sample:
                status, kg = sample
                live_kg = kg
                settled = settled or (status == STATUS_SETTLED and kg >= _ARM_MIN_KG)
        elif ftype == T_RESULT and settled:
            result = _result(payload)

    out: list[bytes] = []

    def nxt() -> int:
        nonlocal seq
        s, seq = seq, (seq + 1) & 0xFF
        return s

    if phase == "waiting" and live_kg is not None and live_kg >= _ARM_MIN_KG:
        w = live_kg
        out = [
            _ctrl(nxt(), 0x30),
            build_c0(nxt(), profile, w, now, tz),
            build_c1(nxt(), profile, w),
            build_c0(nxt(), profile, w, now, tz),
            make_frame(nxt(), T_TRIGGER, token),
            build_c0(nxt(), profile, w, now, tz),
            build_c1(nxt(), profile, w),
            build_c0(nxt(), profile, w, now, tz),
            _ctrl(nxt(), 0x31),
            _ctrl(nxt(), 0x39),
        ]
        state["phase"] = "armed"
    elif phase == "armed" and result is not None:
        w = result["weight_kg"]
        out = [_ctrl(nxt(), 0x3A), build_c0(nxt(), profile, w, now, tz), build_c1(nxt(), profile, w)]
        state["phase"] = "done"

    state["seq"] = seq
    return out
