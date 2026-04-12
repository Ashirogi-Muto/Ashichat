"""Identity management for AshiChat.

Each device has a long-term Ed25519 keypair.  The public key uniquely defines
device identity:  ``peer_id = SHA-256(public_key_raw_bytes)``.

Private key is stored **unencrypted** in v1 with **chmod 600** permissions.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    BestAvailableEncryption,
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from cryptography.exceptions import InvalidSignature

from ashichat.logging_setup import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Peer ID derivation
# ---------------------------------------------------------------------------

def derive_peer_id(public_key: Ed25519PublicKey) -> bytes:
    """``peer_id = SHA-256(raw_public_key_bytes)``  → 32 bytes."""
    raw = public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
    return hashlib.sha256(raw).digest()


def fingerprint(peer_id: bytes) -> str:
    """Human-readable short fingerprint (first 8 hex chars)."""
    return peer_id[:4].hex()


# ---------------------------------------------------------------------------
# LocalIdentity
# ---------------------------------------------------------------------------

@dataclass
class LocalIdentity:
    """The node's own long-term identity."""

    private_key: Ed25519PrivateKey
    public_key: Ed25519PublicKey
    peer_id: bytes  # SHA-256(public_key)

    def sign(self, data: bytes) -> bytes:
        """Sign *data* with our Ed25519 private key.  Returns 64-byte signature."""
        return self.private_key.sign(data)

    def fingerprint(self) -> str:
        """Short human-readable form of our peer ID."""
        return fingerprint(self.peer_id)

    @property
    def public_key_bytes(self) -> bytes:
        """Raw 32-byte public key."""
        return self.public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)


# ---------------------------------------------------------------------------
# Signature verification (for remote peers)
# ---------------------------------------------------------------------------

def verify_signature(
    public_key: Ed25519PublicKey,
    data: bytes,
    signature: bytes,
) -> bool:
    """Verify an Ed25519 *signature* over *data*.  Returns ``True`` / ``False``."""
    try:
        public_key.verify(signature, data)
        return True
    except InvalidSignature:
        return False


# ---------------------------------------------------------------------------
# Key generation / persistence
# ---------------------------------------------------------------------------

def generate_identity() -> LocalIdentity:
    """Generate a fresh Ed25519 keypair and derive the peer ID."""
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    peer_id = derive_peer_id(public_key)
    log.info("Generated new identity — fingerprint %s", fingerprint(peer_id))
    return LocalIdentity(private_key=private_key, public_key=public_key, peer_id=peer_id)


def _restrict_permissions(path: Path) -> None:
    """Best-effort chmod 600 on the private key file.

    On Windows ``os.chmod`` is limited, but we set it anyway for the platforms
    that honour it.
    """
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
    except OSError:
        log.warning("Could not restrict permissions on %s", path)


def save_identity(identity: LocalIdentity, base_dir: Path) -> None:
    """Persist keypair to ``base_dir/identity/``."""
    identity_dir = base_dir / "identity"
    identity_dir.mkdir(parents=True, exist_ok=True)

    priv_path = identity_dir / "private_key.pem"
    pub_path = identity_dir / "public_key.pem"

    # Private key — PEM, unencrypted for v1
    priv_pem = identity.private_key.private_bytes(
        Encoding.PEM,
        PrivateFormat.PKCS8,
        NoEncryption(),
    )
    priv_path.write_bytes(priv_pem)
    _restrict_permissions(priv_path)

    # Public key — PEM
    pub_pem = identity.public_key.public_bytes(
        Encoding.PEM,
        PublicFormat.SubjectPublicKeyInfo,
    )
    pub_path.write_bytes(pub_pem)

    log.info("Identity saved to %s", identity_dir)


def load_identity(base_dir: Path) -> LocalIdentity:
    """Load existing keypair from ``base_dir/identity/``.

    Raises ``FileNotFoundError`` if the key files don't exist.
    """
    priv_path = base_dir / "identity" / "private_key.pem"
    priv_pem = priv_path.read_bytes()

    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    private_key = load_pem_private_key(priv_pem, password=None)
    if not isinstance(private_key, Ed25519PrivateKey):
        raise TypeError("Expected Ed25519 private key")

    public_key = private_key.public_key()
    peer_id = derive_peer_id(public_key)
    log.info("Loaded identity — fingerprint %s", fingerprint(peer_id))
    return LocalIdentity(private_key=private_key, public_key=public_key, peer_id=peer_id)


def load_or_generate_identity(base_dir: Path) -> LocalIdentity:
    """Load identity if it exists, otherwise generate and save a new one."""
    try:
        return load_identity(base_dir)
    except FileNotFoundError:
        identity = generate_identity()
        save_identity(identity, base_dir)
        return identity
