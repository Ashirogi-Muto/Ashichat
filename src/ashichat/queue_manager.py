"""Message queue manager for AshiChat — DATA packet send/recv + ACK tracking."""

from __future__ import annotations

import asyncio
import struct
import time
import uuid
from enum import Enum

from ashichat.crypto import build_nonce, decrypt, encrypt
from ashichat.logging_setup import get_logger
from ashichat.packet import AckPayload, DataPayload, PacketType, make_packet
from ashichat.session import Session

log = get_logger(__name__)

MAX_RETRIES = 5
ACK_TIMEOUT = 30.0  # seconds


class MessageStatus(Enum):
    QUEUED = "queued"
    PENDING = "pending"
    DELIVERED = "delivered"
    ACKNOWLEDGED = "acknowledged"
    FAILED = "failed"


class QueueManager:
    """Manages outgoing message queue and incoming DATA processing."""

    def __init__(self) -> None:
        # message_id → {receiver, status, retry_count, ciphertext, ...}
        self._queue: dict[str, dict] = {}

    def enqueue(self, receiver_peer_id: bytes, plaintext: bytes) -> str:
        """Queue a message for sending.  Returns message_id."""
        msg_id = str(uuid.uuid4())
        self._queue[msg_id] = {
            "receiver": receiver_peer_id,
            "status": MessageStatus.QUEUED,
            "retry_count": 0,
            "plaintext": plaintext,
            "created_at": time.time(),
        }
        return msg_id

    def restore_entry(
        self, message_id: str, receiver: bytes, status: str,
        retry_count: int, plaintext: bytes = b"",
        sequence_number: int | None = None,
    ) -> None:
        """Re-register a queue entry from SQLite on startup.

        Plaintext is now persisted in SQLite, so restored entries can be
        re-encrypted and sent normally via ``build_data_packet``.
        """
        try:
            msg_status = MessageStatus(status)
        except ValueError:
            msg_status = MessageStatus.QUEUED
        self._queue[message_id] = {
            "receiver": receiver,
            "status": msg_status,
            "retry_count": retry_count,
            "plaintext": plaintext,
            "created_at": time.time(),
            "sequence_number": sequence_number,
        }

    def build_data_packet(self, message_id: str, session: Session):
        """Build an encrypted DATA packet for a queued message.

        Returns ``(Packet, sequence_number)`` or ``None`` if not found/not queued.
        """
        entry = self._queue.get(message_id)
        if entry is None or entry["status"] != MessageStatus.QUEUED:
            return None

        seq = session.next_sequence()
        nonce = build_nonce(session.session_id, seq)
        # AAD = packet_type || sequence_number
        aad = struct.pack(">BI", PacketType.DATA, seq)

        ct, tag = encrypt(session.encryption_key, entry["plaintext"], nonce, aad)

        payload = DataPayload(
            session_id=session.session_id,
            sequence_number=seq,
            ciphertext=ct,
            auth_tag=tag,
        )

        entry["status"] = MessageStatus.PENDING
        entry["sequence_number"] = seq
        entry["sent_at"] = time.time()

        return make_packet(PacketType.DATA, payload), seq

    def rebuild_for_sync(self, message_id: str, session: Session):
        """Re-encrypt an already-dispatched message for SYNC retransmission.

        Uses the message's *original* sequence number instead of generating a new one.
        """
        entry = self._queue.get(message_id)
        if entry is None or entry.get("sequence_number") is None:
            return None

        if not entry.get("plaintext"):
            return None

        seq = entry["sequence_number"]
        nonce = build_nonce(session.session_id, seq)
        # AAD = packet_type || sequence_number
        aad = struct.pack(">BI", PacketType.DATA, seq)

        ct, tag = encrypt(session.encryption_key, entry["plaintext"], nonce, aad)

        payload = DataPayload(
            session_id=session.session_id,
            sequence_number=seq,
            ciphertext=ct,
            auth_tag=tag,
        )

        entry["sent_at"] = time.time()
        return make_packet(PacketType.DATA, payload)

    def handle_ack(self, ack: AckPayload) -> str | None:
        """Mark matching message as DELIVERED. Returns message_id or None."""
        for msg_id, entry in self._queue.items():
            if (
                entry["status"] == MessageStatus.PENDING
                and entry.get("sequence_number") == ack.ack_sequence_number
            ):
                entry["status"] = MessageStatus.DELIVERED
                return msg_id
        return None

    def handle_timeout(self, message_id: str) -> MessageStatus:
        """Handle ACK timeout — revert to QUEUED or mark FAILED."""
        entry = self._queue.get(message_id)
        if entry is None:
            return MessageStatus.FAILED

        entry["retry_count"] += 1
        if entry["retry_count"] >= MAX_RETRIES:
            entry["status"] = MessageStatus.FAILED
            return MessageStatus.FAILED

        entry["status"] = MessageStatus.QUEUED
        return MessageStatus.QUEUED

    def get_queued_for_peer(self, peer_id: bytes) -> list[str]:
        """Return message IDs queued for a specific peer."""
        return [
            mid
            for mid, e in self._queue.items()
            if e["receiver"] == peer_id and e["status"] == MessageStatus.QUEUED
        ]

    def get_status(self, message_id: str) -> MessageStatus | None:
        entry = self._queue.get(message_id)
        return entry["status"] if entry else None

    def acknowledge_by_sequence(
        self, peer_id: bytes, sequence_number: int,
    ) -> str | None:
        """Transition a DELIVERED message to ACKNOWLEDGED by sequence number.

        Returns the message ID if found, else ``None``.
        """
        for msg_id, entry in self._queue.items():
            if (
                entry["receiver"] == peer_id
                and entry.get("sequence_number") == sequence_number
                and entry["status"] == MessageStatus.DELIVERED
            ):
                entry["status"] = MessageStatus.ACKNOWLEDGED
                return msg_id
        return None

    def undelivered_count(self) -> int:
        """Count messages that have not reached DELIVERED/ACKNOWLEDGED."""
        return sum(
            1
            for entry in self._queue.values()
            if entry["status"] in {MessageStatus.QUEUED, MessageStatus.PENDING}
        )

    def get_timed_out_messages(self) -> list[str]:
        """Return message IDs stuck in PENDING beyond ACK_TIMEOUT."""
        now = time.time()
        return [
            mid
            for mid, e in self._queue.items()
            if e["status"] == MessageStatus.PENDING
            and now - e.get("sent_at", now) > ACK_TIMEOUT
        ]

    # -- Retry loop lifecycle ------------------------------------------------

    async def start_retry_loop(
        self,
        resend_fn=None,   # async fn(msg_id, peer_id) -> None
        on_failed_fn=None,  # async fn(msg_id, peer_id) -> None
    ) -> None:
        """Start the background ACK-timeout retry loop."""
        self._resend_fn = resend_fn
        self._on_failed_fn = on_failed_fn
        self._retry_running = True
        self._retry_task = asyncio.create_task(self._retry_loop())

    async def stop_retry_loop(self) -> None:
        self._retry_running = False
        task = getattr(self, "_retry_task", None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _retry_loop(self) -> None:
        """Check every 5s for PENDING messages that have timed out."""
        try:
            while getattr(self, "_retry_running", False):
                timed_out = self.get_timed_out_messages()
                for msg_id in timed_out:
                    status = self.handle_timeout(msg_id)
                    entry = self._queue.get(msg_id)
                    if entry is None:
                        continue
                    peer_id = entry["receiver"]
                    if status == MessageStatus.FAILED:
                        log.warning(
                            "Message %s to %s failed after %d retries",
                            msg_id[:8], peer_id.hex()[:8], MAX_RETRIES,
                        )
                        if self._on_failed_fn:
                            try:
                                await self._on_failed_fn(msg_id, peer_id)
                            except Exception:
                                log.exception("on_failed callback error")
                    elif status == MessageStatus.QUEUED:
                        log.info(
                            "Message %s to %s timed out, retry %d/%d",
                            msg_id[:8], peer_id.hex()[:8],
                            entry.get("retry_count", 0), MAX_RETRIES,
                        )
                        if self._resend_fn:
                            try:
                                await self._resend_fn(msg_id, peer_id)
                            except Exception:
                                log.exception("resend callback error")

                await asyncio.sleep(5)
        except asyncio.CancelledError:
            pass

    @staticmethod
    def decrypt_data_packet(
        data_payload: DataPayload, session: Session
    ) -> bytes | None:
        """Decrypt an incoming DATA packet.

        Returns plaintext on success, ``None`` on replay or decrypt failure.

        SAFETY: The recv_sequence is only advanced **after** successful
        decryption to prevent a forged high-sequence packet from poisoning
        the session state.
        """
        # 1. Replay check (read-only — does NOT advance counter)
        if not session.check_recv_sequence(data_payload.sequence_number):
            log.warning(
                "Replay rejected: seq %d <= %d",
                data_payload.sequence_number,
                session.recv_sequence,
            )
            return None

        # 2. Decrypt
        nonce = build_nonce(session.session_id, data_payload.sequence_number)
        aad = struct.pack(">BI", PacketType.DATA, data_payload.sequence_number)

        try:
            plaintext = decrypt(
                session.encryption_key,
                data_payload.ciphertext,
                nonce,
                aad,
                data_payload.auth_tag,
            )
        except Exception:
            log.warning("Decrypt failed for seq %d", data_payload.sequence_number)
            return None

        # 3. Advance recv_sequence ONLY after successful decryption
        session.advance_recv_sequence(data_payload.sequence_number)
        return plaintext

    @staticmethod
    def build_ack(session_id: bytes, sequence_number: int):
        """Build an ACK packet for a received DATA."""
        payload = AckPayload(
            session_id=session_id,
            ack_sequence_number=sequence_number,
        )
        return make_packet(PacketType.ACK, payload)
