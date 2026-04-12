"""Reconnection manager with exponential backoff.

Backoff: 1s → 2s → 4s → 8s → 16s → 32s → 1m → 5m → 15m → 1h → 6h cap
After 7 days offline → ARCHIVED, retry every 24h.
"""

from __future__ import annotations

import asyncio
import time

from ashichat.logging_setup import get_logger

log = get_logger(__name__)

BACKOFF_SCHEDULE = [1, 2, 4, 8, 16, 32, 60, 300, 900, 3600, 21600]
ARCHIVE_DAYS = 7
ARCHIVE_RETRY = 86400  # 24h


class ReconnectManager:
    """Manages reconnection attempts with exponential backoff."""

    def __init__(self) -> None:
        # peer_id → {attempt, first_disconnect, task}
        self._state: dict[bytes, dict] = {}
        self._connect_fn = None  # set by Node

    def set_connect_fn(self, fn) -> None:
        self._connect_fn = fn

    def get_backoff(self, peer_id: bytes) -> float:
        """Current backoff interval for a peer."""
        state = self._state.get(peer_id)
        if state is None:
            return BACKOFF_SCHEDULE[0]
        attempt = state.get("attempt", 0)
        if attempt >= len(BACKOFF_SCHEDULE):
            return BACKOFF_SCHEDULE[-1]  # cap at 6h
        return BACKOFF_SCHEDULE[attempt]

    def should_archive(self, peer_id: bytes) -> bool:
        state = self._state.get(peer_id)
        if state is None:
            return False
        return time.time() - state["first_disconnect"] > ARCHIVE_DAYS * 86400

    def record_disconnect(self, peer_id: bytes) -> None:
        if peer_id not in self._state:
            self._state[peer_id] = {
                "attempt": 0,
                "first_disconnect": time.time(),
            }

    def record_connect(self, peer_id: bytes) -> None:
        self._state.pop(peer_id, None)

    def record_attempt(self, peer_id: bytes) -> None:
        state = self._state.setdefault(peer_id, {
            "attempt": 0,
            "first_disconnect": time.time(),
        })
        state["attempt"] = state.get("attempt", 0) + 1
