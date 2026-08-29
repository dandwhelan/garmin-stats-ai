"""Fitdays / Lefu (OEM) 8-electrode scale — vendor GATT service 0xFFB0.

These units (sold as "Health Scale", Hutbit, CH-xxx and a dozen other
badges) pair with the Fitdays app and expose a proprietary service instead
of the standard Weight Scale service 0x181B. The browser subscribes to
0xFFB2, writes the five handshake frames to 0xFFB1, then keeps poking the
last handshake frame once a second so the scale keeps streaming.

Two on-the-wire layouts are known and auto-detected:

* ``lefu_ac02`` — 8-byte ``AC 02 | D0 D1 D2 D3 | STATUS | CKSUM`` frames,
  the layout the handshake above elicits.
* ``lefu_offset`` — the E.volve / ESPHome layout, where bytes 3..5 are a
  big-endian u24 biased by 0x680000 and scaled by 1000.

Protocol assumptions worth knowing (documented rather than hidden):

* Only ``STATUS`` 0xCE (live) and 0xCA (settled) are treated as weight
  frames; other status bytes are some other message type and are ignored.
* Any 8-byte frame starting ``AC 02`` is considered part of the ac02
  family and is never re-interpreted as an offset frame, so a checksum
  failure means "drop", not "try the other layout".
* Impedance decoding is deliberately conservative — see
  :data:`_IMPEDANCE_MIN` / :data:`_IMPEDANCE_MAX`.
"""

from __future__ import annotations

from typing import Any, Sequence

from .base import AdapterDescriptor, ScaleReading

__all__ = ["LefuAdapter", "LEFU_DESCRIPTOR"]

# --- ac02 layout ----------------------------------------------------------
_AC02_LEN = 8
_AC02_PREFIX = (0xAC, 0x02)
_STATUS_LIVE = 0xCE
_STATUS_STABLE = 0xCA

# --- offset layout --------------------------------------------------------
_OFFSET_BIAS = 0x680000
_OFFSET_SCALE = 1000.0
_OFFSET_MIN_KG = 2.0
_OFFSET_MAX_KG = 300.0
#: How many consecutive identical offset frames before we call it settled.
_OFFSET_STABLE_RUN = 3

# --- impedance ------------------------------------------------------------
_IMPEDANCE_FRAME_LEN = 40
_IMPEDANCE_FRAME_PREFIX = 0xFF
#: Plausible whole-body BIA impedance for a consumer 50 kHz scale. The gate
#: is strict on purpose: a published decode of this frame produced nonsense
#: (0.4-0.7 % body fat), so "weight only" beats a fabricated composition.
_IMPEDANCE_MIN = 150
_IMPEDANCE_MAX = 1200
#: Wider window recorded (but never accepted) in ``extras`` so a firmware
#: revision we do not match yet can be diagnosed from a real capture.
_CANDIDATE_MIN = 50
_CANDIDATE_MAX = 5000


LEFU_DESCRIPTOR = AdapterDescriptor(
    id="lefu",
    label="Fitdays / Lefu 8-electrode scale (FFB0)",
    service_uuid="0000ffb0-0000-1000-8000-00805f9b34fb",
    notify_uuid="0000ffb2-0000-1000-8000-00805f9b34fb",
    write_uuid="0000ffb1-0000-1000-8000-00805f9b34fb",
    handshake=[
        "ac02fa010000ccc7",
        "ac02fb021fa5cc8d",
        "ac02fde20101ccad",
        "ac02fc010000ccc9",
        "ac02fe060000ccd0",
    ],
    poll={"frame": "ac02fe060000ccd0", "interval_ms": 1000},
    # "JEETI" covers the JEETIFxxxx-style random alphanumeric names some
    # Fitdays-paired units advertise instead of a brand string.
    name_prefixes=["Health Scale", "Lefu", "CH", "Hutbit", "Scale", "JEETI"],
)


def _is_ac02_family(frame: bytes) -> bool:
    """True for any 8-byte AC 02 frame, checksum valid or not."""
    return len(frame) == _AC02_LEN and (frame[0], frame[1]) == _AC02_PREFIX


def _decode_ac02(frame: bytes) -> tuple[float, bool] | None:
    """``AC 02 | D0 D1 D2 D3 | STATUS | CKSUM`` -> (kg, stable)."""
    if not _is_ac02_family(frame):
        return None
    d0, d1, d2, d3, status, cksum = frame[2], frame[3], frame[4], frame[5], frame[6], frame[7]
    if ((d0 + d1 + d2 + d3 + status) & 0xFF) != cksum:
        return None
    if status not in (_STATUS_LIVE, _STATUS_STABLE):
        return None  # not a weight frame
    weight_kg = int.from_bytes(bytes((d0, d1)), "big") / 10.0
    return weight_kg, status == _STATUS_STABLE


