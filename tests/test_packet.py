"""Tests for ashichat.packet (Phase 2)."""

from __future__ import annotations

import os
import struct

import pytest

from ashichat.packet import (
    HEADER_SIZE,
    MAX_TTL,
    PROTOCOL_VERSION,
    AckPayload,
    DataPayload,
    EndpointUpdatePayload,
    HelloAckPayload,
    HelloPayload,
    Packet,
    PacketError,
    PacketType,
    PingPayload,
    PongPayload,
    ResolveRequestPayload,
    make_packet,
)


# ═══════════════════════════════════════════════════════════════════════════
# Packet header roundtrip
# ═══════════════════════════════════════════════════════════════════════════


class TestPacketRoundtrip:
    @pytest.mark.parametrize("ptype", list(PacketType))
    def test_roundtrip_all_types(self, ptype: PacketType) -> None:
        payload = os.urandom(32)
        pkt = Packet(version=PROTOCOL_VERSION, packet_type=ptype, payload=payload)
        raw = pkt.serialize()
        parsed = Packet.deserialize(raw)
        assert parsed.version == PROTOCOL_VERSION
        assert parsed.packet_type == ptype
        assert parsed.payload == payload

    def test_empty_payload(self) -> None:
        pkt = Packet(version=PROTOCOL_VERSION, packet_type=PacketType.PING, payload=b"")
        raw = pkt.serialize()
        parsed = Packet.deserialize(raw)
        assert parsed.payload == b""

    def test_max_size_payload(self) -> None:
        big = os.urandom(65535)
        pkt = Packet(version=PROTOCOL_VERSION, packet_type=PacketType.DATA, payload=big)
        raw = pkt.serialize()
        parsed = Packet.deserialize(raw)
        assert parsed.payload == big


# ═══════════════════════════════════════════════════════════════════════════
# Typed payload roundtrips
# ═══════════════════════════════════════════════════════════════════════════


class TestHelloPayload:
    def test_roundtrip(self) -> None:
        orig = HelloPayload(
            identity_public_key=os.urandom(32),
            ephemeral_public_key=os.urandom(32),
            random_nonce=os.urandom(16),
            signature=os.urandom(64),
        )
        data = orig.serialize()
        parsed = HelloPayload.deserialize(data)
        assert parsed.identity_public_key == orig.identity_public_key
        assert parsed.ephemeral_public_key == orig.ephemeral_public_key
        assert parsed.random_nonce == orig.random_nonce
        assert parsed.signature == orig.signature

    def test_truncated(self) -> None:
        with pytest.raises(PacketError):
            HelloPayload.deserialize(os.urandom(100))


class TestHelloAckPayload:
    def test_roundtrip(self) -> None:
        orig = HelloAckPayload(
            identity_public_key=os.urandom(32),
            ephemeral_public_key=os.urandom(32),
            session_id=os.urandom(8),
            random_nonce=os.urandom(16),
            signature=os.urandom(64),
        )
        parsed = HelloAckPayload.deserialize(orig.serialize())
        assert parsed.identity_public_key == orig.identity_public_key
        assert parsed.session_id == orig.session_id
        assert parsed.signature == orig.signature


class TestDataPayload:
    def test_roundtrip(self) -> None:
        orig = DataPayload(
            session_id=os.urandom(8),
            sequence_number=42,
            ciphertext=os.urandom(128),
            auth_tag=os.urandom(16),
        )
        parsed = DataPayload.deserialize(orig.serialize())
        assert parsed.session_id == orig.session_id
        assert parsed.sequence_number == 42
        assert parsed.ciphertext == orig.ciphertext
        assert parsed.auth_tag == orig.auth_tag

    def test_empty_ciphertext(self) -> None:
        orig = DataPayload(
            session_id=os.urandom(8),
            sequence_number=0,
            ciphertext=b"",
            auth_tag=os.urandom(16),
        )
        parsed = DataPayload.deserialize(orig.serialize())
        assert parsed.ciphertext == b""

    def test_truncated(self) -> None:
        with pytest.raises(PacketError):
            DataPayload.deserialize(os.urandom(10))


class TestAckPayload:
    def test_roundtrip(self) -> None:
        orig = AckPayload(session_id=os.urandom(8), ack_sequence_number=99)
        parsed = AckPayload.deserialize(orig.serialize())
        assert parsed.session_id == orig.session_id
        assert parsed.ack_sequence_number == 99


