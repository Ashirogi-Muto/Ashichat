"""Tests for peer_state, heartbeat, rate_limiter, storage, overlay, queue_manager (Phases 5-11)."""

from __future__ import annotations

import os
import struct
import time
from pathlib import Path

import pytest

from ashichat.peer_state import PeerState, PeerStateMachine, PeerStateManager, VALID_TRANSITIONS
from ashichat.rate_limiter import GlobalRateLimiter, PeerRateLimiter, TokenBucket
from ashichat.packet import PacketType
from ashichat.overlay import OverlayManager, PeerTable, MAX_PEER_TABLE
from ashichat.queue_manager import MessageStatus, QueueManager
from ashichat.session import Session
from ashichat.storage import MessageLog, StorageManager


# ═══════════════════════════════════════════════════════════════════════════
# Peer State Machine
# ═══════════════════════════════════════════════════════════════════════════


class TestPeerStateMachine:
    def test_valid_transition(self) -> None:
        sm = PeerStateMachine(PeerState.DISCONNECTED)
        old = sm.transition(PeerState.CONNECTING)
        assert old == PeerState.DISCONNECTED
        assert sm.state == PeerState.CONNECTING

    def test_invalid_transition_raises(self) -> None:
        sm = PeerStateMachine(PeerState.CONNECTED)
        with pytest.raises(ValueError, match="Invalid transition"):
            sm.transition(PeerState.ARCHIVED)

    def test_all_valid_transitions(self) -> None:
        for src, dests in VALID_TRANSITIONS.items():
            for dst in dests:
                sm = PeerStateMachine(src)
                sm.transition(dst)
                assert sm.state == dst

    def test_archived_not_terminal(self) -> None:
        sm = PeerStateMachine(PeerState.ARCHIVED)
        sm.transition(PeerState.CONNECTING)
        assert sm.state == PeerState.CONNECTING


class TestPeerStateManager:
    async def test_event_fires(self) -> None:
        mgr = PeerStateManager()
        events: list = []
        mgr.on_state_change(lambda pid, old, new: events.append((old, new)))
        mgr.ensure_machine(b"\x01" * 32, PeerState.DISCONNECTED)
        await mgr.update_state(b"\x01" * 32, PeerState.CONNECTING)
        assert len(events) == 1
        assert events[0] == (PeerState.DISCONNECTED, PeerState.CONNECTING)


# ═══════════════════════════════════════════════════════════════════════════
# Rate Limiter
# ═══════════════════════════════════════════════════════════════════════════


class TestTokenBucket:
    def test_under_limit(self) -> None:
        tb = TokenBucket(rate=10.0, capacity=10)
        for _ in range(10):
            assert tb.consume() is True

    def test_over_limit(self) -> None:
        tb = TokenBucket(rate=10.0, capacity=5)
        for _ in range(5):
            tb.consume()
        assert tb.consume() is False


class TestPeerRateLimiter:
    def test_data_limit(self) -> None:
        rl = PeerRateLimiter()
        pid = b"\x01" * 32
        # 100 DATA/sec allowed
        for _ in range(100):
            assert rl.check(pid, PacketType.DATA) is True
        assert rl.check(pid, PacketType.DATA) is False

    def test_per_peer_isolation(self) -> None:
        rl = PeerRateLimiter()
        a = b"\x01" * 32
        b_peer = b"\x02" * 32
        for _ in range(100):
            rl.check(a, PacketType.DATA)
        # b should still have capacity
        assert rl.check(b_peer, PacketType.DATA) is True


class TestGlobalRateLimiter:
    def test_resolve_limit(self) -> None:
        gl = GlobalRateLimiter()
        for _ in range(50):
            assert gl.check_resolve_forward() is True
        assert gl.check_resolve_forward() is False


# ═══════════════════════════════════════════════════════════════════════════
# Storage
# ═══════════════════════════════════════════════════════════════════════════


