"""Peer state machine for AshiChat.

Deterministic state transitions with event-driven callbacks for UI.
"""

from __future__ import annotations

import asyncio
import time
from enum import Enum
from typing import Callable

from ashichat.logging_setup import get_logger

log = get_logger(__name__)


class PeerState(Enum):
    """Connection states for a peer."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    IDLE = "idle"
    SUSPECT = "suspect"
    RESOLVING = "resolving"
    FAILED = "failed"
    ARCHIVED = "archived"


# Valid transitions — deterministic
VALID_TRANSITIONS: dict[PeerState, set[PeerState]] = {
    PeerState.DISCONNECTED: {PeerState.CONNECTING, PeerState.RESOLVING, PeerState.ARCHIVED},
    PeerState.CONNECTING: {PeerState.CONNECTED, PeerState.DISCONNECTED},
    PeerState.CONNECTED: {PeerState.IDLE, PeerState.DISCONNECTED},
    PeerState.IDLE: {PeerState.SUSPECT, PeerState.CONNECTED, PeerState.DISCONNECTED},
    PeerState.SUSPECT: {PeerState.DISCONNECTED, PeerState.CONNECTED},
    PeerState.RESOLVING: {PeerState.CONNECTING, PeerState.FAILED},
    PeerState.FAILED: {PeerState.DISCONNECTED, PeerState.RESOLVING},
    PeerState.ARCHIVED: {PeerState.CONNECTING, PeerState.RESOLVING},
}

# 7 days in seconds
ARCHIVE_THRESHOLD = 7 * 24 * 3600


class PeerStateMachine:
    """State machine for a single peer with transition validation."""

    def __init__(self, initial: PeerState = PeerState.DISCONNECTED) -> None:
        self._state = initial
        self._last_change = time.time()

    @property
    def state(self) -> PeerState:
        return self._state

    @property
    def time_in_state(self) -> float:
        return time.time() - self._last_change

    def transition(self, new_state: PeerState) -> PeerState:
        """Transition to *new_state*.

        Returns the old state.
        Raises ``ValueError`` if the transition is invalid.
        """
        valid = VALID_TRANSITIONS.get(self._state, set())
        if new_state not in valid:
            raise ValueError(
                f"Invalid transition: {self._state.value} → {new_state.value}"
            )
        old = self._state
        self._state = new_state
        self._last_change = time.time()
        return old

    def should_archive(self) -> bool:
        """Check if DISCONNECTED > 7 days → should transition to ARCHIVED."""
        return (
            self._state == PeerState.DISCONNECTED
            and self.time_in_state > ARCHIVE_THRESHOLD
        )


class PeerStateManager:
    """Manages state machines for all peers with event callbacks."""

    def __init__(self) -> None:
        self._machines: dict[bytes, PeerStateMachine] = {}
        self._callbacks: list[Callable[[bytes, PeerState, PeerState], None]] = []

    def on_state_change(
        self, callback: Callable[[bytes, PeerState, PeerState], None]
    ) -> None:
        """Register a callback: ``callback(peer_id, old_state, new_state)``."""
        self._callbacks.append(callback)

    def get_state(self, peer_id: bytes) -> PeerState:
        machine = self._machines.get(peer_id)
        if machine is None:
            return PeerState.DISCONNECTED
        return machine.state

    def ensure_machine(
        self, peer_id: bytes, initial: PeerState = PeerState.DISCONNECTED
    ) -> PeerStateMachine:
        if peer_id not in self._machines:
            self._machines[peer_id] = PeerStateMachine(initial)
        return self._machines[peer_id]

    async def update_state(self, peer_id: bytes, new_state: PeerState) -> None:
        machine = self.ensure_machine(peer_id)
        old = machine.transition(new_state)
        log.info(
            "Peer %s: %s → %s",
            peer_id.hex()[:8],
            old.value,
            new_state.value,
        )
        for cb in self._callbacks:
            try:
                cb(peer_id, old, new_state)
            except Exception:
                log.exception("State change callback error")

    def all_peers(self) -> dict[bytes, PeerState]:
        return {pid: m.state for pid, m in self._machines.items()}