class TestEndpointUpdatePayload:
    def test_roundtrip_ipv4(self) -> None:
        orig = EndpointUpdatePayload(
            peer_id=os.urandom(32),
            endpoint_ip="192.168.1.100",
            endpoint_port=9000,
            version_counter=5,
            signature=os.urandom(64),
        )
        parsed = EndpointUpdatePayload.deserialize(orig.serialize())
        assert parsed.endpoint_ip == "192.168.1.100"
        assert parsed.endpoint_port == 9000
        assert parsed.version_counter == 5

    def test_roundtrip_ipv6(self) -> None:
        orig = EndpointUpdatePayload(
            peer_id=os.urandom(32),
            endpoint_ip="::1",
            endpoint_port=8080,
            version_counter=1,
            signature=os.urandom(64),
        )
        parsed = EndpointUpdatePayload.deserialize(orig.serialize())
        assert parsed.endpoint_ip == "::1"
        assert parsed.endpoint_port == 8080

    def test_invalid_ip(self) -> None:
        bad = EndpointUpdatePayload(
            peer_id=os.urandom(32),
            endpoint_ip="not-an-ip",
            endpoint_port=9000,
            version_counter=1,
            signature=os.urandom(64),
        )
        with pytest.raises(PacketError, match="Invalid IP"):
            bad.serialize()


class TestResolveRequestPayload:
    def test_roundtrip(self) -> None:
        orig = ResolveRequestPayload(
            request_id=os.urandom(16),
            target_peer_id=os.urandom(32),
            ttl=3,
        )
        parsed = ResolveRequestPayload.deserialize(orig.serialize())
        assert parsed.request_id == orig.request_id
        assert parsed.target_peer_id == orig.target_peer_id
        assert parsed.ttl == 3

    def test_ttl_clamped_serialize(self) -> None:
        orig = ResolveRequestPayload(
            request_id=os.urandom(16),
            target_peer_id=os.urandom(32),
            ttl=255,
        )
        data = orig.serialize()
        parsed = ResolveRequestPayload.deserialize(data)
        assert parsed.ttl == MAX_TTL  # clamped to 5

    def test_ttl_clamped_deserialize(self) -> None:
        """Raw byte with ttl=200 gets clamped."""
        raw = os.urandom(16) + os.urandom(32) + bytes([200])
        parsed = ResolveRequestPayload.deserialize(raw)
        assert parsed.ttl == MAX_TTL


class TestPingPongPayload:
    def test_ping_roundtrip(self) -> None:
        orig = PingPayload(session_id=os.urandom(8), ping_id=os.urandom(8))
        parsed = PingPayload.deserialize(orig.serialize())
        assert parsed.session_id == orig.session_id
        assert parsed.ping_id == orig.ping_id

    def test_pong_roundtrip(self) -> None:
        orig = PongPayload(session_id=os.urandom(8), ping_id=os.urandom(8))
        parsed = PongPayload.deserialize(orig.serialize())
        assert parsed.ping_id == orig.ping_id


# ═══════════════════════════════════════════════════════════════════════════
# Invalid packets
# ═══════════════════════════════════════════════════════════════════════════


class TestInvalidPackets:
    def test_invalid_version(self) -> None:
        raw = struct.pack(">BBH", 0x02, 0x01, 0) 
        with pytest.raises(PacketError, match="Unknown version"):
            Packet.deserialize(raw)

    def test_unknown_type(self) -> None:
        raw = struct.pack(">BBH", 0x01, 0xFF, 0)
        with pytest.raises(PacketError, match="Unknown packet type"):
            Packet.deserialize(raw)

    def test_truncated_header(self) -> None:
        with pytest.raises(PacketError, match="too short"):
            Packet.deserialize(b"\x01\x01")

    def test_truncated_payload(self) -> None:
        # Declare 100 bytes but only provide 5
        raw = struct.pack(">BBH", 0x01, 0x03, 100) + os.urandom(5)
        with pytest.raises(PacketError, match="Truncated"):
            Packet.deserialize(raw)

    def test_empty_bytes(self) -> None:
        with pytest.raises(PacketError):
            Packet.deserialize(b"")

    @pytest.mark.parametrize("_", range(20))
    def test_random_bytes_never_crash(self, _: int) -> None:
        """Fuzz-like: random bytes should raise PacketError, never crash."""
        data = os.urandom(50)
        try:
            Packet.deserialize(data)
        except PacketError:
            pass  # expected
        # If it parses (unlikely but possible for valid header+type), that's fine too


class TestMakePacket:
    def test_convenience_helper(self) -> None:
        ping = PingPayload(session_id=os.urandom(8), ping_id=os.urandom(8))
        pkt = make_packet(PacketType.PING, ping)
        assert pkt.version == PROTOCOL_VERSION
        assert pkt.packet_type == PacketType.PING
        assert pkt.payload == ping.serialize()
