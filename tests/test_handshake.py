"""Tests for ashichat.handshake and ashichat.session (Phase 3)."""

from __future__ import annotations

import os

import pytest

from ashichat.handshake import (
    create_hello,
    process_hello,
    process_hello_ack,
)
from ashichat.identity import derive_peer_id, generate_identity
from ashichat.packet import HelloPayload, Packet, PacketType
from ashichat.session import Session, SessionRegistry, MAX_SESSIONS


# ═══════════════════════════════════════════════════════════════════════════
# Handshake flow
# ═══════════════════════════════════════════════════════════════════════════


class TestHandshakeFlow:
    """Full HELLO → HELLO_ACK flow between Alice and Bob."""

    def _setup_peers(self):
        alice = generate_identity()
        bob = generate_identity()
        alice_known = {bob.peer_id}
        bob_known = {alice.peer_id}
        return alice, bob, alice_known, bob_known

    def test_successful_handshake(self) -> None:
        alice, bob, alice_known, bob_known = self._setup_peers()

        # Alice creates HELLO
        hello_pkt, alice_state = create_hello(alice)
        assert hello_pkt.packet_type == PacketType.HELLO

        # Bob processes HELLO → produces HELLO_ACK + session keys
        result = process_hello(hello_pkt, bob_known, bob)
        assert result is not None
        ack_pkt, bob_keys = result
        assert ack_pkt.packet_type == PacketType.HELLO_ACK

        # Alice processes HELLO_ACK → derives session keys
        alice_keys = process_hello_ack(ack_pkt, alice_state, alice_known)
        assert alice_keys is not None

        # CRITICAL INVARIANT: both sides derive the same encryption key
        assert alice_keys.encryption_key == bob_keys.encryption_key
        assert alice_keys.session_id == bob_keys.session_id

    def test_both_know_each_other(self) -> None:
        alice, bob, alice_known, bob_known = self._setup_peers()
        hello_pkt, state = create_hello(alice)
        result = process_hello(hello_pkt, bob_known, bob)
        assert result is not None
        ack_pkt, bob_keys = result
        alice_keys = process_hello_ack(ack_pkt, state, alice_known)
        assert alice_keys is not None
        assert alice_keys.remote_peer_id == bob.peer_id
        assert bob_keys.remote_peer_id == alice.peer_id
        assert alice_keys.session_id == bob_keys.session_id

    def test_unknown_peer_rejected_hello(self) -> None:
        alice, bob, _, _ = self._setup_peers()
        hello_pkt, _ = create_hello(alice)
        # Bob does NOT know Alice
        result = process_hello(hello_pkt, set(), bob)
        assert result is None

    def test_unknown_peer_rejected_hello_ack(self) -> None:
        alice, bob, alice_known, bob_known = self._setup_peers()
        hello_pkt, state = create_hello(alice)
        result = process_hello(hello_pkt, bob_known, bob)
        assert result is not None
        ack_pkt, _ = result
        # Alice does NOT know Bob
        alice_keys = process_hello_ack(ack_pkt, state, set())
        assert alice_keys is None

    def test_invalid_hello_signature(self) -> None:
        alice, bob, _, bob_known = self._setup_peers()
        hello_pkt, _ = create_hello(alice)

        # Tamper with the signature in the payload
        payload = HelloPayload.deserialize(hello_pkt.payload)
        tampered_sig = bytearray(payload.signature)
        tampered_sig[0] ^= 0xFF
        tampered_payload = HelloPayload(
            identity_public_key=payload.identity_public_key,
            ephemeral_public_key=payload.ephemeral_public_key,
            random_nonce=payload.random_nonce,
            signature=bytes(tampered_sig),
        )
        tampered_pkt = Packet(
            version=hello_pkt.version,
            packet_type=hello_pkt.packet_type,
            payload=tampered_payload.serialize(),
        )

        result = process_hello(tampered_pkt, bob_known, bob)
        assert result is None

    def test_session_key_uniqueness(self) -> None:
        """Two handshakes between the same peers produce different keys."""
        alice, bob, alice_known, bob_known = self._setup_peers()

        hello1, state1 = create_hello(alice)
        result1 = process_hello(hello1, bob_known, bob)
        ack1, keys1 = result1
        alice_keys1 = process_hello_ack(ack1, state1, alice_known)

        hello2, state2 = create_hello(alice)
        result2 = process_hello(hello2, bob_known, bob)
        ack2, keys2 = result2
        alice_keys2 = process_hello_ack(ack2, state2, alice_known)

        assert alice_keys1.encryption_key != alice_keys2.encryption_key
        assert alice_keys1.session_id != alice_keys2.session_id


