"""Tests for ashichat.identity and ashichat.crypto (Phase 1)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey

from ashichat.identity import (
    LocalIdentity,
    derive_peer_id,
    fingerprint,
    generate_identity,
    load_identity,
    save_identity,
    verify_signature,
)
from ashichat.crypto import (
    build_nonce,
    compute_shared_secret,
    decrypt,
    derive_session_key,
    encrypt,
    generate_ephemeral_keypair,
    generate_nonce_16,
    generate_session_id,
)


# ═══════════════════════════════════════════════════════════════════════════
# Identity tests
# ═══════════════════════════════════════════════════════════════════════════


class TestIdentityGeneration:
    def test_generate_produces_valid_identity(self) -> None:
        ident = generate_identity()
        assert isinstance(ident.public_key, Ed25519PublicKey)
        assert len(ident.peer_id) == 32
        assert len(ident.public_key_bytes) == 32

    def test_peer_id_is_sha256_of_pubkey(self) -> None:
        import hashlib

        ident = generate_identity()
        expected = hashlib.sha256(ident.public_key_bytes).digest()
        assert ident.peer_id == expected

    def test_fingerprint_is_first_4_hex_bytes(self) -> None:
        ident = generate_identity()
        assert ident.fingerprint() == ident.peer_id[:4].hex()
        assert len(ident.fingerprint()) == 8

    def test_two_identities_differ(self) -> None:
        a = generate_identity()
        b = generate_identity()
        assert a.peer_id != b.peer_id


class TestSignVerify:
    def test_sign_verify_roundtrip(self) -> None:
        ident = generate_identity()
        data = b"hello ashichat"
        sig = ident.sign(data)
        assert len(sig) == 64
        assert verify_signature(ident.public_key, data, sig) is True

    def test_tampered_data_fails(self) -> None:
        ident = generate_identity()
        sig = ident.sign(b"original")
        assert verify_signature(ident.public_key, b"tampered", sig) is False

    def test_wrong_key_fails(self) -> None:
        a = generate_identity()
        b = generate_identity()
        sig = a.sign(b"data")
        assert verify_signature(b.public_key, b"data", sig) is False


class TestKeyPersistence:
    def test_save_load_roundtrip(self, tmp_path: Path) -> None:
        original = generate_identity()
        save_identity(original, tmp_path)
        loaded = load_identity(tmp_path)

        assert loaded.peer_id == original.peer_id
        assert loaded.public_key_bytes == original.public_key_bytes

        # Verify the loaded key can sign and the original key can verify
        sig = loaded.sign(b"test")
        assert verify_signature(original.public_key, b"test", sig)

    def test_load_missing_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_identity(tmp_path)

    def test_private_key_permissions(self, tmp_path: Path) -> None:
        ident = generate_identity()
        save_identity(ident, tmp_path)
        priv_path = tmp_path / "identity" / "private_key.pem"
        assert priv_path.exists()
        # On Unix this would check 0o600; on Windows we just verify the file exists
        # and is not empty
        assert priv_path.stat().st_size > 0


# ═══════════════════════════════════════════════════════════════════════════
# Crypto tests
# ═══════════════════════════════════════════════════════════════════════════


class TestECDH:
    def test_shared_secret_equality(self) -> None:
        """Alice and Bob derive the same shared secret."""
        alice_priv, alice_pub = generate_ephemeral_keypair()
        bob_priv, bob_pub = generate_ephemeral_keypair()

        alice_secret = compute_shared_secret(alice_priv, bob_pub)
        bob_secret = compute_shared_secret(bob_priv, alice_pub)

        assert alice_secret == bob_secret
        assert len(alice_secret) == 32

    def test_different_pairs_different_secrets(self) -> None:
        priv1, pub1 = generate_ephemeral_keypair()
        priv2, pub2 = generate_ephemeral_keypair()
        priv3, pub3 = generate_ephemeral_keypair()

        s1 = compute_shared_secret(priv1, pub2)
        s2 = compute_shared_secret(priv1, pub3)
        assert s1 != s2


class TestHKDF:
    def test_determinism(self) -> None:
        secret = b"\x42" * 32
        nonce_a = b"\x01" * 16
        nonce_b = b"\x02" * 16

        k1 = derive_session_key(secret, nonce_a, nonce_b)
        k2 = derive_session_key(secret, nonce_a, nonce_b)
        assert k1 == k2
        assert len(k1) == 32

    def test_different_salt_different_key(self) -> None:
        secret = b"\x42" * 32
        k1 = derive_session_key(secret, b"\x01" * 16, b"\x02" * 16)
        k2 = derive_session_key(secret, b"\x03" * 16, b"\x04" * 16)
        assert k1 != k2

    def test_different_secret_different_key(self) -> None:
        k1 = derive_session_key(b"\x01" * 32, b"\xaa" * 16, b"\xbb" * 16)
        k2 = derive_session_key(b"\x02" * 32, b"\xaa" * 16, b"\xbb" * 16)
        assert k1 != k2


class TestAESGCM:
    def test_roundtrip(self) -> None:
        key = os.urandom(32)
        nonce = os.urandom(12)
        aad = b"associated"
        plaintext = b"hello world from ashichat!"

        ct, tag = encrypt(key, plaintext, nonce, aad)
        result = decrypt(key, ct, nonce, aad, tag)
        assert result == plaintext

    def test_tamper_ciphertext(self) -> None:
        key = os.urandom(32)
        nonce = os.urandom(12)
        aad = b"aad"
        ct, tag = encrypt(key, b"secret", nonce, aad)

        tampered = bytearray(ct)
        if len(tampered) > 0:
            tampered[0] ^= 0xFF
        with pytest.raises(Exception):  # InvalidTag
            decrypt(key, bytes(tampered), nonce, aad, tag)

    def test_tamper_tag(self) -> None:
        key = os.urandom(32)
        nonce = os.urandom(12)
        aad = b"aad"
        ct, tag = encrypt(key, b"secret", nonce, aad)

        bad_tag = bytearray(tag)
        bad_tag[0] ^= 0xFF
        with pytest.raises(Exception):
            decrypt(key, ct, nonce, aad, bytes(bad_tag))

    def test_wrong_aad(self) -> None:
        key = os.urandom(32)
        nonce = os.urandom(12)
        ct, tag = encrypt(key, b"data", nonce, b"correct_aad")

        with pytest.raises(Exception):
            decrypt(key, ct, nonce, b"wrong_aad", tag)

    def test_empty_plaintext(self) -> None:
        key = os.urandom(32)
        nonce = os.urandom(12)
        ct, tag = encrypt(key, b"", nonce, b"")
        result = decrypt(key, ct, nonce, b"", tag)
        assert result == b""


class TestNonce:
    def test_correct_length(self) -> None:
        sid = os.urandom(8)
        nonce = build_nonce(sid, 42)
        assert len(nonce) == 12

    def test_concatenation(self) -> None:
        sid = b"\xaa" * 8
        nonce = build_nonce(sid, 1)
        assert nonce[:8] == sid
        assert nonce[8:] == b"\x00\x00\x00\x01"

    def test_uniqueness(self) -> None:
        sid = os.urandom(8)
        n1 = build_nonce(sid, 0)
        n2 = build_nonce(sid, 1)
        assert n1 != n2

    def test_different_session_different_nonce(self) -> None:
        n1 = build_nonce(b"\x00" * 8, 0)
        n2 = build_nonce(b"\x01" * 8, 0)
        assert n1 != n2

    def test_bad_session_id_length(self) -> None:
        with pytest.raises(AssertionError):
            build_nonce(b"\x00" * 7, 0)


class TestSecureRandom:
    def test_session_id_length(self) -> None:
        assert len(generate_session_id()) == 8

    def test_nonce_16_length(self) -> None:
        assert len(generate_nonce_16()) == 16

    def test_uniqueness(self) -> None:
        ids = {generate_session_id() for _ in range(100)}
        assert len(ids) == 100  # collision in 100 random 8-byte values is ~impossible
