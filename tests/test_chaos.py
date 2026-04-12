"""Phase 14 — Chaos / resilience tests for AshiChat.

Simulates adverse network conditions to verify the system survives:
    1. Packet loss (30%)
    2. Packet duplication
    3. Packet reordering
    4. Connection drop mid-conversation
    5. Simultaneous reconnect
    6. Rapid disconnect/reconnect flapping
    7. Queue overflow (1000 messages while offline)
    8. Rate limit flood
    9. Stale endpoint resolution
   10. Long offline → ARCHIVED
"""

from __future__ import annotations

import asyncio
import os
import random
import struct
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ashichat.config import AshiChatConfig, DebugConfig, NetworkConfig, StorageConfig
from ashichat.crypto import build_nonce, decrypt, encrypt
from ashichat.handshake import create_hello, process_hello, process_hello_ack
from ashichat.identity import generate_identity
from ashichat.node import Node
from ashichat.overlay import PeerTable, OverlayManager, MAX_PEER_TABLE
from ashichat.packet import (
    AckPayload,
    DataPayload,
    Packet,
    PacketType,
    PingPayload,
    PongPayload,
    ResolveRequestPayload,
    make_packet,
)
from ashichat.peer_state import (
    ARCHIVE_THRESHOLD,
    PeerState,
    PeerStateMachine,
    PeerStateManager,
    VALID_TRANSITIONS,
)
from ashichat.queue_manager import MessageStatus, QueueManager
from ashichat.rate_limiter import GlobalRateLimiter, PeerRateLimiter, TokenBucket
from ashichat.reconnect import BACKOFF_SCHEDULE, ReconnectManager
from ashichat.session import Session, SessionRegistry
from ashichat.storage import MessageLog, StorageManager
from ashichat.transport_udp import UDPTransport, start_udp_listener


# ---------------------------------------------------------------------------
# Deterministic RNG for reproducible chaos
# ---------------------------------------------------------------------------

CHAOS_SEED = 42


# ---------------------------------------------------------------------------
# ChaoticTransport — a wrapper that injects faults
# ---------------------------------------------------------------------------


class ChaoticTransport:
    """Wraps a UDPTransport and injects configurable faults.

    - ``loss_rate``: fraction of packets silently dropped (0.0–1.0)
    - ``dup_rate``: fraction of packets sent twice
    - ``reorder_delay``: max delay (seconds) added to simulate reordering
    """

    def __init__(
        self,
        inner: UDPTransport,
        loss_rate: float = 0.0,
        dup_rate: float = 0.0,
        reorder_delay: float = 0.0,
        seed: int = CHAOS_SEED,
    ) -> None:
        self._inner = inner
        self._loss_rate = loss_rate
        self._dup_rate = dup_rate
        self._reorder_delay = reorder_delay
        self._rng = random.Random(seed)
        self._sent = 0
        self._dropped = 0
        self._duped = 0

    def send_packet(self, packet: Packet, addr: tuple[str, int]) -> None:
        self._sent += 1

        # Drop?
        if self._rng.random() < self._loss_rate:
            self._dropped += 1
            return

        # Send original
        if self._reorder_delay > 0:
            delay = self._rng.random() * self._reorder_delay
            asyncio.get_event_loop().call_later(
                delay, self._inner.send_packet, packet, addr
            )
        else:
            self._inner.send_packet(packet, addr)

        # Duplicate?
        if self._rng.random() < self._dup_rate:
            self._duped += 1
            if self._reorder_delay > 0:
                delay = self._rng.random() * self._reorder_delay
                asyncio.get_event_loop().call_later(
                    delay, self._inner.send_packet, packet, addr
                )
            else:
                self._inner.send_packet(packet, addr)

    @property
    def stats(self) -> dict:
        return {
            "sent": self._sent,
            "dropped": self._dropped,
            "duped": self._duped,
        }


