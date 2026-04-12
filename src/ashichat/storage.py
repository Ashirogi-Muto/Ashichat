"""Storage layer for AshiChat — SQLite metadata + encrypted message logs.

AUTHORITY RULE: The SQLite sessions table is the SOURCE OF TRUTH for session
sequence numbers. The in-memory Session dataclass caches these values for
performance, but on restart they MUST be loaded from here.
On every sequence change, DB is updated BEFORE the packet is sent/ACK'd.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

from ashichat.logging_setup import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Record types
# ---------------------------------------------------------------------------

@dataclass
class PeerRecord:
    peer_id: bytes
    public_key: bytes
    last_known_endpoint: str | None
    version_counter: int
    last_seen: float | None
    nickname: str | None
    archived: bool = False


@dataclass
class QueueRecord:
    message_id: str
    receiver: bytes
    status: str
    retry_count: int
    created_at: float
    updated_at: float


@dataclass
class SessionRecord:
    session_id: bytes
    peer_id: bytes
    send_sequence: int
    recv_sequence: int
    created_at: float


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS peers (
    peer_id BLOB PRIMARY KEY,
    public_key BLOB NOT NULL,
    last_known_endpoint TEXT,
    version_counter INTEGER DEFAULT 0,
    last_seen REAL,
    nickname TEXT,
    archived INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS message_queue (
    message_id TEXT PRIMARY KEY,
    receiver BLOB NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    retry_count INTEGER DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (receiver) REFERENCES peers(peer_id)
);

-- AUTHORITY RULE: This table is the SOURCE OF TRUTH for session sequence
-- numbers. The in-memory Session dataclass caches these values for
-- performance, but on restart they MUST be loaded from here.
-- On every sequence change, DB is updated BEFORE the packet is sent/ACK'd.
CREATE TABLE IF NOT EXISTS sessions (
    session_id BLOB PRIMARY KEY,
    peer_id BLOB NOT NULL,
    send_sequence INTEGER DEFAULT 0,
    recv_sequence INTEGER DEFAULT 0,
    created_at REAL NOT NULL,
    FOREIGN KEY (peer_id) REFERENCES peers(peer_id)
);
"""


# ---------------------------------------------------------------------------
# StorageManager
# ---------------------------------------------------------------------------

