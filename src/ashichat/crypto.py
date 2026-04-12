"""Cryptographic primitives for AshiChat.

All functions are synchronous (pure computation, no I/O).

Primitives:
    - X25519 ephemeral key exchange
    - HKDF-SHA256 key derivation
    - AES-256-GCM authenticated encryption
    - Nonce builder (session_id || sequence_number)
    - Secure random helpers
"""

from __future__ import annotations

import os
import struct

from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


# ---------------------------------------------------------------------------
# X25519 ephemeral key generation
# ---------------------------------------------------------------------------

def generate_ephemeral_keypair() -> tuple[X25519PrivateKey, X25519PublicKey]:
    """Generate an ephemeral X25519 keypair for one handshake."""
    private = X25519PrivateKey.generate()
    return private, private.public_key()


def compute_shared_secret(
    local_private: X25519PrivateKey,
    remote_public: X25519PublicKey,
) -> bytes:
    """``shared_secret = X25519(local_ephemeral, remote_ephemeral)``."""
    return local_private.exchange(remote_public)


# ---------------------------------------------------------------------------
# HKDF key derivation
# ---------------------------------------------------------------------------

_SESSION_INFO = b"ashichat-session-v1"


def derive_session_key(
    shared_secret: bytes,
    initiator_nonce: bytes,
    receiver_nonce: bytes,
) -> bytes:
    """Derive a 32-byte session key via HKDF-SHA256.

    ``salt = initiator_nonce || receiver_nonce``
    ``info = b"ashichat-session-v1"``
    """
    hkdf = HKDF(
        algorithm=SHA256(),
        length=32,
        salt=initiator_nonce + receiver_nonce,
        info=_SESSION_INFO,
    )
    return hkdf.derive(shared_secret)


# ---------------------------------------------------------------------------
# AES-256-GCM encryption
# ---------------------------------------------------------------------------

def encrypt(
    key: bytes,
    plaintext: bytes,
    nonce: bytes,
    aad: bytes,
) -> tuple[bytes, bytes]:
    """AES-256-GCM encrypt.

    Returns ``(ciphertext, auth_tag)``.  The ``cryptography`` library appends
    the 16-byte tag to the ciphertext; we split it off for the wire format.
    """
    aesgcm = AESGCM(key)
    ct_with_tag = aesgcm.encrypt(nonce, plaintext, aad)
    # tag is the last 16 bytes
    ciphertext = ct_with_tag[:-16]
    auth_tag = ct_with_tag[-16:]
    return ciphertext, auth_tag


def decrypt(
    key: bytes,
    ciphertext: bytes,
    nonce: bytes,
    aad: bytes,
    auth_tag: bytes,
) -> bytes:
    """AES-256-GCM decrypt.

    Re-appends the tag for the ``cryptography`` library, which expects
    ``ciphertext || tag``.  Raises ``InvalidTag`` on failure.
    """
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ciphertext + auth_tag, aad)


# ---------------------------------------------------------------------------
# Nonce builder
# ---------------------------------------------------------------------------

def build_nonce(session_id: bytes, sequence_number: int) -> bytes:
    """Build a 12-byte nonce: ``session_id (8) || sequence_number (4 BE)``."""
    assert len(session_id) == 8, f"session_id must be 8 bytes, got {len(session_id)}"
    return session_id + struct.pack(">I", sequence_number)


# ---------------------------------------------------------------------------
# Secure random helpers
# ---------------------------------------------------------------------------

def random_bytes(n: int) -> bytes:
    """Return *n* cryptographically secure random bytes."""
    return os.urandom(n)


def generate_session_id() -> bytes:
    """Return a cryptographically random 8-byte session ID."""
    return os.urandom(8)


def generate_nonce_16() -> bytes:
    """Return a cryptographically random 16-byte nonce (for handshake)."""
    return os.urandom(16)