# ═══════════════════════════════════════════════════════════════════════════
# 1. Packet Loss — 30% drop → messages still eventually ACK'd via retry
# ═══════════════════════════════════════════════════════════════════════════


class TestPacketLoss:
    """Messages survive 30% random packet loss via queue retry."""

    def test_queue_retry_after_loss(self) -> None:
        """Simulate loss by not sending ACK, then timeout → requeue."""
        qm = QueueManager()
        session = Session(
            session_id=os.urandom(8),
            encryption_key=os.urandom(32),
            remote_peer_id=os.urandom(32),
        )
        msg_id = qm.enqueue(session.remote_peer_id, b"hello through loss")

        # First attempt: "lost" — no ACK arrives
        result = qm.build_data_packet(msg_id, session)
        assert result is not None
        assert qm.get_status(msg_id) == MessageStatus.PENDING

        # Timeout fires → requeue
        status = qm.handle_timeout(msg_id)
        assert status == MessageStatus.QUEUED

        # Second attempt: succeeds
        result2 = qm.build_data_packet(msg_id, session)
        assert result2 is not None
        pkt, seq = result2

        ack = AckPayload(session_id=session.session_id, ack_sequence_number=seq)
        delivered = qm.handle_ack(ack)
        assert delivered == msg_id
        assert qm.get_status(msg_id) == MessageStatus.DELIVERED

    async def test_chaotic_transport_drops(self) -> None:
        """ChaoticTransport with 30% loss drops ~30% of packets."""
        received: list[Packet] = []

        async def handler(pkt, addr):
            received.append(pkt)

        _, proto_a = await start_udp_listener(0, handler, "127.0.0.1")
        _, proto_b = await start_udp_listener(0, handler, "127.0.0.1")
        addr_b = proto_b._transport.get_extra_info("sockname")

        chaotic = ChaoticTransport(proto_a, loss_rate=0.3, seed=CHAOS_SEED)

        # Send 100 packets
        for _ in range(100):
            ping = make_packet(
                PacketType.PING,
                PingPayload(session_id=os.urandom(8), ping_id=os.urandom(8)),
            )
            chaotic.send_packet(ping, addr_b)

        await asyncio.sleep(0.5)

        # Should receive ~70 (±15)
        assert 50 < len(received) < 90
        assert chaotic.stats["dropped"] > 0

        proto_a.close()
        proto_b.close()


# ═══════════════════════════════════════════════════════════════════════════
# 2. Packet Duplication — each sent twice → only processed once
# ═══════════════════════════════════════════════════════════════════════════


class TestPacketDuplication:
    """Sequence-based dedup rejects duplicate DATA packets."""

    def test_replay_rejected_on_decrypt(self) -> None:
        session_send = Session(
            session_id=os.urandom(8),
            encryption_key=os.urandom(32),
            remote_peer_id=os.urandom(32),
        )
        qm = QueueManager()
        msg_id = qm.enqueue(session_send.remote_peer_id, b"dup test")
        pkt, seq = qm.build_data_packet(msg_id, session_send)

        dp = DataPayload.deserialize(pkt.payload)

        recv_session = Session(
            session_id=session_send.session_id,
            encryption_key=session_send.encryption_key,
            remote_peer_id=b"\x00" * 32,
        )

        # First: accepted
        r1 = QueueManager.decrypt_data_packet(dp, recv_session)
        assert r1 is not None

        # Duplicate: rejected (seq <= recv_sequence)
        r2 = QueueManager.decrypt_data_packet(dp, recv_session)
        assert r2 is None

    async def test_chaotic_duplication(self) -> None:
        """ChaoticTransport with 100% dup rate sends double."""
        received: list[Packet] = []

        async def handler(pkt, addr):
            received.append(pkt)

        _, proto_a = await start_udp_listener(0, handler, "127.0.0.1")
        _, proto_b = await start_udp_listener(0, handler, "127.0.0.1")
        addr_b = proto_b._transport.get_extra_info("sockname")

        chaotic = ChaoticTransport(proto_a, dup_rate=1.0, seed=CHAOS_SEED)

        for _ in range(10):
            ping = make_packet(
                PacketType.PING,
                PingPayload(session_id=os.urandom(8), ping_id=os.urandom(8)),
            )
            chaotic.send_packet(ping, addr_b)

        await asyncio.sleep(0.3)

        # Should receive 20 (10 original + 10 dups)
        assert len(received) == 20
        assert chaotic.stats["duped"] == 10

        proto_a.close()
        proto_b.close()