class StorageManager:
    """Async SQLite metadata store."""

    def __init__(self) -> None:
        self._db: aiosqlite.Connection | None = None

    async def initialize(self, db_path: Path) -> None:
        """Open DB and create schema if needed."""
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(db_path))
        await self._db.executescript(_SCHEMA)
        # Lightweight migration for existing DBs created before `archived` column.
        async with self._db.execute("PRAGMA table_info(peers)") as cur:
            cols = [row[1] async for row in cur]
        if "archived" not in cols:
            await self._db.execute(
                "ALTER TABLE peers ADD COLUMN archived INTEGER DEFAULT 0"
            )
        await self._db.commit()
        log.info("Storage initialized at %s", db_path)

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    # -- Peer operations -----------------------------------------------------

    async def add_peer(
        self, peer_id: bytes, public_key: bytes, nickname: str | None = None
    ) -> None:
        await self._db.execute(
            "INSERT OR REPLACE INTO peers (peer_id, public_key, nickname, version_counter, last_seen) "
            "VALUES (?, ?, ?, 0, ?)",
            (peer_id, public_key, nickname, time.time()),
        )
        await self._db.commit()

    async def get_peer(self, peer_id: bytes) -> PeerRecord | None:
        async with self._db.execute(
            "SELECT * FROM peers WHERE peer_id = ?", (peer_id,)
        ) as cur:
            row = await cur.fetchone()
            return _row_to_peer(row) if row else None

    async def get_all_peers(self) -> list[PeerRecord]:
        async with self._db.execute("SELECT * FROM peers") as cur:
            return [_row_to_peer(r) async for r in cur]

    async def update_endpoint(
        self, peer_id: bytes, endpoint: str, version_counter: int
    ) -> bool:
        """Update endpoint only if version_counter > stored version."""
        async with self._db.execute(
            "SELECT version_counter FROM peers WHERE peer_id = ?", (peer_id,)
        ) as cur:
            row = await cur.fetchone()
            if row and row[0] >= version_counter:
                return False  # stale

        await self._db.execute(
            "UPDATE peers SET last_known_endpoint = ?, version_counter = ?, last_seen = ? "
            "WHERE peer_id = ?",
            (endpoint, version_counter, time.time(), peer_id),
        )
        await self._db.commit()
        return True

    async def update_last_seen(self, peer_id: bytes) -> None:
        await self._db.execute(
            "UPDATE peers SET last_seen = ? WHERE peer_id = ?",
            (time.time(), peer_id),
        )
        await self._db.commit()

    async def update_peer_nickname(self, peer_id: bytes, nickname: str | None) -> None:
        await self._db.execute(
            "UPDATE peers SET nickname = ? WHERE peer_id = ?",
            (nickname, peer_id),
        )
        await self._db.commit()

    async def set_peer_archived(self, peer_id: bytes, archived: bool) -> None:
        await self._db.execute(
            "UPDATE peers SET archived = ? WHERE peer_id = ?",
            (1 if archived else 0, peer_id),
        )
        await self._db.commit()

    async def remove_peer(self, peer_id: bytes) -> None:
        await self._db.execute("DELETE FROM sessions WHERE peer_id = ?", (peer_id,))
        await self._db.execute("DELETE FROM message_queue WHERE receiver = ?", (peer_id,))
        await self._db.execute("DELETE FROM peers WHERE peer_id = ?", (peer_id,))
        await self._db.commit()

    # -- Queue operations ----------------------------------------------------

    async def enqueue_message(
        self, message_id: str, receiver: bytes, created_at: float
    ) -> None:
        await self._db.execute(
            "INSERT INTO message_queue (message_id, receiver, status, retry_count, created_at, updated_at) "
            "VALUES (?, ?, 'queued', 0, ?, ?)",
            (message_id, receiver, created_at, created_at),
        )
        await self._db.commit()

    async def update_message_status(self, message_id: str, status: str) -> None:
        await self._db.execute(
            "UPDATE message_queue SET status = ?, updated_at = ? WHERE message_id = ?",
            (status, time.time(), message_id),
        )
        await self._db.commit()

    async def get_queued_messages(self, receiver: bytes) -> list[QueueRecord]:
        async with self._db.execute(
            "SELECT * FROM message_queue WHERE receiver = ? AND status = 'queued' "
            "ORDER BY created_at",
            (receiver,),
        ) as cur:
            return [_row_to_queue(r) async for r in cur]

    async def increment_retry(self, message_id: str) -> int:
        await self._db.execute(
            "UPDATE message_queue SET retry_count = retry_count + 1, updated_at = ? "
            "WHERE message_id = ?",
            (time.time(), message_id),
        )
        await self._db.commit()
        async with self._db.execute(
            "SELECT retry_count FROM message_queue WHERE message_id = ?",
            (message_id,),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

    # -- Session persistence -------------------------------------------------

    async def save_session_state(
        self,
        session_id: bytes,
        peer_id: bytes,
        send_seq: int,
        recv_seq: int,
    ) -> None:
        await self._db.execute(
            "INSERT OR REPLACE INTO sessions "
            "(session_id, peer_id, send_sequence, recv_sequence, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, peer_id, send_seq, recv_seq, time.time()),
        )
        await self._db.commit()

    async def load_session_state(self, session_id: bytes) -> SessionRecord | None:
        async with self._db.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ) as cur:
            row = await cur.fetchone()
            return _row_to_session(row) if row else None

    async def update_send_sequence(self, session_id: bytes, seq: int) -> None:
        await self._db.execute(
            "UPDATE sessions SET send_sequence = ? WHERE session_id = ?",
            (seq, session_id),
        )
        await self._db.commit()

    async def update_recv_sequence(self, session_id: bytes, seq: int) -> None:
        await self._db.execute(
            "UPDATE sessions SET recv_sequence = ? WHERE session_id = ?",
            (seq, session_id),
        )
        await self._db.commit()


