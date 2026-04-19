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
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

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
    endpoint_signature: bytes | None = None
    archived: bool = False


@dataclass
class QueueRecord:
    message_id: str
    receiver: bytes
    status: str
    retry_count: int
    created_at: float
    updated_at: float
    plaintext: bytes = b""
    sequence_number: int | None = None


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
    archived INTEGER DEFAULT 0,
    endpoint_signature BLOB
);

CREATE TABLE IF NOT EXISTS message_queue (
    message_id TEXT PRIMARY KEY,
    receiver BLOB NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    retry_count INTEGER DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    plaintext BLOB NOT NULL DEFAULT x'',
    sequence_number INTEGER,
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

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value INTEGER NOT NULL
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
        if "endpoint_signature" not in cols:
            await self._db.execute(
                "ALTER TABLE peers ADD COLUMN endpoint_signature BLOB"
            )
        # Migration: add plaintext column to message_queue if missing
        async with self._db.execute("PRAGMA table_info(message_queue)") as cur2:
            mq_cols = [row[1] async for row in cur2]
        if "plaintext" not in mq_cols:
            await self._db.execute(
                "ALTER TABLE message_queue ADD COLUMN plaintext BLOB NOT NULL DEFAULT x''"
            )
        if "sequence_number" not in mq_cols:
            await self._db.execute(
                "ALTER TABLE message_queue ADD COLUMN sequence_number INTEGER"
            )
        await self._db.commit()
        log.info("Storage initialized at %s", db_path)

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    # -- Peer operations -----------------------------------------------------

    async def add_peer(
        self,
        peer_id: bytes,
        public_key: bytes,
        nickname: str | None = None,
        endpoint: tuple[str, int] | str | None = None,
    ) -> None:
        endpoint_text = _format_endpoint(endpoint)
        await self._db.execute(
            "INSERT INTO peers (peer_id, public_key, last_known_endpoint, nickname, version_counter, last_seen, archived) "
            "VALUES (?, ?, ?, ?, 0, ?, 0) "
            "ON CONFLICT(peer_id) DO UPDATE SET "
            "public_key = excluded.public_key, "
            "last_known_endpoint = COALESCE(excluded.last_known_endpoint, peers.last_known_endpoint), "
            "nickname = COALESCE(excluded.nickname, peers.nickname), "
            "last_seen = excluded.last_seen",
            (peer_id, public_key, endpoint_text, nickname, time.time()),
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
        self,
        peer_id: bytes,
        endpoint: str,
        version_counter: int,
        signature: bytes | None = None,
    ) -> bool:
        """Update endpoint only if version_counter > stored version."""
        async with self._db.execute(
            "SELECT version_counter FROM peers WHERE peer_id = ?", (peer_id,)
        ) as cur:
            row = await cur.fetchone()
            if row and row[0] >= version_counter:
                return False  # stale

        await self._db.execute(
            "UPDATE peers SET last_known_endpoint = ?, version_counter = ?, last_seen = ?, "
            "endpoint_signature = COALESCE(?, endpoint_signature) "
            "WHERE peer_id = ?",
            (endpoint, version_counter, time.time(), signature, peer_id),
        )
        await self._db.commit()
        return True

    async def get_local_endpoint_version(self) -> int:
        async with self._db.execute(
            "SELECT value FROM meta WHERE key = 'local_endpoint_version'"
        ) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else 0

    async def set_local_endpoint_version(self, version: int) -> None:
        await self._db.execute(
            "INSERT INTO meta (key, value) VALUES ('local_endpoint_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (version,),
        )
        await self._db.commit()

    async def remember_endpoint(
        self,
        peer_id: bytes,
        endpoint: tuple[str, int] | str,
    ) -> None:
        """Persist the latest authenticated endpoint observation."""
        await self._db.execute(
            "UPDATE peers SET last_known_endpoint = ?, last_seen = ? WHERE peer_id = ?",
            (_format_endpoint(endpoint), time.time(), peer_id),
        )
        await self._db.commit()

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
        self, message_id: str, receiver: bytes, created_at: float,
        plaintext: bytes = b"",
    ) -> None:
        await self._db.execute(
            "INSERT INTO message_queue "
            "(message_id, receiver, status, retry_count, created_at, updated_at, plaintext, sequence_number) "
            "VALUES (?, ?, 'queued', 0, ?, ?, ?, NULL)",
            (message_id, receiver, created_at, created_at, plaintext),
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

    async def load_all_queued_messages(self) -> list[QueueRecord]:
        """Load message records needed for retry and sync after restart."""
        async with self._db.execute(
            "SELECT * FROM message_queue WHERE status IN ('queued', 'pending', 'delivered', 'acknowledged') "
            "ORDER BY created_at",
        ) as cur:
            return [_row_to_queue(r) async for r in cur]

    async def update_message_sequence(
        self,
        message_id: str,
        sequence_number: int,
    ) -> None:
        await self._db.execute(
            "UPDATE message_queue SET sequence_number = ?, updated_at = ? WHERE message_id = ?",
            (sequence_number, time.time(), message_id),
        )
        await self._db.commit()

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

    async def clear_message_plaintext(self, message_id: str) -> None:
        await self._db.execute(
            "UPDATE message_queue SET plaintext = x'' WHERE message_id = ?",
            (message_id,),
        )
        await self._db.commit()

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

    async def load_all_sessions(self) -> list[SessionRecord]:
        """Load all persisted session records (for startup sequence restore)."""
        async with self._db.execute("SELECT * FROM sessions") as cur:
            return [_row_to_session(r) async for r in cur]


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
        """Crash-safe append using atomic write process.

        Follows the spec's required sequence:
        1. Build the entry line.
        2. Write to temp file.
        3. ``fsync`` the temp file.
        4. Atomic ``os.rename()`` temp → main log.

        Because the log is append-only and we cannot ``rename`` over an
        existing file without losing prior entries, we read the current log
        content, append the new entry, write the full result to a temp file,
        fsync, and then atomically replace the main log via ``os.rename``.
        This ensures the main log is never left in a half-written state.
        """
        log_path = self._log_path(peer_id)
        tmp_path = log_path.with_suffix(".log.tmp")

        # Build length-prefixed line
        import struct

        line = struct.pack(">I", len(encrypted_blob)) + encrypted_blob + b"\n"

        # Read existing content (empty if file doesn't exist yet)
        existing = b""
        if log_path.exists():
            with open(log_path, "rb") as f:
                existing = f.read()

        # Write existing + new entry to temp
        fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, existing + line)
            os.fsync(fd)
        finally:
            os.close(fd)

        # Atomic rename: temp → main log
        os.rename(str(tmp_path), str(log_path))

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