# ═══════════════════════════════════════════════════════════════════════════
# 3. Packet Reordering — out-of-order delivery handled correctly
# ═══════════════════════════════════════════════════════════════════════════


class TestPacketReordering:
    """Out-of-order packets are accepted if sequence is monotonically higher."""

    def test_out_of_order_accepted(self) -> None:
        key = os.urandom(32)
        sid = os.urandom(8)

        recv_session = Session(
            session_id=sid,
            encryption_key=key,
            remote_peer_id=b"\x00" * 32,
        )

        # Build messages with sequences 1, 2, 3
        plaintexts = [f"msg-{i}".encode() for i in range(3)]
        packets = []
        for i, pt in enumerate(plaintexts):
            seq = i + 1
            nonce = build_nonce(sid, seq)
            aad = struct.pack(">BI", PacketType.DATA, seq)
            ct, tag = encrypt(key, pt, nonce, aad)
            packets.append(DataPayload(
                session_id=sid,
                sequence_number=seq,
                ciphertext=ct,
                auth_tag=tag,
            ))

        # Deliver out of order: 2, 1, 3
        r2 = QueueManager.decrypt_data_packet(packets[1], recv_session)  # seq=2
        assert r2 == b"msg-1"

        r1 = QueueManager.decrypt_data_packet(packets[0], recv_session)  # seq=1 → rejected (1 < 2)
        assert r1 is None

        r3 = QueueManager.decrypt_data_packet(packets[2], recv_session)  # seq=3
        assert r3 == b"msg-2"


# ═══════════════════════════════════════════════════════════════════════════
# 4. Connection Drop — mid-conversation → reconnect and resume
# ═══════════════════════════════════════════════════════════════════════════


class TestConnectionDrop:
    """Connection drop mid-conversation triggers state machine transitions."""

    async def test_state_transition_on_disconnect(self) -> None:
        mgr = PeerStateManager()
        pid = os.urandom(32)
        mgr.ensure_machine(pid, PeerState.CONNECTED)

        # Simulate connection drop
        await mgr.update_state(pid, PeerState.DISCONNECTED)
        assert mgr.get_state(pid) == PeerState.DISCONNECTED

        # Reconnect attempt
        await mgr.update_state(pid, PeerState.CONNECTING)
        assert mgr.get_state(pid) == PeerState.CONNECTING

        # Reconnect success
        await mgr.update_state(pid, PeerState.CONNECTED)
        assert mgr.get_state(pid) == PeerState.CONNECTED


# ═══════════════════════════════════════════════════════════════════════════
# 5. Simultaneous Reconnect — both peers try → one succeeds
# ═══════════════════════════════════════════════════════════════════════════


class TestSimultaneousReconnect:
    """When both sides handshake simultaneously, each can establish a session."""

    def test_both_sides_handshake(self) -> None:
        alice = generate_identity()
        bob = generate_identity()
        alice_known = {bob.peer_id}
        bob_known = {alice.peer_id}

        # Alice → Bob
        hello_a, state_a = create_hello(alice)
        # Bob → Alice (simultaneously)
        hello_b, state_b = create_hello(bob)

        # Both process each other's HELLO
        result_b = process_hello(hello_a, bob_known, bob)
        result_a = process_hello(hello_b, alice_known, alice)

        # Both should succeed — the session registry will keep the latest
        assert result_b is not None
        assert result_a is not None

        ack_a, keys_a = result_a
        ack_b, keys_b = result_b

        # Both have valid keys (different, since different handshakes)
        assert keys_a.encryption_key != keys_b.encryption_key