class TestStorageManager:
    async def test_schema_creation(self, tmp_path: Path) -> None:
        sm = StorageManager()
        await sm.initialize(tmp_path / "test.db")
        # Should not raise on second init
        await sm.close()

    async def test_peer_crud(self, tmp_path: Path) -> None:
        sm = StorageManager()
        await sm.initialize(tmp_path / "test.db")

        pid = b"\xaa" * 32
        pub = b"\xbb" * 32
        await sm.add_peer(pid, pub, "Alice")

        peer = await sm.get_peer(pid)
        assert peer is not None
        assert peer.nickname == "Alice"
        assert peer.public_key == pub

        all_peers = await sm.get_all_peers()
        assert len(all_peers) == 1
        await sm.close()

    async def test_endpoint_version_check(self, tmp_path: Path) -> None:
        sm = StorageManager()
        await sm.initialize(tmp_path / "test.db")

        pid = b"\xaa" * 32
        await sm.add_peer(pid, b"\xbb" * 32)

        assert await sm.update_endpoint(pid, "1.2.3.4:9000", 5) is True
        assert await sm.update_endpoint(pid, "5.6.7.8:9000", 3) is False  # stale
        assert await sm.update_endpoint(pid, "5.6.7.8:9000", 6) is True
        await sm.close()

    async def test_session_persistence(self, tmp_path: Path) -> None:
        sm = StorageManager()
        await sm.initialize(tmp_path / "test.db")

        sid = b"\x01" * 8
        pid = b"\xaa" * 32
        await sm.add_peer(pid, b"\xbb" * 32)
        await sm.save_session_state(sid, pid, 42, 10)

        rec = await sm.load_session_state(sid)
        assert rec is not None
        assert rec.send_sequence == 42
        assert rec.recv_sequence == 10
        await sm.close()

    async def test_message_queue_lifecycle(self, tmp_path: Path) -> None:
        sm = StorageManager()
        await sm.initialize(tmp_path / "test.db")

        pid = b"\xaa" * 32
        await sm.add_peer(pid, b"\xbb" * 32)
        await sm.enqueue_message("msg1", pid, time.time())

        queued = await sm.get_queued_messages(pid)
        assert len(queued) == 1
        assert queued[0].status == "queued"

        await sm.update_message_status("msg1", "delivered")
        queued = await sm.get_queued_messages(pid)
        assert len(queued) == 0
        await sm.close()


class TestMessageLog:
    async def test_append_and_read(self, tmp_path: Path) -> None:
        ml = MessageLog(tmp_path / "messages")
        pid = b"\x01" * 32
        blob = b"encrypted data here"
        await ml.append_message(pid, blob)

        msgs = await ml.read_messages(pid)
        assert len(msgs) == 1
        assert msgs[0] == blob

    async def test_multiple_messages(self, tmp_path: Path) -> None:
        ml = MessageLog(tmp_path / "messages")
        pid = b"\x01" * 32
        for i in range(5):
            await ml.append_message(pid, f"msg-{i}".encode())

        msgs = await ml.read_messages(pid)
        assert len(msgs) == 5
        assert msgs[2] == b"msg-2"

    async def test_crash_safety(self, tmp_path: Path) -> None:
        ml = MessageLog(tmp_path / "messages")
        pid = b"\x01" * 32
        await ml.append_message(pid, b"first")

        # Simulate a tmp file left from crash
        tmp_file = (tmp_path / "messages" / f"{pid.hex()}.log.tmp")
        tmp_file.write_bytes(b"garbage")

        # Should still read correctly
        msgs = await ml.read_messages(pid)
        assert len(msgs) == 1
        assert msgs[0] == b"first"


# ═══════════════════════════════════════════════════════════════════════════
# Overlay
# ═══════════════════════════════════════════════════════════════════════════


class TestPeerTable:
    def test_max_table_size(self) -> None:
        pt = PeerTable(b"\x00" * 32)
        for i in range(MAX_PEER_TABLE + 10):
            pt.add_peer(i.to_bytes(32, "big"), os.urandom(32))
        assert pt.size() <= MAX_PEER_TABLE

    def test_direct_peers_not_evicted(self) -> None:
        pt = PeerTable(b"\x00" * 32)
        dp_id = b"\xFF" * 32
        pt.add_direct_peer(dp_id, os.urandom(32), "Friend")

        for i in range(MAX_PEER_TABLE):
            pt.add_peer(i.to_bytes(32, "big"), os.urandom(32))

        assert pt.is_known(dp_id)
        assert pt.get_entry(dp_id).is_direct

    def test_no_self_in_table(self) -> None:
        local = b"\xAA" * 32
        pt = PeerTable(local)
        pt.add_peer(local, os.urandom(32))
        assert not pt.is_known(local)


