"""Tests for ashichat.node and ashichat.invite (Phases 12-13)."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from ashichat.config import AshiChatConfig, NetworkConfig, StorageConfig, DebugConfig
from ashichat.identity import generate_identity
from ashichat.invite import (
    InviteData,
    InviteError,
    generate_invite,
    generate_invite_readable,
    parse_invite,
)
from ashichat.node import Node
from ashichat.overlay import PeerTable
from ashichat.packet import (
    EndpointUpdatePayload,
    Packet,
    PacketType,
    ResolveRequestPayload,
)
from ashichat.session import Session


# ═══════════════════════════════════════════════════════════════════════════
# Invite system
# ═══════════════════════════════════════════════════════════════════════════


class TestInviteBase85:
    def test_roundtrip_no_endpoint(self) -> None:
        ident = generate_identity()
        code = generate_invite(ident.public_key)
        assert code.startswith("ashichat://v1:")
        data = parse_invite(code)
        assert data.public_key.public_bytes_raw() == ident.public_key_bytes
        assert data.endpoint is None

    def test_roundtrip_with_ipv4_endpoint(self) -> None:
        ident = generate_identity()
        code = generate_invite(ident.public_key, endpoint=("192.168.1.100", 9000))
        data = parse_invite(code)
        assert data.endpoint == ("192.168.1.100", 9000)

    def test_roundtrip_with_ipv6_endpoint(self) -> None:
        ident = generate_identity()
        code = generate_invite(ident.public_key, endpoint=("::1", 8080))
        data = parse_invite(code)
        assert data.endpoint == ("::1", 8080)


class TestInviteBase32:
    def test_roundtrip_no_endpoint(self) -> None:
        ident = generate_identity()
        code = generate_invite_readable(ident.public_key)
        assert code.startswith("ashichat://v1.h:")
        data = parse_invite(code)
        assert data.public_key.public_bytes_raw() == ident.public_key_bytes
        assert data.endpoint is None

    def test_roundtrip_with_endpoint(self) -> None:
        ident = generate_identity()
        code = generate_invite_readable(ident.public_key, endpoint=("10.0.0.1", 9000))
        data = parse_invite(code)
        assert data.endpoint == ("10.0.0.1", 9000)

    def test_human_readable_uppercase(self) -> None:
        ident = generate_identity()
        code = generate_invite_readable(ident.public_key)
        payload_part = code.split("ashichat://v1.h:")[1]
        # Base32 is uppercase + digits (and optional =)
        assert payload_part == payload_part.upper()


class TestInviteCrossFormat:
    def test_base85_not_parsed_as_base32(self) -> None:
        ident = generate_identity()
        code85 = generate_invite(ident.public_key)
        code32 = generate_invite_readable(ident.public_key)

        # Both should parse correctly
        d85 = parse_invite(code85)
        d32 = parse_invite(code32)
        assert d85.public_key.public_bytes_raw() == d32.public_key.public_bytes_raw()


class TestInviteErrors:
    def test_invalid_prefix(self) -> None:
        with pytest.raises(InviteError, match="prefix"):
            parse_invite("https://example.com")

    def test_invalid_base85(self) -> None:
        with pytest.raises(InviteError):
            parse_invite("ashichat://v1:!!!invalid!!!")

    def test_truncated_payload(self) -> None:
        with pytest.raises(InviteError):
            parse_invite("ashichat://v1:YQ==")  # too short


# ═══════════════════════════════════════════════════════════════════════════
# Node orchestrator
# ═══════════════════════════════════════════════════════════════════════════


class TestNode:
    def _make_config(self, tmp_path: Path, port: int = 0) -> AshiChatConfig:
        return AshiChatConfig(
            network=NetworkConfig(udp_port=port),
            storage=StorageConfig(),
            debug=DebugConfig(),
            base_dir=tmp_path,
        )

    async def test_start_stop(self, tmp_path: Path) -> None:
        config = self._make_config(tmp_path, port=0)
        node = Node(config)
        await node.start()

        assert node.identity is not None
        assert node.transport is not None
        assert node.peer_table is not None

        await node.stop()

    async def test_identity_persists(self, tmp_path: Path) -> None:
        config = self._make_config(tmp_path, port=0)

        # First run — generates identity
        node1 = Node(config)
        await node1.start()
        peer_id_1 = node1.identity.peer_id
        await node1.stop()

        # Second run — loads same identity
        node2 = Node(config)
        await node2.start()
        peer_id_2 = node2.identity.peer_id
        await node2.stop()

        assert peer_id_1 == peer_id_2

    async def test_packet_dispatch(self, tmp_path: Path) -> None:
        """PING dispatches to ping handler, which sends PONG."""
        config = self._make_config(tmp_path, port=0)
        node = Node(config)
        # Override to bind on localhost only
        from ashichat.transport_udp import start_udp_listener
        from ashichat.packet import PacketType, PingPayload, PongPayload, make_packet

        await node.start()

        received: list = []

        async def receiver_handler(pkt, addr):
            received.append(pkt)

        _, receiver = await start_udp_listener(0, receiver_handler, "127.0.0.1")
        recv_addr = receiver._transport.get_extra_info("sockname")
        node_addr = node.transport._transport.get_extra_info("sockname")
        # Use 127.0.0.1 with the node's port for loopback
        node_local_addr = ("127.0.0.1", node_addr[1])

        # Send PING to node
        ping = make_packet(
            PacketType.PING,
            PingPayload(session_id=os.urandom(8), ping_id=b"\xaa" * 8),
        )
        receiver.send_packet(ping, node_local_addr)

        await asyncio.sleep(0.3)

        # Node should have responded with PONG
        pongs = [p for p in received if p.packet_type == PacketType.PONG]
        assert len(pongs) >= 1

        receiver.close()
        await node.stop()

    async def test_session_survives_restart(self, tmp_path: Path) -> None:
        """Session sequences are persisted to DB and restored on restart."""
        from ashichat.session import Session

        config = self._make_config(tmp_path, port=0)

        node = Node(config)
        await node.start()

        # Manually register a session
        session = Session(
            session_id=b"\x01" * 8,
            encryption_key=os.urandom(32),
            remote_peer_id=os.urandom(32),
            send_sequence=42,
            recv_sequence=10,
        )
        node.session_registry.register(session)

        # Stop flushes to DB
        await node.stop()

        # Verify DB has the values
        from ashichat.storage import StorageManager
        sm = StorageManager()
        await sm.initialize(tmp_path / "data" / "ashichat.db")
        rec = await sm.load_session_state(b"\x01" * 8)
        assert rec is not None
        assert rec.send_sequence == 42
        assert rec.recv_sequence == 10
        await sm.close()

    async def test_add_contact_from_invite_persists_peer_and_endpoint(self, tmp_path: Path) -> None:
        alice_dir = tmp_path / "alice"
        bob_dir = tmp_path / "bob"
        alice = Node(self._make_config(alice_dir, port=0))
        bob = Node(self._make_config(bob_dir, port=0))

        await alice.start()
        await bob.start()
        try:
            bob_port = bob.transport._transport.get_extra_info("sockname")[1]
            invite = generate_invite(bob.identity.public_key, endpoint=("127.0.0.1", bob_port))

            result = await alice.add_contact_from_invite(invite)
            known = await alice.get_known_peers()

            assert result.peer_id == bob.identity.peer_id
            assert result.connection_started is True
            assert len(known) == 1
            assert known[0].peer_id == bob.identity.peer_id
            assert known[0].last_known_endpoint == f"127.0.0.1:{bob_port}"
        finally:
            await alice.stop()
            await bob.stop()

    async def test_accept_invite_bootstraps_both_sides_and_delivers_first_message(
        self, tmp_path: Path
    ) -> None:
        alice_dir = tmp_path / "alice"
        bob_dir = tmp_path / "bob"
        alice = Node(self._make_config(alice_dir, port=0))
        bob = Node(self._make_config(bob_dir, port=0))

        received: list[tuple[bytes, bytes]] = []
        bob.on_message_received(lambda peer_id, plaintext: received.append((peer_id, plaintext)))

        await alice.start()
        await bob.start()
        try:
            bob_port = bob.transport._transport.get_extra_info("sockname")[1]
            invite = generate_invite(bob.identity.public_key, endpoint=("127.0.0.1", bob_port))

            await alice.add_contact_from_invite(invite)
            await _wait_for(
                lambda: (
                    alice.session_registry.get_by_peer_id(bob.identity.peer_id) is not None
                    and bob.session_registry.get_by_peer_id(alice.identity.peer_id) is not None
                )
            )

            bob_known = await bob.get_known_peers()
            assert any(peer.peer_id == alice.identity.peer_id for peer in bob_known)

            await alice.send_message(bob.identity.peer_id, "hello from alice")
            await _wait_for(lambda: len(received) == 1)

            assert received == [(alice.identity.peer_id, b"hello from alice")]
        finally:
            await alice.stop()
            await bob.stop()

    async def test_resolve_request_uses_cached_signed_endpoint_update(
        self, tmp_path: Path
    ) -> None:
        node = Node(self._make_config(tmp_path, port=0))
        node.identity = generate_identity()
        node.peer_table = PeerTable(node.identity.peer_id)

        remote = generate_identity()
        node.peer_table.add_direct_peer(
            remote.peer_id,
            remote.public_key_bytes,
            endpoint=("10.0.0.9", 9100),
        )
        entry = node.peer_table.get_entry(remote.peer_id)
        assert entry is not None
        entry.version_counter = 7
        entry.endpoint_signature = b"\x55" * 64

        sent: list = []
        node._send_to = lambda packet, addr: sent.append((packet, addr))

        req = ResolveRequestPayload(
            request_id=os.urandom(16),
            target_peer_id=remote.peer_id,
            ttl=5,
        )
        await node._handle_resolve_request(
            Packet(version=0x01, packet_type=PacketType.RESOLVE_REQUEST, payload=req.serialize()),
            ("127.0.0.1", 9999),
        )

        assert len(sent) == 1
        pkt, addr = sent[0]
        assert addr == ("127.0.0.1", 9999)
        assert pkt.packet_type == PacketType.ENDPOINT_UPDATE
        update = EndpointUpdatePayload.deserialize(pkt.payload)
        assert update.peer_id == remote.peer_id
        assert update.endpoint_ip == "10.0.0.9"
        assert update.endpoint_port == 9100
        assert update.version_counter == 7
        assert update.signature == b"\x55" * 64

    async def test_build_endpoint_update_uses_monotonic_local_version(
        self, tmp_path: Path
    ) -> None:
        node = Node(self._make_config(tmp_path, port=0))
        await node.start()
        try:
            pkt1 = node._build_endpoint_update(("127.0.0.1", 9001))
            pkt2 = node._build_endpoint_update(("127.0.0.1", 9002))
            await asyncio.sleep(0)

            assert pkt1 is not None
            assert pkt2 is not None

            update1 = EndpointUpdatePayload.deserialize(pkt1.payload)
            update2 = EndpointUpdatePayload.deserialize(pkt2.payload)
            assert update1.version_counter == 1
            assert update2.version_counter == 2
            assert await node.storage.get_local_endpoint_version() == 2
        finally:
            await node.stop()

    async def test_reconnect_falls_back_to_resolution_after_failed_attempts(
        self, tmp_path: Path
    ) -> None:
        node = Node(self._make_config(tmp_path, port=0))
        node.identity = generate_identity()
        node.peer_table = PeerTable(node.identity.peer_id)

        remote = generate_identity()
        node.peer_table.add_direct_peer(
            remote.peer_id,
            remote.public_key_bytes,
            endpoint=("10.0.0.5", 9000),
        )

        calls: list[tuple[str, object]] = []

        class FakeResolver:
            async def resolve_peer(self, peer_id, *, force_network=False):
                calls.append(("resolve", force_network))
                return ("10.0.0.99", 9010)

        async def fake_connect(peer_id: bytes, endpoint: tuple[str, int]) -> None:
            calls.append(("connect", endpoint))

        node.resolver = FakeResolver()
        node.connect_to_peer = fake_connect  # type: ignore[method-assign]
        async def fake_remember_endpoint(peer_id: bytes, endpoint: tuple[str, int]) -> None:
            return None
        node.storage.remember_endpoint = fake_remember_endpoint  # type: ignore[method-assign]
        node.reconnect.record_disconnect(remote.peer_id)
        node.reconnect.record_attempt(remote.peer_id)

        await node._reconnect_peer(remote.peer_id)

        assert ("resolve", True) in calls
        assert ("connect", ("10.0.0.99", 9010)) in calls
        assert node.peer_table.get_endpoint(remote.peer_id) == ("10.0.0.99", 9010)

    async def test_queued_message_payload_lives_in_encrypted_outbox_not_sqlite(
        self, tmp_path: Path
    ) -> None:
        node = Node(self._make_config(tmp_path, port=0))
        await node.start()
        try:
            remote = generate_identity()
            await node.storage.add_peer(remote.peer_id, remote.public_key_bytes)
            node.peer_table.add_direct_peer(remote.peer_id, remote.public_key_bytes)

            msg_id = await node.send_message(remote.peer_id, "store me safely")
            assert msg_id is not None

            queued = await node.storage.load_all_queued_messages()
            record = next(q for q in queued if q.message_id == msg_id)
            assert record.plaintext == b""

            assert node.outbox is not None
            recovered = await node.outbox.load_message(msg_id)
            assert recovered == b"store me safely"

            outbox_blob = (tmp_path / "data" / "outbox" / f"{msg_id}.bin").read_bytes()
            assert b"store me safely" not in outbox_blob
        finally:
            await node.stop()

    async def test_sent_message_sequence_and_payload_survive_restart(
        self, tmp_path: Path
    ) -> None:
        config = self._make_config(tmp_path, port=0)
        remote = generate_identity()

        node1 = Node(config)
        await node1.start()
        try:
            await node1.storage.add_peer(remote.peer_id, remote.public_key_bytes, endpoint=("127.0.0.1", 9009))
            node1.peer_table.add_direct_peer(
                remote.peer_id,
                remote.public_key_bytes,
                endpoint=("127.0.0.1", 9009),
            )
            node1.session_registry.register(
                Session(
                    session_id=b"\x11" * 8,
                    encryption_key=os.urandom(32),
                    remote_peer_id=remote.peer_id,
                )
            )

            msg_id = await node1.send_message(remote.peer_id, "restart sync seed")
            assert msg_id is not None
            seq1 = node1.queue_manager._queue[msg_id]["sequence_number"]
            assert seq1 is not None
        finally:
            await node1.stop()

        node2 = Node(config)
        await node2.start()
        try:
            restored = node2.queue_manager._queue[msg_id]
            assert restored["sequence_number"] == seq1
            assert restored["plaintext"] == b"restart sync seed"
        finally:
            await node2.stop()


async def _wait_for(predicate, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met before timeout")