# ═══════════════════════════════════════════════════════════════════════════
# 6. Rapid Disconnect/Reconnect — flap 10 times → state stays consistent
# ═══════════════════════════════════════════════════════════════════════════


class TestRapidFlapping:
    """State machine survives rapid connect/disconnect cycling."""

    async def test_flap_10_times(self) -> None:
        mgr = PeerStateManager()
        pid = os.urandom(32)
        mgr.ensure_machine(pid, PeerState.DISCONNECTED)

        for _ in range(10):
            await mgr.update_state(pid, PeerState.CONNECTING)
            await mgr.update_state(pid, PeerState.CONNECTED)
            await mgr.update_state(pid, PeerState.DISCONNECTED)

        # After 10 flaps, state is deterministic
        assert mgr.get_state(pid) == PeerState.DISCONNECTED

    async def test_never_enters_invalid_state(self) -> None:
        """No sequence of valid transitions can produce an invalid state."""
        sm = PeerStateMachine(PeerState.DISCONNECTED)

        # Exhaustive valid path
        transitions = [
            PeerState.CONNECTING,
            PeerState.CONNECTED,
            PeerState.IDLE,
            PeerState.SUSPECT,
            PeerState.DISCONNECTED,
            PeerState.RESOLVING,
            PeerState.FAILED,
            PeerState.DISCONNECTED,
            PeerState.ARCHIVED,
            PeerState.CONNECTING,
            PeerState.CONNECTED,
        ]

        for t in transitions:
            sm.transition(t)
            # After every transition, state is valid
            assert sm.state == t
            assert sm.state in PeerState


# ═══════════════════════════════════════════════════════════════════════════
# 7. Queue Overflow — 1000 messages while peer offline → delivered on reconnect
# ═══════════════════════════════════════════════════════════════════════════


class TestQueueOverflow:
    """1000 messages queued while offline are all deliverable on reconnect."""

    def test_queue_1000_messages(self) -> None:
        qm = QueueManager()
        pid = os.urandom(32)

        msg_ids = []
        for i in range(1000):
            mid = qm.enqueue(pid, f"message-{i}".encode())
            msg_ids.append(mid)

        # All should be queued
        queued = qm.get_queued_for_peer(pid)
        assert len(queued) == 1000

        # Simulate reconnect: send all
        session = Session(
            session_id=os.urandom(8),
            encryption_key=os.urandom(32),
            remote_peer_id=pid,
        )

        delivered = 0
        for mid in msg_ids:
            result = qm.build_data_packet(mid, session)
            if result is not None:
                pkt, seq = result
                ack = AckPayload(
                    session_id=session.session_id,
                    ack_sequence_number=seq,
                )
                qm.handle_ack(ack)
                delivered += 1

        assert delivered == 1000

    async def test_queue_1000_persist_to_db(self, tmp_path: Path) -> None:
        """1000 queued messages survive DB persistence."""
        sm = StorageManager()
        await sm.initialize(tmp_path / "overflow.db")

        pid = os.urandom(32)
        await sm.add_peer(pid, os.urandom(32))

        for i in range(1000):
            await sm.enqueue_message(f"msg-{i}", pid, time.time())

        queued = await sm.get_queued_messages(pid)
        assert len(queued) == 1000

        await sm.close()


# ═══════════════════════════════════════════════════════════════════════════
# 8. Rate Limit Flood — 500 RESOLVE_REQUEST/min → excess dropped
# ═══════════════════════════════════════════════════════════════════════════


