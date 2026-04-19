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
    """Manages reconnection attempts with exponential backoff.

    The Node must call :meth:`set_connect_fn` before starting, and
    :meth:`start` to launch the background reconnection loop.
    """

    def __init__(self) -> None:
        # peer_id → {attempt, first_disconnect, next_retry}
        self._state: dict[bytes, dict] = {}
        self._connect_fn = None  # async fn(peer_id) set by Node
        self._archive_fn = None  # async fn(peer_id) set by Node
        self._task: asyncio.Task | None = None
        self._running = False

    def set_connect_fn(self, fn) -> None:
        """Set the async callback: ``async fn(peer_id) -> None``."""
        self._connect_fn = fn

    def set_archive_fn(self, fn) -> None:
        """Set the async callback for archiving: ``async fn(peer_id) -> None``."""
        self._archive_fn = fn

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

    def get_attempt_count(self, peer_id: bytes) -> int:
        state = self._state.get(peer_id)
        if state is None:
            return 0
        return int(state.get("attempt", 0))

    def record_disconnect(self, peer_id: bytes) -> None:
        if peer_id not in self._state:
            self._state[peer_id] = {
                "attempt": 0,
                "first_disconnect": time.time(),
                "next_retry": time.time() + BACKOFF_SCHEDULE[0],
            }

    def record_connect(self, peer_id: bytes) -> None:
        self._state.pop(peer_id, None)

    def record_attempt(self, peer_id: bytes) -> None:
        state = self._state.setdefault(peer_id, {
            "attempt": 0,
            "first_disconnect": time.time(),
            "next_retry": time.time(),
        })
        state["attempt"] = state.get("attempt", 0) + 1
        backoff = self.get_backoff(peer_id)
        state["next_retry"] = time.time() + backoff

    # -- Lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Launch the background reconnection loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._reconnect_loop())
        log.info("Reconnect loop started")

    async def stop(self) -> None:
        """Stop the background reconnection loop."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        log.info("Reconnect loop stopped")

    async def _reconnect_loop(self) -> None:
        """Background loop: check disconnected peers and try reconnecting."""
        try:
            while self._running:
                now = time.time()
                for peer_id, state in list(self._state.items()):
                    if now < state.get("next_retry", 0):
                        continue

                    # Check for ARCHIVED threshold (7 days)
                    if self.should_archive(peer_id):
                        if self._archive_fn:
                            try:
                                await self._archive_fn(peer_id)
                            except Exception:
                                log.exception(
                                    "Archive callback failed for %s",
                                    peer_id.hex()[:8],
                                )
                        # Archived peers retry every 24h
                        state["next_retry"] = now + ARCHIVE_RETRY
                        continue

                    # Attempt reconnection
                    if self._connect_fn:
                        try:
                            log.info(
                                "Reconnect attempt %d for %s (backoff %.0fs)",
                                state.get("attempt", 0) + 1,
                                peer_id.hex()[:8],
                                self.get_backoff(peer_id),
                            )
                            await self._connect_fn(peer_id)
                        except Exception:
                            log.exception(
                                "Reconnect failed for %s",
                                peer_id.hex()[:8],
                            )

                    self.record_attempt(peer_id)

                # Check every 5 seconds
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            pass
