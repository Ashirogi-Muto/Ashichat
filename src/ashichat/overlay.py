"""Overlay mesh and peer table for AshiChat.

DirectPeers: explicit trusted contacts (not counted toward overlay cap).
OverlayPeers: bounded random subset (default K=30, max 50).
Peer table max: 500 entries.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field

from ashichat.logging_setup import get_logger

log = get_logger(__name__)

MAX_PEER_TABLE = 500
DEFAULT_OVERLAY_K = 30
MAX_OVERLAY_K = 50
ROTATION_INTERVAL = 600  # 10 minutes
INACTIVE_THRESHOLD = 3600  # 1 hour
ROTATION_PERCENT = 0.10  # 10%


@dataclass
class PeerEntry:
    peer_id: bytes
    public_key: bytes
    is_direct: bool = False
    endpoint: tuple[str, int] | None = None
    last_seen: float = field(default_factory=time.time)
    version_counter: int = 0
    nickname: str | None = None
    endpoint_signature: bytes | None = None


class PeerTable:
    """Stores known peers with max 500 entries."""

    def __init__(self, local_peer_id: bytes) -> None:
        self._local_id = local_peer_id
        self._peers: dict[bytes, PeerEntry] = {}

    def add_direct_peer(
        self,
        peer_id: bytes,
        public_key: bytes,
        nickname: str | None = None,
        endpoint: tuple[str, int] | None = None,
    ) -> None:
        """Add an explicitly trusted contact."""
        if peer_id == self._local_id:
            return
        self._peers[peer_id] = PeerEntry(
            peer_id=peer_id,
            public_key=public_key,
            is_direct=True,
            nickname=nickname,
            endpoint=endpoint,
        )

    def add_peer(
        self,
        peer_id: bytes,
        public_key: bytes,
        endpoint: tuple[str, int] | None = None,
    ) -> None:
        """Add a non-direct peer (overlay candidate)."""
        if peer_id == self._local_id:
            return
        if peer_id in self._peers:
            self._peers[peer_id].last_seen = time.time()
            if endpoint:
                self._peers[peer_id].endpoint = endpoint
            return
        if len(self._peers) >= MAX_PEER_TABLE:
            self._evict()
        self._peers[peer_id] = PeerEntry(
            peer_id=peer_id,
            public_key=public_key,
            endpoint=endpoint,
        )

    def remove_peer(self, peer_id: bytes) -> None:
        self._peers.pop(peer_id, None)

    def get_direct_peers(self) -> list[PeerEntry]:
        return [p for p in self._peers.values() if p.is_direct]

    def get_direct_peer_ids(self) -> set[bytes]:
        return {p.peer_id for p in self._peers.values() if p.is_direct}

    def get_all_non_direct(self) -> list[PeerEntry]:
        return [p for p in self._peers.values() if not p.is_direct]

    def is_known(self, peer_id: bytes) -> bool:
        return peer_id in self._peers

    def get_entry(self, peer_id: bytes) -> PeerEntry | None:
        return self._peers.get(peer_id)

    def get_endpoint(self, peer_id: bytes) -> tuple[str, int] | None:
        entry = self._peers.get(peer_id)
        return entry.endpoint if entry else None

    def update_last_seen(self, peer_id: bytes) -> None:
        entry = self._peers.get(peer_id)
        if entry:
            entry.last_seen = time.time()

    def size(self) -> int:
        return len(self._peers)

    def _evict(self) -> None:
        """Evict one peer when table is full.

        Priority: oldest inactive non-DirectPeers, then lowest version.
        """
        candidates = sorted(
            (p for p in self._peers.values() if not p.is_direct),
            key=lambda p: (p.last_seen, p.version_counter),
        )
        if candidates:
            evicted = candidates[0]
            del self._peers[evicted.peer_id]
            log.debug("Evicted peer %s", evicted.peer_id.hex()[:8])


class OverlayManager:
    """Manages the bounded overlay peer subset."""

    def __init__(self, peer_table: PeerTable, overlay_k: int = DEFAULT_OVERLAY_K) -> None:
        self._table = peer_table
        self._k = min(overlay_k, MAX_OVERLAY_K)
        self._overlay: list[bytes] = []
        self._task: asyncio.Task | None = None

    def select_overlay(self) -> list[bytes]:
        """Select K overlay peers with 60% priority indirect + 40% random."""
        non_direct = self._table.get_all_non_direct()
        active = [
            p for p in non_direct
            if time.time() - p.last_seen < INACTIVE_THRESHOLD
        ]

        # 60% priority (indirect/recently active)
        priority_count = int(self._k * 0.6)
        random.shuffle(active)
        priority = [p.peer_id for p in active[:priority_count]]

        # Remaining filled randomly from all known non-direct
        remaining_count = self._k - len(priority)
        remaining_pool = [p.peer_id for p in non_direct if p.peer_id not in set(priority)]
        random.shuffle(remaining_pool)
        remaining = remaining_pool[:remaining_count]

        self._overlay = priority + remaining
        return self._overlay

    def get_overlay(self) -> list[bytes]:
        return list(self._overlay)

    async def rotate(self) -> None:
        """Replace 10% of overlay peers, drop inactive > 1 hour."""
        if not self._overlay:
            self.select_overlay()
            return

        # Drop inactive
        self._overlay = [
            pid for pid in self._overlay
            if self._table.get_entry(pid) is not None
            and time.time() - self._table.get_entry(pid).last_seen < INACTIVE_THRESHOLD
        ]

        # Replace 10%
        replace_count = max(1, int(len(self._overlay) * ROTATION_PERCENT))
        if len(self._overlay) > replace_count:
            # Remove the oldest
            self._overlay = self._overlay[replace_count:]

        # Fill back up to K
        current = set(self._overlay)
        candidates = [
            p.peer_id
            for p in self._table.get_all_non_direct()
            if p.peer_id not in current
            and time.time() - p.last_seen < INACTIVE_THRESHOLD
        ]
        random.shuffle(candidates)
        needed = self._k - len(self._overlay)
        self._overlay.extend(candidates[:needed])

    async def start_rotation_loop(self) -> None:
        """Background task: rotate every 10 minutes."""
        self._task = asyncio.create_task(self._rotation_loop())

    async def _rotation_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(ROTATION_INTERVAL)
                await self.rotate()
                log.debug("Overlay rotated: %d peers", len(self._overlay))
        except asyncio.CancelledError:
            pass

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