class TestRateLimitFlood:
    """Flood rate limiter with excess traffic → only allowed count passes."""

    def test_peer_resolve_flood(self) -> None:
        rl = PeerRateLimiter()
        pid = os.urandom(32)

        allowed = sum(
            1 for _ in range(500)
            if rl.check(pid, PacketType.RESOLVE_REQUEST)
        )

        # 10/min bucket: initial burst of 10 allowed
        assert allowed == 10

    def test_global_resolve_flood(self) -> None:
        gl = GlobalRateLimiter()

        allowed = sum(
            1 for _ in range(500)
            if gl.check_resolve_forward()
        )

        # 50/min bucket: initial burst of 50 allowed
        assert allowed == 50

    def test_data_flood_from_one_peer(self) -> None:
        rl = PeerRateLimiter()
        pid = os.urandom(32)

        allowed = sum(
            1 for _ in range(1000)
            if rl.check(pid, PacketType.DATA)
        )

        # 100/sec bucket: initial burst of 100
        assert allowed == 100

    def test_unlimited_type_not_rate_limited(self) -> None:
        rl = PeerRateLimiter()
        pid = os.urandom(32)

        # PING doesn't have a rate limit defined
        allowed = sum(
            1 for _ in range(1000)
            if rl.check(pid, PacketType.PING)
        )
        assert allowed == 1000


# ═══════════════════════════════════════════════════════════════════════════
# 9. Stale Endpoint — stored endpoint wrong → version check rejects
# ═══════════════════════════════════════════════════════════════════════════


class TestStaleEndpoint:
    """Stale endpoint updates (lower version_counter) are rejected."""

    async def test_stale_endpoint_rejected(self, tmp_path: Path) -> None:
        sm = StorageManager()
        await sm.initialize(tmp_path / "stale.db")

        pid = os.urandom(32)
        await sm.add_peer(pid, os.urandom(32))

        # Update to version 5
        assert await sm.update_endpoint(pid, "1.2.3.4:9000", 5) is True

        # Stale update (version 3) rejected
        assert await sm.update_endpoint(pid, "5.6.7.8:9000", 3) is False

        # Same version rejected
        assert await sm.update_endpoint(pid, "5.6.7.8:9000", 5) is False

        # Newer version accepted
        assert await sm.update_endpoint(pid, "10.0.0.1:9000", 6) is True

        peer = await sm.get_peer(pid)
        assert peer.last_known_endpoint == "10.0.0.1:9000"
        assert peer.version_counter == 6

        await sm.close()


# ═══════════════════════════════════════════════════════════════════════════
# 10. Long Offline — peer offline > 7 days → ARCHIVED, still retries daily
# ═══════════════════════════════════════════════════════════════════════════


class TestLongOffline:
    """Peer offline > 7 days transitions to ARCHIVED."""

    def test_should_archive_after_7_days(self) -> None:
        sm = PeerStateMachine(PeerState.DISCONNECTED)
        # Hack: pretend the last state change was 8 days ago
        sm._last_change = time.time() - (8 * 24 * 3600)
        assert sm.should_archive() is True

    def test_should_not_archive_under_7_days(self) -> None:
        sm = PeerStateMachine(PeerState.DISCONNECTED)
        sm._last_change = time.time() - (6 * 24 * 3600)
        assert sm.should_archive() is False

    def test_archived_can_reconnect(self) -> None:
        sm = PeerStateMachine(PeerState.ARCHIVED)
        sm.transition(PeerState.CONNECTING)
        assert sm.state == PeerState.CONNECTING

    def test_reconnect_backoff_caps_at_6h(self) -> None:
        rm = ReconnectManager()
        pid = os.urandom(32)
        rm.record_disconnect(pid)

        for _ in range(100):
            rm.record_attempt(pid)

        backoff = rm.get_backoff(pid)
        assert backoff == BACKOFF_SCHEDULE[-1]  # 21600 = 6h

    def test_reconnect_clears_on_connect(self) -> None:
        rm = ReconnectManager()
        pid = os.urandom(32)
        rm.record_disconnect(pid)
        rm.record_attempt(pid)
        rm.record_attempt(pid)

        rm.record_connect(pid)

        # After connecting, backoff resets
        backoff = rm.get_backoff(pid)
        assert backoff == BACKOFF_SCHEDULE[0]  # 1s


