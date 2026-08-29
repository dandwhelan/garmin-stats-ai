"""Adapter registry — the single place a new scale family is registered."""

from __future__ import annotations

from typing import Any

from .base import ScaleAdapter
from .lefu import LefuAdapter

__all__ = ["ADAPTERS", "get_adapter", "descriptors"]

#: Adding a scale family is a one-line change here.
ADAPTERS: dict[str, ScaleAdapter] = {
    "lefu": LefuAdapter(),
}


def get_adapter(adapter_id: str | None) -> ScaleAdapter | None:
    """Look up an adapter by id; ``None`` for unknown/blank ids."""
    if not adapter_id or not isinstance(adapter_id, str):
        return None
    return ADAPTERS.get(adapter_id.strip().lower())


def descriptors() -> list[dict[str, Any]]:
    """JSON-safe descriptors for every adapter (served to the browser)."""
    return [adapter.descriptor.to_dict() for adapter in ADAPTERS.values()]
