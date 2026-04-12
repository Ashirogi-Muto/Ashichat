"""Session state management for AshiChat.

AUTHORITY RULE: The SQLite sessions table is the source of truth for
send_sequence and recv_sequence. These in-memory fields are a write-through
cache. On restart, values MUST be loaded from DB. On every sequence change,
the DB MUST be updated before the packet is sent (send) or ACK'd (recv).
This prevents sequence reuse after crash.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from ashichat.logging_setup import get_logger

log = get_logger(__name__)

# Max concurrent sessions (resource limit)
MAX_SESSIONS = 100


@dataclass
class SessionKeys:
    """Result of a successful handshake."""

    encryption_key: bytes  # 32 bytes (AES-256)
    session_id: bytes  # 8 bytes (random)
    remote_peer_id: bytes  # 32 bytes
    remote_public_key_bytes: bytes  # 32 bytes raw Ed25519


@dataclass
class Session:
    """Active encrypted session with one peer.

    AUTHORITY RULE: ``send_sequence`` and ``recv_sequence`` are an in-memory
    **write-through cache** of the DB values. The SQLite ``sessions`` table is
    the canonical source of truth. On every increment the DB MUST be updated
    *before* the packet leaves the wire.
    """

    session_id: bytes
    encryption_key: bytes
    remote_peer_id: bytes
    send_sequence: int = 1  # Starts at 1; recv_sequence 0 means "nothing received"
    recv_sequence: int = 0  # Highest sequence seen so far
    created_at: float = field(default_factory=time.time)

    _seq_lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )

    def next_sequence(self) -> int:
        """Atomically increment and return the next send sequence number."""
        with self._seq_lock:
            seq = self.send_sequence
            self.send_sequence += 1
            return seq

    def validate_recv_sequence(self, seq: int) -> bool:
        """Return ``True`` if *seq* > ``recv_sequence`` (reject replays)."""
        if seq <= self.recv_sequence:
            return False
        self.recv_sequence = seq
        return True


class SessionRegistry:
    """Thread-safe registry of active sessions.

    Provides lookup by ``session_id`` or ``peer_id``.
    Enforces ``MAX_SESSIONS`` limit.
    """

    def __init__(self) -> None:
        self._by_session: dict[bytes, Session] = {}
        self._by_peer: dict[bytes, Session] = {}
        self._lock = threading.Lock()

    def register(self, session: Session) -> None:
        """Register a new session.  Raises ``RuntimeError`` if at capacity."""
        with self._lock:
            if len(self._by_session) >= MAX_SESSIONS:
                raise RuntimeError(
                    f"Session limit reached ({MAX_SESSIONS})"
                )
            # If there's an existing session for this peer, remove it
            old = self._by_peer.get(session.remote_peer_id)
            if old is not None:
                self._by_session.pop(old.session_id, None)
            self._by_session[session.session_id] = session
            self._by_peer[session.remote_peer_id] = session

    def get_by_session_id(self, session_id: bytes) -> Session | None:
        return self._by_session.get(session_id)

    def get_by_peer_id(self, peer_id: bytes) -> Session | None:
        return self._by_peer.get(peer_id)

    def remove(self, session_id: bytes) -> None:
        with self._lock:
            session = self._by_session.pop(session_id, None)
            if session is not None:
                self._by_peer.pop(session.remote_peer_id, None)

    def active_count(self) -> int:
        return len(self._by_session)

    def all_sessions(self) -> list[Session]:
        return list(self._by_session.values())
