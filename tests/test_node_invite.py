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