# ═══════════════════════════════════════════════════════════════════════════
# Session
# ═══════════════════════════════════════════════════════════════════════════


class TestSession:
    def test_sequence_initial_values(self) -> None:
        s = Session(
            session_id=os.urandom(8),
            encryption_key=os.urandom(32),
            remote_peer_id=os.urandom(32),
        )
        assert s.send_sequence == 1  # starts at 1
        assert s.recv_sequence == 0  # 0 means "nothing received"

    def test_next_sequence_increments(self) -> None:
        s = Session(
            session_id=os.urandom(8),
            encryption_key=os.urandom(32),
            remote_peer_id=os.urandom(32),
        )
        assert s.next_sequence() == 1
        assert s.next_sequence() == 2
        assert s.next_sequence() == 3
        assert s.send_sequence == 4

    def test_validate_recv_rejects_replay(self) -> None:
        s = Session(
            session_id=os.urandom(8),
            encryption_key=os.urandom(32),
            remote_peer_id=os.urandom(32),
        )
        assert s.validate_recv_sequence(1) is True
        assert s.validate_recv_sequence(1) is False  # replay
        assert s.validate_recv_sequence(0) is False  # old

    def test_validate_recv_accepts_higher(self) -> None:
        s = Session(
            session_id=os.urandom(8),
            encryption_key=os.urandom(32),
            remote_peer_id=os.urandom(32),
        )
        assert s.validate_recv_sequence(5) is True
        assert s.validate_recv_sequence(10) is True
        assert s.recv_sequence == 10


class TestSessionRegistry:
    def test_register_and_lookup(self) -> None:
        reg = SessionRegistry()
        s = Session(
            session_id=b"\x01" * 8,
            encryption_key=os.urandom(32),
            remote_peer_id=b"\x02" * 32,
        )
        reg.register(s)
        assert reg.get_by_session_id(b"\x01" * 8) is s
        assert reg.get_by_peer_id(b"\x02" * 32) is s
        assert reg.active_count() == 1

    def test_remove(self) -> None:
        reg = SessionRegistry()
        s = Session(
            session_id=b"\x01" * 8,
            encryption_key=os.urandom(32),
            remote_peer_id=b"\x02" * 32,
        )
        reg.register(s)
        reg.remove(b"\x01" * 8)
        assert reg.get_by_session_id(b"\x01" * 8) is None
        assert reg.active_count() == 0

    def test_replaces_old_session_for_same_peer(self) -> None:
        reg = SessionRegistry()
        s1 = Session(
            session_id=b"\x01" * 8,
            encryption_key=os.urandom(32),
            remote_peer_id=b"\xAA" * 32,
        )
        s2 = Session(
            session_id=b"\x02" * 8,
            encryption_key=os.urandom(32),
            remote_peer_id=b"\xAA" * 32,
        )
        reg.register(s1)
        reg.register(s2)
        assert reg.get_by_peer_id(b"\xAA" * 32) is s2
        assert reg.get_by_session_id(b"\x01" * 8) is None  # old removed
        assert reg.active_count() == 1

    def test_max_sessions_enforced(self) -> None:
        reg = SessionRegistry()
        for i in range(MAX_SESSIONS):
            reg.register(
                Session(
                    session_id=i.to_bytes(8, "big"),
                    encryption_key=os.urandom(32),
                    remote_peer_id=i.to_bytes(32, "big"),
                )
            )
        with pytest.raises(RuntimeError, match="limit"):
            reg.register(
                Session(
                    session_id=b"\xFF" * 8,
                    encryption_key=os.urandom(32),
                    remote_peer_id=b"\xFF" * 32,
                )
            )