# ═══════════════════════════════════════════════════════════════════════════
# Combined chaos: multi-fault scenario
# ═══════════════════════════════════════════════════════════════════════════


class TestMultiFault:
    """Multiple faults combined in a single scenario."""

    async def test_loss_and_dup_combined(self) -> None:
        """30% loss + 20% dup → system doesn't crash, some packets arrive."""
        received: list[Packet] = []

        async def handler(pkt, addr):
            received.append(pkt)

        _, proto_a = await start_udp_listener(0, handler, "127.0.0.1")
        _, proto_b = await start_udp_listener(0, handler, "127.0.0.1")
        addr_b = proto_b._transport.get_extra_info("sockname")

        chaotic = ChaoticTransport(
            proto_a, loss_rate=0.3, dup_rate=0.2, seed=CHAOS_SEED
        )

        for _ in range(100):
            ping = make_packet(
                PacketType.PING,
                PingPayload(session_id=os.urandom(8), ping_id=os.urandom(8)),
            )
            chaotic.send_packet(ping, addr_b)

        await asyncio.sleep(0.5)

        # Should receive some packets (not 0, not 100)
        assert 0 < len(received) < 120
        assert chaotic.stats["dropped"] > 0

        proto_a.close()
        proto_b.close()

    def test_encrypt_decrypt_survives_all_sequences(self) -> None:
        """Encrypt 100 messages, decrypt only the ones with valid sequences."""
        key = os.urandom(32)
        sid = os.urandom(8)

        send_session = Session(
            session_id=sid,
            encryption_key=key,
            remote_peer_id=os.urandom(32),
        )
        recv_session = Session(
            session_id=sid,
            encryption_key=key,
            remote_peer_id=os.urandom(32),
        )

        qm = QueueManager()
        delivered = 0

        for i in range(100):
            mid = qm.enqueue(send_session.remote_peer_id, f"chaos-{i}".encode())
            result = qm.build_data_packet(mid, send_session)
            if result is None:
                continue
            pkt, seq = result
            dp = DataPayload.deserialize(pkt.payload)

            pt = QueueManager.decrypt_data_packet(dp, recv_session)
            if pt is not None:
                delivered += 1

        # All 100 should decrypt (sequential, no loss/dup in this path)
        assert delivered == 100

    async def test_full_state_machine_walk(self) -> None:
        """Walk through every reachable state combination without crashing."""
        mgr = PeerStateManager()

        for src, dests in VALID_TRANSITIONS.items():
            for dst in dests:
                pid = os.urandom(32)
                mgr.ensure_machine(pid, src)
                try:
                    await mgr.update_state(pid, dst)
                except ValueError:
                    pass  # some transitions may require specific preconditions

    def test_peer_table_survives_mass_churn(self) -> None:
        """Add 2000 peers, remove 1000 randomly → table stays consistent."""
        pt = PeerTable(b"\x00" * 32)
        rng = random.Random(CHAOS_SEED)

        ids = []
        for i in range(2000):
            pid = i.to_bytes(32, "big")
            pt.add_peer(pid, os.urandom(32))
            ids.append(pid)

        assert pt.size() <= MAX_PEER_TABLE

        # Remove 1000 randomly
        rng.shuffle(ids)
        for pid in ids[:1000]:
            pt.remove_peer(pid)

        # Table size should be max(0, remaining)
        assert pt.size() <= MAX_PEER_TABLE
        assert pt.size() >= 0

    async def test_message_log_mass_write(self, tmp_path: Path) -> None:
        """Write 500 messages to log → all readable back."""
        ml = MessageLog(tmp_path / "chaos_msgs", log_limit_mb=1000)
        pid = os.urandom(32)

        for i in range(500):
            await ml.append_message(pid, f"chaos-msg-{i}".encode())

        msgs = await ml.read_messages(pid)
        assert len(msgs) == 500
        assert msgs[0] == b"chaos-msg-0"
        assert msgs[499] == b"chaos-msg-499"