class TestOverlayManager:
    def test_overlay_max_k(self) -> None:
        pt = PeerTable(b"\x00" * 32)
        for i in range(100):
            pt.add_peer(i.to_bytes(32, "big"), os.urandom(32))

        om = OverlayManager(pt, overlay_k=30)
        overlay = om.select_overlay()
        assert len(overlay) <= 30


# ═══════════════════════════════════════════════════════════════════════════
# Queue Manager
# ═══════════════════════════════════════════════════════════════════════════


class TestQueueManager:
    def _make_session(self) -> Session:
        return Session(
            session_id=os.urandom(8),
            encryption_key=os.urandom(32),
            remote_peer_id=os.urandom(32),
        )

    def test_enqueue_and_send(self) -> None:
        qm = QueueManager()
        session = self._make_session()
        pid = session.remote_peer_id

        msg_id = qm.enqueue(pid, b"hello")
        assert qm.get_status(msg_id) == MessageStatus.QUEUED

        result = qm.build_data_packet(msg_id, session)
        assert result is not None
        assert qm.get_status(msg_id) == MessageStatus.PENDING

    def test_ack_delivers(self) -> None:
        from ashichat.packet import AckPayload

        qm = QueueManager()
        session = self._make_session()
        pid = session.remote_peer_id

        msg_id = qm.enqueue(pid, b"hello")
        pkt, seq = qm.build_data_packet(msg_id, session)

        ack = AckPayload(session_id=session.session_id, ack_sequence_number=seq)
        result = qm.handle_ack(ack)
        assert result == msg_id
        assert qm.get_status(msg_id) == MessageStatus.DELIVERED

    def test_timeout_retries(self) -> None:
        qm = QueueManager()
        session = self._make_session()
        msg_id = qm.enqueue(session.remote_peer_id, b"hello")
        qm.build_data_packet(msg_id, session)

        # Timeout → back to queued
        status = qm.handle_timeout(msg_id)
        assert status == MessageStatus.QUEUED

    def test_max_retries_fails(self) -> None:
        qm = QueueManager()
        session = self._make_session()
        msg_id = qm.enqueue(session.remote_peer_id, b"hello")

        for _ in range(5):
            qm.build_data_packet(msg_id, session)
            qm.handle_timeout(msg_id)

        assert qm.get_status(msg_id) == MessageStatus.FAILED

    def test_decrypt_roundtrip(self) -> None:
        qm = QueueManager()
        session = self._make_session()
        msg_id = qm.enqueue(session.remote_peer_id, b"secret message")
        pkt, seq = qm.build_data_packet(msg_id, session)

        # Parse the DATA payload
        from ashichat.packet import DataPayload
        dp = DataPayload.deserialize(pkt.payload)

        # Create a "receiver" session with same key
        recv_session = Session(
            session_id=session.session_id,
            encryption_key=session.encryption_key,
            remote_peer_id=b"\x00" * 32,
        )

        plaintext = QueueManager.decrypt_data_packet(dp, recv_session)
        assert plaintext == b"secret message"

    def test_replay_rejected(self) -> None:
        from ashichat.packet import DataPayload

        qm = QueueManager()
        session = self._make_session()
        msg_id = qm.enqueue(session.remote_peer_id, b"msg")
        pkt, seq = qm.build_data_packet(msg_id, session)

        dp = DataPayload.deserialize(pkt.payload)
        recv = Session(
            session_id=session.session_id,
            encryption_key=session.encryption_key,
            remote_peer_id=b"\x00" * 32,
        )

        # First decrypt succeeds
        assert QueueManager.decrypt_data_packet(dp, recv) is not None
        # Replay rejected
        assert QueueManager.decrypt_data_packet(dp, recv) is None