# ---------------------------------------------------------------------------
# Message log (encrypted, append-only, per-peer)
# ---------------------------------------------------------------------------

class MessageLog:
    """Encrypted message log files, one per peer."""

    def __init__(
        self, messages_dir: Path, log_limit_mb: int = 100, max_rotations: int = 3
    ) -> None:
        self._dir = messages_dir
        self._limit_bytes = log_limit_mb * 1024 * 1024
        self._max_rotations = max_rotations
        self._dir.mkdir(parents=True, exist_ok=True)

    def _log_path(self, peer_id: bytes) -> Path:
        return self._dir / f"{peer_id.hex()}.log"

    async def append_message(self, peer_id: bytes, encrypted_blob: bytes) -> None:
        """Crash-safe append: write to tmp, fsync, then rename/append."""
        log_path = self._log_path(peer_id)
        tmp_path = log_path.with_suffix(".log.tmp")

        # Build line: length-prefixed blob + newline
        import struct

        line = struct.pack(">I", len(encrypted_blob)) + encrypted_blob + b"\n"

        # Write to temp
        with open(tmp_path, "ab") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())

        # Append temp contents to main log
        with open(log_path, "ab") as main:
            with open(tmp_path, "rb") as tmp:
                main.write(tmp.read())
            main.flush()
            os.fsync(main.fileno())

        # Remove temp
        tmp_path.unlink(missing_ok=True)

        # Check rotation
        await self.rotate_if_needed(peer_id)

    async def read_messages(self, peer_id: bytes) -> list[bytes]:
        """Read all encrypted blobs for a peer."""
        import struct

        log_path = self._log_path(peer_id)
        if not log_path.exists():
            return []

        blobs: list[bytes] = []
        with open(log_path, "rb") as f:
            data = f.read()

        pos = 0
        while pos + 4 <= len(data):
            length = struct.unpack(">I", data[pos : pos + 4])[0]
            pos += 4
            if pos + length > len(data):
                break
            blobs.append(data[pos : pos + length])
            pos += length + 1  # +1 for newline

        return blobs

    async def rotate_if_needed(self, peer_id: bytes) -> None:
        """Rotate if file > limit."""
        log_path = self._log_path(peer_id)
        if not log_path.exists():
            return
        if log_path.stat().st_size <= self._limit_bytes:
            return

        # Rotate: .log → .log.1, .log.1 → .log.2, etc.
        for i in range(self._max_rotations, 0, -1):
            src = log_path.with_suffix(f".log.{i}") if i > 0 else log_path
            if i == self._max_rotations:
                old = log_path.with_suffix(f".log.{i}")
                old.unlink(missing_ok=True)
            if i > 1:
                prev = log_path.with_suffix(f".log.{i - 1}")
                if prev.exists():
                    prev.rename(log_path.with_suffix(f".log.{i}"))

        if log_path.exists():
            log_path.rename(log_path.with_suffix(".log.1"))


# ---------------------------------------------------------------------------
# Row conversions
# ---------------------------------------------------------------------------

def _row_to_peer(row: Any) -> PeerRecord:
    return PeerRecord(
        peer_id=row[0],
        public_key=row[1],
        last_known_endpoint=row[2],
        version_counter=row[3],
        last_seen=row[4],
        nickname=row[5],
        archived=bool(row[6]) if len(row) > 6 else False,
    )


def _row_to_queue(row: Any) -> QueueRecord:
    return QueueRecord(
        message_id=row[0],
        receiver=row[1],
        status=row[2],
        retry_count=row[3],
        created_at=row[4],
        updated_at=row[5],
    )


def _row_to_session(row: Any) -> SessionRecord:
    return SessionRecord(
        session_id=row[0],
        peer_id=row[1],
        send_sequence=row[2],
        recv_sequence=row[3],
        created_at=row[4],
    )
