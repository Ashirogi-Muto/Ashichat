"""Message queue manager for AshiChat — DATA packet send/recv + ACK tracking."""

from __future__ import annotations

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

        return make_packet(PacketType.DATA, payload), seq

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

    def undelivered_count(self) -> int:
        """Count messages that have not reached DELIVERED/ACKNOWLEDGED."""
        return sum(
            1
            for entry in self._queue.values()
            if entry["status"] in {MessageStatus.QUEUED, MessageStatus.PENDING}
        )

    @staticmethod
    def decrypt_data_packet(
        data_payload: DataPayload, session: Session
    ) -> bytes | None:
        """Decrypt an incoming DATA packet.

        Returns plaintext on success, ``None`` on replay or decrypt failure.
        """
        # 1. Replay check
        if not session.validate_recv_sequence(data_payload.sequence_number):
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
            return decrypt(
                session.encryption_key,
                data_payload.ciphertext,
                nonce,
                aad,
                data_payload.auth_tag,
            )
        except Exception:
            log.warning("Decrypt failed for seq %d", data_payload.sequence_number)
            return None

    @staticmethod
    def build_ack(session_id: bytes, sequence_number: int):
        """Build an ACK packet for a received DATA."""
        payload = AckPayload(
            session_id=session_id,
            ack_sequence_number=sequence_number,
        )
        return make_packet(PacketType.ACK, payload)