class OutboxStore:
    """Encrypted outbound message spool.

    Keeps message bodies out of SQLite while preserving restart-safe retries.
    Each message is stored in its own encrypted file addressed by message_id.
    """

    def __init__(self, outbox_dir: Path, key: bytes) -> None:
        self._dir = outbox_dir
        self._key = key
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, message_id: str) -> Path:
        return self._dir / f"{message_id}.bin"

    async def store_message(self, message_id: str, plaintext: bytes) -> None:
        nonce = os.urandom(12)
        blob = nonce + AESGCM(self._key).encrypt(nonce, plaintext, message_id.encode("utf-8"))
        tmp_path = self._path(message_id).with_suffix(".bin.tmp")
        final_path = self._path(message_id)

        fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, blob)
            os.fsync(fd)
        finally:
            os.close(fd)

        os.replace(str(tmp_path), str(final_path))

    async def load_message(self, message_id: str) -> bytes | None:
        path = self._path(message_id)
        if not path.exists():
            return None

        blob = path.read_bytes()
        if len(blob) < 12:
            return None
        nonce = blob[:12]
        ciphertext = blob[12:]
        try:
            return AESGCM(self._key).decrypt(
                nonce,
                ciphertext,
                message_id.encode("utf-8"),
            )
        except Exception:
            return None

    async def delete_message(self, message_id: str) -> None:
        self._path(message_id).unlink(missing_ok=True)


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
        endpoint_signature=row[7] if len(row) > 7 and row[7] else None,
        archived=bool(row[6]) if len(row) > 6 else False,
    )


def _format_endpoint(endpoint: tuple[str, int] | str | None) -> str | None:
    if endpoint is None:
        return None
    if isinstance(endpoint, str):
        return endpoint
    host, port = endpoint
    return f"{host}:{port}"


def _row_to_queue(row: Any) -> QueueRecord:
    return QueueRecord(
        message_id=row[0],
        receiver=row[1],
        status=row[2],
        retry_count=row[3],
        created_at=row[4],
        updated_at=row[5],
        plaintext=row[6] if len(row) > 6 and row[6] else b"",
        sequence_number=row[7] if len(row) > 7 and row[7] is not None else None,
    )


def _row_to_session(row: Any) -> SessionRecord:
    return SessionRecord(
        session_id=row[0],
        peer_id=row[1],
        send_sequence=row[2],
        recv_sequence=row[3],
        created_at=row[4],
    )
