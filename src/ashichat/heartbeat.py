"""Heartbeat manager — PING/PONG with adaptive intervals.

Intervals:
    < 1 min idle: 10 s
    1–5 min idle: 20 s
    > 5 min idle: 60 s
    Reset to 10 s on any traffic.

Suspicion:
    3 missed pings → SUSPECT
    6 missed pings → DISCONNECTED
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Callable

from ashichat.logging_setup import get_logger
from ashichat.packet import PacketType, PingPayload, PongPayload, make_packet

log = get_logger(__name__)


class HeartbeatManager:
    """Manages per-peer heartbeat loops."""

    def __init__(
        self,
        send_fn: Callable,  # send_fn(packet, addr)
        on_suspect: Callable[[bytes], None] | None = None,
        on_disconnect: Callable[[bytes], None] | None = None,
    ) -> None:
        self._send_fn = send_fn
        self._on_suspect = on_suspect
        self._on_disconnect = on_disconnect

        # peer_id → task
        self._tasks: dict[bytes, asyncio.Task] = {}
        # peer_id → last traffic timestamp
        self._last_traffic: dict[bytes, float] = {}
        # peer_id → pending ping_id
        self._pending_pings: dict[bytes, bytes] = {}
        # peer_id → (session_id, addr)
        self._peer_info: dict[bytes, tuple[bytes, tuple[str, int]]] = {}

    def record_traffic(self, peer_id: bytes) -> None:
        """Call on any valid authenticated packet to reset idle timer."""
        self._last_traffic[peer_id] = time.time()

    def register_peer(
        self, peer_id: bytes, session_id: bytes, addr: tuple[str, int]
    ) -> None:
        """Register session info for a peer."""
        self._peer_info[peer_id] = (session_id, addr)
        self._last_traffic[peer_id] = time.time()

    def _get_interval(self, peer_id: bytes) -> float:
        """Adaptive heartbeat interval based on idle duration."""
        last = self._last_traffic.get(peer_id, time.time())
        idle = time.time() - last
        if idle < 60:
            return 10.0
        elif idle < 300:
            return 20.0
        else:
            return 60.0

    async def start_heartbeat(self, peer_id: bytes) -> None:
        """Start heartbeat loop for a connected peer."""
        await self.stop_heartbeat(peer_id)
        self._tasks[peer_id] = asyncio.create_task(self._heartbeat_loop(peer_id))

    async def stop_heartbeat(self, peer_id: bytes) -> None:
        """Stop heartbeat for a peer."""
        task = self._tasks.pop(peer_id, None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def handle_pong(self, pong: PongPayload) -> bytes | None:
        """Handle a PONG response.  Returns the peer_id if matched."""
        for pid, pending in list(self._pending_pings.items()):
            if pending == pong.ping_id:
                del self._pending_pings[pid]
                self.record_traffic(pid)
                return pid
        return None

    async def _heartbeat_loop(self, peer_id: bytes) -> None:
        missed = 0
        try:
            while True:
                interval = self._get_interval(peer_id)
                await asyncio.sleep(interval)

                info = self._peer_info.get(peer_id)
                if info is None:
                    break

                session_id, addr = info
                ping_id = os.urandom(8)
                self._pending_pings[peer_id] = ping_id

                ping = make_packet(
                    PacketType.PING,
                    PingPayload(session_id=session_id, ping_id=ping_id),
                )
                self._send_fn(ping, addr)

                # Wait for PONG
                await asyncio.sleep(min(interval, 5.0))

                if peer_id in self._pending_pings:
                    # No PONG received
                    missed += 1
                    del self._pending_pings[peer_id]

                    if missed >= 6:
                        log.warning("Peer %s: 6 missed pings → DISCONNECTED", peer_id.hex()[:8])
                        if self._on_disconnect:
                            self._on_disconnect(peer_id)
                        break
                    elif missed >= 3:
                        log.info("Peer %s: %d missed pings → SUSPECT", peer_id.hex()[:8], missed)
                        if self._on_suspect:
                            self._on_suspect(peer_id)
                else:
                    missed = 0

        except asyncio.CancelledError:
            pass

    async def stop_all(self) -> None:
        """Stop all heartbeat loops."""
        for peer_id in list(self._tasks):
            await self.stop_heartbeat(peer_id)
