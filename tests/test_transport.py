"""Tests for ashichat.transport_udp (Phase 4)."""

from __future__ import annotations

import asyncio
import os

import pytest

from ashichat.handshake import create_hello, process_hello, process_hello_ack
from ashichat.identity import generate_identity
from ashichat.packet import Packet, PacketType, PingPayload, PongPayload, make_packet
from ashichat.transport_udp import UDPTransport, start_udp_listener, stop_udp_listener


class TestUDPLoopback:
    """Two UDP listeners on localhost exchange packets."""

    async def test_ping_pong(self) -> None:
        received: list[tuple[Packet, tuple[str, int]]] = []

        async def handler(pkt: Packet, addr: tuple[str, int]) -> None:
            received.append((pkt, addr))

        _, proto_a = await start_udp_listener(0, handler, "127.0.0.1")
        _, proto_b = await start_udp_listener(0, handler, "127.0.0.1")

        addr_a = proto_a._transport.get_extra_info("sockname")
        addr_b = proto_b._transport.get_extra_info("sockname")

        # A sends PING to B
        ping = make_packet(
            PacketType.PING,
            PingPayload(session_id=os.urandom(8), ping_id=os.urandom(8)),
        )
        proto_a.send_packet(ping, addr_b)

        await asyncio.sleep(0.1)
        assert len(received) == 1
        assert received[0][0].packet_type == PacketType.PING

        proto_a.close()
        proto_b.close()

    async def test_invalid_data_dropped(self) -> None:
        received: list = []

        async def handler(pkt, addr):
            received.append(pkt)

        _, proto = await start_udp_listener(0, handler, "127.0.0.1")
        addr = proto._transport.get_extra_info("sockname")

        # Send garbage bytes directly
        proto._transport.sendto(b"\xff\xff\xff\xff", addr)
        await asyncio.sleep(0.1)

        assert len(received) == 0  # garbage was dropped

        proto.close()

    async def test_loopback_handshake(self) -> None:
        """Full HELLO/HELLO_ACK exchange over real UDP."""
        alice = generate_identity()
        bob = generate_identity()
        alice_known = {bob.peer_id}
        bob_known = {alice.peer_id}

        alice_received: list[tuple[Packet, tuple]] = []
        bob_received: list[tuple[Packet, tuple]] = []

        async def alice_handler(pkt, addr):
            alice_received.append((pkt, addr))

        async def bob_handler(pkt, addr):
            bob_received.append((pkt, addr))

        _, proto_a = await start_udp_listener(0, alice_handler, "127.0.0.1")
        _, proto_b = await start_udp_listener(0, bob_handler, "127.0.0.1")

        addr_a = proto_a._transport.get_extra_info("sockname")
        addr_b = proto_b._transport.get_extra_info("sockname")

        # Alice → Bob: HELLO
        hello_pkt, alice_state = create_hello(alice)
        proto_a.send_packet(hello_pkt, addr_b)
        await asyncio.sleep(0.1)

        assert len(bob_received) == 1
        assert bob_received[0][0].packet_type == PacketType.HELLO

        # Bob processes HELLO → sends HELLO_ACK
        result = process_hello(bob_received[0][0], bob_known, bob)
        assert result is not None
        ack_pkt, bob_keys = result
        proto_b.send_packet(ack_pkt, addr_a)
        await asyncio.sleep(0.1)

        assert len(alice_received) == 1
        assert alice_received[0][0].packet_type == PacketType.HELLO_ACK

        # Alice processes HELLO_ACK
        alice_keys = process_hello_ack(alice_received[0][0], alice_state, alice_known)
        assert alice_keys is not None
        assert alice_keys.encryption_key == bob_keys.encryption_key
        assert alice_keys.session_id == bob_keys.session_id

        proto_a.close()
        proto_b.close()

    async def test_large_packet(self) -> None:
        received: list[Packet] = []

        async def handler(pkt, addr):
            received.append(pkt)

        _, proto_a = await start_udp_listener(0, handler, "127.0.0.1")
        _, proto_b = await start_udp_listener(0, handler, "127.0.0.1")

        addr_b = proto_b._transport.get_extra_info("sockname")

        # Send a packet with 8KB payload (well within UDP limits on loopback)
        big_payload = os.urandom(8000)
        pkt = Packet(version=0x01, packet_type=PacketType.DATA, payload=big_payload)
        proto_a.send_packet(pkt, addr_b)

        await asyncio.sleep(0.1)
        assert len(received) == 1
        assert received[0].payload == big_payload

        proto_a.close()
        proto_b.close()
