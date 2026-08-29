"""Shared types for server-side Bluetooth scale decoding.

The browser is a dumb BLE pipe: it discovers the device, writes the
handshake frames the adapter descriptor asks for, and POSTs every notify
payload back as a hex string. All parsing and all body-composition maths
happen here so they are unit-testable (they used to live untested in
``web/static/app.js``).

Every input reaching these types comes from an untrusted source (a browser
relaying bytes from an unknown-firmware scale), so nothing in this module
raises on malformed input — bad frames are dropped, not fatal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence, runtime_checkable

__all__ = [
    "AdapterDescriptor",
    "ScaleReading",
    "ScaleAdapter",
    "parse_hex_frames",
]


@dataclass(frozen=True)
class AdapterDescriptor:
    """Everything the browser needs to talk to one family of scale.

    Serialised verbatim to the frontend by ``GET /api/scale/adapters``, so
    every field must stay JSON-safe (see :meth:`to_dict`).
    """

    id: str
    label: str
    service_uuid: str
    notify_uuid: str
    write_uuid: str
    #: Hex frames the browser writes to ``write_uuid``, in order, after
    #: subscribing to notifications. Empty list = no handshake needed.
    handshake: list[str] = field(default_factory=list)
    #: Optional keepalive: ``{"frame": "<hex>", "interval_ms": int}``.
    #: Some scales stop notifying unless periodically poked.
    poll: dict[str, Any] | None = None
    #: Fallback ``requestDevice`` name filters, for units that do not
    #: advertise their vendor service UUID in the advertisement packet.
    name_prefixes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Plain JSON-safe dict for the frontend."""
        return {
            "id": self.id,
            "label": self.label,
            "service_uuid": self.service_uuid,
            "notify_uuid": self.notify_uuid,
            "write_uuid": self.write_uuid,
            "handshake": list(self.handshake),
            "poll": dict(self.poll) if self.poll else None,
            "name_prefixes": list(self.name_prefixes),
        }


@dataclass
class ScaleReading:
    """One decoded measurement.

    ``stable`` marks a final (settled) reading as opposed to a live
    fluctuating one. ``extras`` carries anything decoded-but-not-trusted:
    per-channel impedances, candidate offsets, undecoded-yet-plausible
    fields — useful for diagnosing an unmatched firmware revision without
    letting speculative numbers into the main fields.
    """

    weight_kg: float
    stable: bool
    impedance_ohm: float | None = None
    variant: str = ""
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "weight_kg": self.weight_kg,
            "stable": self.stable,
            "impedance_ohm": self.impedance_ohm,
            "variant": self.variant,
            "extras": dict(self.extras),
        }


@runtime_checkable
class ScaleAdapter(Protocol):
    """Protocol every scale adapter implements."""

    descriptor: AdapterDescriptor

    def decode(self, frames: Sequence[bytes]) -> ScaleReading | None:
        """Fold a whole session's notify frames into the best reading.

        Implementations must never raise: garbage, truncated, empty and
        unknown-length frames all yield ``None``.
        """
        ...


def parse_hex_frames(frames: Sequence[str]) -> list[bytes]:
    """Decode browser-supplied hex strings to bytes, dropping junk.

    Tolerates whitespace, ``0x``/``0X`` prefixes, colon/dash separators and
    mixed case. Anything that is not an even-length run of hex digits is
    silently skipped — never raises, because the caller is an untrusted
    HTTP body.
    """
    out: list[bytes] = []
    if not frames:
        return out
    for raw in frames:
        if isinstance(raw, (bytes, bytearray)):  # already decoded — pass through
            out.append(bytes(raw))
            continue
        if not isinstance(raw, str):
            continue
        cleaned = raw.strip().replace("0x", "").replace("0X", "")
        for sep in (" ", "\t", "\n", "\r", ":", "-", ","):
            cleaned = cleaned.replace(sep, "")
        if not cleaned or len(cleaned) % 2:
            continue
        try:
            out.append(bytes.fromhex(cleaned))
        except ValueError:
            continue
    return out
