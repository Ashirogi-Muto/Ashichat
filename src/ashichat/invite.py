"""Invite code generation and parsing for AshiChat.

Two encoding modes:
    Base85 (default, compact): ashichat://v1:<base85_payload>
    Base32 (human-readable):   ashichat://v1.h:<base32_payload>

Payload: [pubkey:32][ip_type:1][ip:4or16][port:2]
    ip_type 0x04 = IPv4, 0x06 = IPv6
    If no endpoint, payload is just [pubkey:32] (length 32)
"""

from __future__ import annotations

import base64
import ipaddress
import struct
from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from ashichat.logging_setup import get_logger

log = get_logger(__name__)

_PREFIX_85 = "ashichat://v1:"
_PREFIX_32 = "ashichat://v1.h:"


class InviteError(Exception):
    """Invalid invite code."""


@dataclass
class InviteData:
    """Parsed invite."""

    public_key: Ed25519PublicKey
    endpoint: tuple[str, int] | None


# ---------------------------------------------------------------------------
# Payload encoding
# ---------------------------------------------------------------------------

def _build_payload(
    public_key: Ed25519PublicKey,
    endpoint: tuple[str, int] | None,
) -> bytes:
    raw = public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)

    if endpoint is None:
        return raw

    host, port = endpoint
    addr = ipaddress.ip_address(host)
    if isinstance(addr, ipaddress.IPv4Address):
        return raw + b"\x04" + addr.packed + struct.pack(">H", port)
    else:
        return raw + b"\x06" + addr.packed + struct.pack(">H", port)


def _parse_payload(data: bytes) -> InviteData:
    if len(data) < 32:
        raise InviteError(f"Payload too short: {len(data)} bytes")

    pubkey_raw = data[:32]
    try:
        pubkey = Ed25519PublicKey.from_public_bytes(pubkey_raw)
    except Exception as e:
        raise InviteError(f"Invalid public key: {e}") from e

    # No endpoint
    if len(data) == 32:
        return InviteData(public_key=pubkey, endpoint=None)

    # Has endpoint
    if len(data) < 33:
        raise InviteError("Truncated endpoint data")

    ip_type = data[32]
    offset = 33

    if ip_type == 0x04:
        if len(data) < offset + 4 + 2:
            raise InviteError("Truncated IPv4 endpoint")
        ip_str = str(ipaddress.IPv4Address(data[offset : offset + 4]))
        offset += 4
    elif ip_type == 0x06:
        if len(data) < offset + 16 + 2:
            raise InviteError("Truncated IPv6 endpoint")
        ip_str = str(ipaddress.IPv6Address(data[offset : offset + 16]))
        offset += 16
    else:
        raise InviteError(f"Unknown IP type: 0x{ip_type:02x}")

    port = struct.unpack(">H", data[offset : offset + 2])[0]
    return InviteData(public_key=pubkey, endpoint=(ip_str, port))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_invite(
    public_key: Ed25519PublicKey,
    endpoint: tuple[str, int] | None = None,
) -> str:
    """Generate a Base85 invite code (compact, default).

    Format: ``ashichat://v1:<base85_payload>``
    """
    payload = _build_payload(public_key, endpoint)
    encoded = base64.b85encode(payload).decode("ascii")
    return f"{_PREFIX_85}{encoded}"


def generate_invite_readable(
    public_key: Ed25519PublicKey,
    endpoint: tuple[str, int] | None = None,
) -> str:
    """Generate a Base32 invite code (human-readable fallback).

    Format: ``ashichat://v1.h:<base32_payload>``
    """
    payload = _build_payload(public_key, endpoint)
    encoded = base64.b32encode(payload).decode("ascii")
    return f"{_PREFIX_32}{encoded}"


def parse_invite(invite_str: str) -> InviteData:
    """Parse an invite code (auto-detects Base85 vs Base32).

    Accepts:
        - ``ashichat://v1:<base85_payload>``
        - ``ashichat://v1.h:<base32_payload>``

    Returns ``InviteData`` with public key and optional endpoint.
    Raises ``InviteError`` on invalid format.
    """
    invite_str = invite_str.strip()

    if invite_str.startswith(_PREFIX_32):
        encoded = invite_str[len(_PREFIX_32):]
        try:
            payload = base64.b32decode(encoded)
        except Exception as e:
            raise InviteError(f"Invalid Base32: {e}") from e
    elif invite_str.startswith(_PREFIX_85):
        encoded = invite_str[len(_PREFIX_85):]
        try:
            payload = base64.b85decode(encoded)
        except Exception as e:
            raise InviteError(f"Invalid Base85: {e}") from e
    else:
        raise InviteError(
            f"Invalid invite prefix. Expected '{_PREFIX_85}' or '{_PREFIX_32}'"
        )

    return _parse_payload(payload)