def _decode_offset(frame: bytes) -> float | None:
    """E.volve/ESPHome layout: u24 at bytes 3..5, biased and /1000."""
    if len(frame) < 6 or _is_ac02_family(frame):
        return None
    raw = int.from_bytes(frame[3:6], "big")
    weight_kg = (raw - _OFFSET_BIAS) / _OFFSET_SCALE
    if not (_OFFSET_MIN_KG <= weight_kg <= _OFFSET_MAX_KG):
        return None
    return weight_kg


def _decode_impedance(frame: bytes) -> tuple[int | None, list[dict[str, Any]]]:
    """Scan a 40-byte 0xFF frame for a plausible u16 impedance.

    Returns ``(accepted_or_None, candidates)``. Both endiannesses at every
    offset are considered; the first value inside the strict plausible band
    wins. Candidates in the wider diagnostic window are all recorded.
    """
    if len(frame) != _IMPEDANCE_FRAME_LEN or frame[0] != _IMPEDANCE_FRAME_PREFIX:
        return None, []
    accepted: int | None = None
    candidates: list[dict[str, Any]] = []
    for offset in range(len(frame) - 1):
        pair = frame[offset : offset + 2]
        for endian in ("be", "le"):
            value = int.from_bytes(pair, "big" if endian == "be" else "little")
            if not (_CANDIDATE_MIN <= value <= _CANDIDATE_MAX):
                continue
            candidates.append({"offset": offset, "endian": endian, "value": value})
            if accepted is None and _IMPEDANCE_MIN <= value <= _IMPEDANCE_MAX:
                accepted = value
    return accepted, candidates


class LefuAdapter:
    """Adapter for the Fitdays/Lefu FFB0 family."""

    descriptor = LEFU_DESCRIPTOR

    def decode(self, frames: Sequence[bytes]) -> ScaleReading | None:
        """Fold the whole session buffer into the best reading so far.

        The HTTP endpoint replays every frame received to date on each
        call, so this is intentionally a pure fold with no adapter state.
        A stable reading always beats a live one; within a class the most
        recent frame wins. Returns ``None`` when no weight was decodable.
        """
        if not frames:
            return None

        best_stable: tuple[float, str] | None = None
        last_live: tuple[float, str] | None = None
        impedance: int | None = None
        candidates: list[dict[str, Any]] = []

        run_value: float | None = None
        run_len = 0

        for frame in frames:
            if not isinstance(frame, (bytes, bytearray)) or not frame:
                run_value, run_len = None, 0
                continue
            frame = bytes(frame)

            # Impedance frames are checked first: a 40-byte 0xFF payload is
            # never a weight frame, and its bytes 3..5 could otherwise
            # coincidentally satisfy the offset layout and be eaten as one.
            if len(frame) == _IMPEDANCE_FRAME_LEN and frame[0] == _IMPEDANCE_FRAME_PREFIX:
                found, found_candidates = _decode_impedance(frame)
                if found is not None:
                    impedance = found
                if found_candidates:
                    candidates = found_candidates
                run_value, run_len = None, 0
                continue

            ac02 = _decode_ac02(frame)
            if ac02 is not None:
                weight, stable = ac02
                if stable:
                    best_stable = (weight, "lefu_ac02")
                else:
                    last_live = (weight, "lefu_ac02")
                run_value, run_len = None, 0
                continue

            offset_kg = _decode_offset(frame)
            if offset_kg is not None:
                run_len = run_len + 1 if offset_kg == run_value else 1
                run_value = offset_kg
                if run_len >= _OFFSET_STABLE_RUN:
                    best_stable = (offset_kg, "lefu_offset")
                else:
                    last_live = (offset_kg, "lefu_offset")
                continue

            run_value, run_len = None, 0

        chosen = best_stable or last_live
        if chosen is None:
            return None
        weight, variant = chosen
        extras: dict[str, Any] = {}
        if candidates:
            extras["impedance_candidates"] = candidates
        return ScaleReading(
            weight_kg=weight,
            stable=best_stable is not None,
            impedance_ohm=float(impedance) if impedance is not None else None,
            variant=variant,
            extras=extras,
        )
