"""Handshake protocol for AshiChat (HELLO / HELLO_ACK).

Implements strict mutual authentication — nodes MUST reject any handshake
from unknown public keys not already present in DirectPeers.
No trust-on-first-use (TOFU).
"""

from __future__ import annotations

from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from ashichat.crypto import (
    compute_shared_secret,
    derive_session_key,
    generate_ephemeral_keypair,
    generate_nonce_16,
    generate_session_id,
)
from ashichat.identity import LocalIdentity, derive_peer_id, verify_signature
from ashichat.logging_setup import get_logger
from ashichat.packet import (
    HelloAckPayload,
    HelloPayload,
    Packet,
    PacketType,
    make_packet,
)
from ashichat.session import SessionKeys

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Handshake state (held in memory between HELLO and HELLO_ACK)
# ---------------------------------------------------------------------------

@dataclass
class HandshakeState:
    """Ephemeral state held by the initiator between HELLO and HELLO_ACK."""

    ephemeral_private_key_bytes: bytes  # raw X25519 private
    ephemeral_public_key_bytes: bytes  # raw X25519 public
    local_nonce: bytes  # 16 bytes
    target_peer_id: bytes | None  # expected responder (if known)


# ---------------------------------------------------------------------------
# Helper: deserialize raw key bytes
# ---------------------------------------------------------------------------

def _ed25519_from_raw(raw: bytes) -> Ed25519PublicKey:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    return Ed25519PublicKey.from_public_bytes(raw)


def _x25519_pub_from_raw(raw: bytes):
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey
    return X25519PublicKey.from_public_bytes(raw)


def _x25519_priv_from_raw(raw: bytes):
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    return X25519PrivateKey.from_private_bytes(raw)


# ---------------------------------------------------------------------------
# Initiator
# ---------------------------------------------------------------------------

def create_hello(local_identity: LocalIdentity) -> tuple[Packet, HandshakeState]:
    """Build a HELLO packet for the initiator side.

    Returns ``(packet, state)`` where *state* must be kept for
    ``process_hello_ack``.
    """
    eph_priv, eph_pub = generate_ephemeral_keypair()
    nonce = generate_nonce_16()

    # Raw bytes for serialization
    eph_pub_bytes = eph_pub.public_bytes(Encoding.Raw, PublicFormat.Raw)
    eph_priv_bytes = eph_priv.private_bytes(
        Encoding.Raw,
        format=__import__("cryptography.hazmat.primitives.serialization", fromlist=["PrivateFormat"]).PrivateFormat.Raw,
        encryption_algorithm=__import__("cryptography.hazmat.primitives.serialization", fromlist=["NoEncryption"]).NoEncryption(),
    )

    # Signature: sign(identity_private, ephemeral_pub || nonce)
    sig = local_identity.sign(eph_pub_bytes + nonce)

    payload = HelloPayload(
        identity_public_key=local_identity.public_key_bytes,
        ephemeral_public_key=eph_pub_bytes,
        random_nonce=nonce,
        signature=sig,
    )

    state = HandshakeState(
        ephemeral_private_key_bytes=eph_priv_bytes,
        ephemeral_public_key_bytes=eph_pub_bytes,
        local_nonce=nonce,
        target_peer_id=None,
    )

    return make_packet(PacketType.HELLO, payload), state


