"""Per-client conversation session manager — keeps chat history separate per browser."""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict


class SessionManager:
    """Thread-safe LRU cache of chat histories keyed by session id.

    Each browser/client gets its own conversation list so concurrent requests
    don't trample each other's history.
    """

    def __init__(self, ttl_seconds: int = 3600, max_sessions: int = 200) -> None:
        self._ttl = ttl_seconds
        self._max = max_sessions
        self._sessions: OrderedDict[str, dict] = OrderedDict()
        self._lock = threading.Lock()

    def get_or_create(self, session_id: str | None) -> tuple[str, list[dict]]:
        """Return (session_id, history_list). Creates a new session if needed."""
        with self._lock:
            self._evict_expired_locked()
            if session_id and session_id in self._sessions:
                entry = self._sessions[session_id]
                entry["last_seen"] = time.time()
                self._sessions.move_to_end(session_id)
                return session_id, entry["history"]

            new_id = session_id or str(uuid.uuid4())
            if len(self._sessions) >= self._max:
                self._sessions.popitem(last=False)
            self._sessions[new_id] = {
                "history": [],
                "last_seen": time.time(),
                "created": time.time(),
            }
            return new_id, self._sessions[new_id]["history"]

    def reset(self, session_id: str) -> None:
        with self._lock:
            if session_id in self._sessions:
                self._sessions[session_id]["history"] = []
                self._sessions[session_id]["last_seen"] = time.time()

    def get_history(self, session_id: str) -> list[dict] | None:
        with self._lock:
            entry = self._sessions.get(session_id)
            return entry["history"] if entry else None

    def _evict_expired_locked(self) -> None:
        now = time.time()
        expired = [
            sid for sid, e in self._sessions.items()
            if now - e["last_seen"] > self._ttl
        ]
        for sid in expired:
            del self._sessions[sid]

    def stats(self) -> dict:
        with self._lock:
            return {
                "active_sessions": len(self._sessions),
                "max_sessions": self._max,
                "ttl_seconds": self._ttl,
            }
