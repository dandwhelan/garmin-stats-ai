"""Server-side Bluetooth scale decoding + body-composition maths.

The browser is a dumb BLE pipe (discover, handshake, relay notify frames as
hex); everything that can be wrong is decided here, where it is testable.
"""

from __future__ import annotations

from .base import AdapterDescriptor, ScaleAdapter, ScaleReading, parse_hex_frames
from .composition import compute_body_composition
from .lefu import LEFU_DESCRIPTOR, LefuAdapter
from .registry import ADAPTERS, descriptors, get_adapter

__all__ = [
    "AdapterDescriptor",
    "ScaleAdapter",
    "ScaleReading",
    "parse_hex_frames",
    "compute_body_composition",
    "LefuAdapter",
    "LEFU_DESCRIPTOR",
    "ADAPTERS",
    "get_adapter",
    "descriptors",
]