def process_hello_ack(
    packet: Packet,
    state: HandshakeState,
    known_peers: set[bytes],
) -> SessionKeys | None:
    """Process a received HELLO_ACK (initiator side).

    Returns ``SessionKeys`` on success, ``None`` on verification failure.
    """
    ack = HelloAckPayload.deserialize(packet.payload)

    # 1. Derive peer_id and check it's known
    remote_pubkey = _ed25519_from_raw(ack.identity_public_key)
    remote_peer_id = derive_peer_id(remote_pubkey)
    if remote_peer_id not in known_peers:
        log.warning("HELLO_ACK from unknown peer %s — rejected", remote_peer_id.hex()[:8])
        return None

    # 2. Verify signature: sign(identity_private, session_id || receiver_eph || initiator_eph || initiator_nonce)
    signed_data = (
        ack.session_id
        + ack.ephemeral_public_key
        + state.ephemeral_public_key_bytes
        + state.local_nonce
    )
    if not verify_signature(remote_pubkey, signed_data, ack.signature):
        log.warning("Invalid HELLO_ACK signature from %s", remote_peer_id.hex()[:8])
        return None

    # 3. Compute shared secret
    local_eph_priv = _x25519_priv_from_raw(state.ephemeral_private_key_bytes)
    remote_eph_pub = _x25519_pub_from_raw(ack.ephemeral_public_key)
    shared_secret = compute_shared_secret(local_eph_priv, remote_eph_pub)

    # 4. Derive session key
    session_key = derive_session_key(shared_secret, state.local_nonce, ack.random_nonce)
    return SessionKeys(
        encryption_key=session_key,
        session_id=ack.session_id,
        remote_peer_id=remote_peer_id,
        remote_public_key_bytes=ack.identity_public_key,
    )


# ---------------------------------------------------------------------------
# Receiver
# ---------------------------------------------------------------------------

def process_hello(
    packet: Packet,
    known_peers: set[bytes],
    local_identity: LocalIdentity,
) -> tuple[Packet, SessionKeys] | None:
    """Process a received HELLO (receiver side).

    Returns ``(hello_ack_packet, session_keys)`` on success, ``None`` if the
    peer is unknown or signature invalid.
    """
    hello = HelloPayload.deserialize(packet.payload)

    # 1. Derive peer_id from the identity key in the HELLO
    remote_pubkey = _ed25519_from_raw(hello.identity_public_key)
    remote_peer_id = derive_peer_id(remote_pubkey)

    # 2. Check peer is in DirectPeers — reject unknown
    if remote_peer_id not in known_peers:
        log.warning("HELLO from unknown peer %s — rejected", remote_peer_id.hex()[:8])
        return None

    # 3. Verify signature: sign(identity_private, ephemeral_pub || nonce)
    signed_data = hello.ephemeral_public_key + hello.random_nonce
    if not verify_signature(remote_pubkey, signed_data, hello.signature):
        log.warning("Invalid HELLO signature from %s", remote_peer_id.hex()[:8])
        return None

    # 4. Generate our own ephemeral key + nonce
    eph_priv, eph_pub = generate_ephemeral_keypair()
    our_nonce = generate_nonce_16()
    eph_pub_bytes = eph_pub.public_bytes(Encoding.Raw, PublicFormat.Raw)

    # 5. Compute shared secret
    remote_eph_pub = _x25519_pub_from_raw(hello.ephemeral_public_key)
    shared_secret = compute_shared_secret(eph_priv, remote_eph_pub)

    # 6. Derive session key (initiator nonce first, then receiver nonce)
    session_key = derive_session_key(shared_secret, hello.random_nonce, our_nonce)
    session_id = generate_session_id()

    # 7. Sign HELLO_ACK: sign(identity_private, session_id || our_eph || remote_eph || remote_nonce)
    ack_signed_data = session_id + eph_pub_bytes + hello.ephemeral_public_key + hello.random_nonce
    ack_sig = local_identity.sign(ack_signed_data)

    ack_payload = HelloAckPayload(
        identity_public_key=local_identity.public_key_bytes,
        ephemeral_public_key=eph_pub_bytes,
        session_id=session_id,
        random_nonce=our_nonce,
        signature=ack_sig,
    )

    keys = SessionKeys(
        encryption_key=session_key,
        session_id=session_id,
        remote_peer_id=remote_peer_id,
        remote_public_key_bytes=hello.identity_public_key,
    )

    return make_packet(PacketType.HELLO_ACK, ack_payload), keys
