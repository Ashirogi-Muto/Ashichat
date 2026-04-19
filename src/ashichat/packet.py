"""Binary wire protocol for AshiChat.

All network packets conform to a binary framing structure — no plaintext JSON
is permitted over the wire.

Header:  [version: 1B][type: 1B][payload_length: 2B BE]
Payload: [N bytes]

Max payload size: 64 KB.
"""

from __future__ import annotations

import ipaddress
import struct
from dataclasses import dataclass
from enum import IntEnum


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROTOCOL_VERSION = 0x01
MAX_PAYLOAD_SIZE = 65535  # 64 KB (2-byte length field max)
MAX_TTL = 5
HEADER_SIZE = 4  # version + type + 2-byte length


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class PacketError(Exception):
    """Raised on any parsing / validation failure."""


# ---------------------------------------------------------------------------
# Packet types
# ---------------------------------------------------------------------------

class PacketType(IntEnum):
    HELLO = 0x01
    HELLO_ACK = 0x02
    DATA = 0x03
    ACK = 0x04
    ENDPOINT_UPDATE = 0x05
    RESOLVE_REQUEST = 0x06
    PING = 0x07
    PONG = 0x08
    SYNC = 0x09
    APP_ACK = 0x0A


_VALID_TYPES = set(PacketType)


# ---------------------------------------------------------------------------
# Top-level Packet
# ---------------------------------------------------------------------------

@dataclass
class Packet:
    """Top-level wire packet."""

    version: int
    packet_type: PacketType
    payload: bytes

    def serialize(self) -> bytes:
        """Serialize to wire format."""
        length = len(self.payload)
        header = struct.pack(">BBH", self.version, self.packet_type, length)
        return header + self.payload

    @classmethod
    def deserialize(cls, data: bytes) -> Packet:
        """Parse raw bytes into a Packet.  Raises ``PacketError`` on failure."""
        if len(data) < HEADER_SIZE:
            raise PacketError(
                f"Data too short for header: {len(data)} < {HEADER_SIZE}"
            )

        version, ptype, length = struct.unpack(">BBH", data[:HEADER_SIZE])

        if version != PROTOCOL_VERSION:
            raise PacketError(f"Unknown version: 0x{version:02x}")

        if ptype not in _VALID_TYPES:
            raise PacketError(f"Unknown packet type: 0x{ptype:02x}")

        if length > MAX_PAYLOAD_SIZE:
            raise PacketError(f"Payload too large: {length} > {MAX_PAYLOAD_SIZE}")

        payload = data[HEADER_SIZE:]
        if len(payload) < length:
            raise PacketError(
                f"Truncated payload: declared {length}, got {len(payload)}"
            )

        return cls(
            version=version,
            packet_type=PacketType(ptype),
            payload=payload[:length],
        )


# ---------------------------------------------------------------------------
# Typed payloads
# ---------------------------------------------------------------------------

@dataclass
class HelloPayload:
    """HELLO packet payload (initiator → receiver)."""

    identity_public_key: bytes  # 32 bytes
    ephemeral_public_key: bytes  # 32 bytes
    random_nonce: bytes  # 16 bytes
    signature: bytes  # 64 bytes

    _SIZE = 32 + 32 + 16 + 64  # 144 bytes

    def serialize(self) -> bytes:
        return (
            self.identity_public_key
            + self.ephemeral_public_key
            + self.random_nonce
            + self.signature
        )

    @classmethod
    def deserialize(cls, data: bytes) -> HelloPayload:
        if len(data) < cls._SIZE:
            raise PacketError(f"HelloPayload too short: {len(data)} < {cls._SIZE}")
        return cls(
            identity_public_key=data[0:32],
            ephemeral_public_key=data[32:64],
            random_nonce=data[64:80],
            signature=data[80:144],
        )


@dataclass
class HelloAckPayload:
    """HELLO_ACK packet payload (receiver → initiator)."""

    identity_public_key: bytes  # 32
    ephemeral_public_key: bytes  # 32
    session_id: bytes  # 8
    random_nonce: bytes  # 16
    signature: bytes  # 64

    _SIZE = 152

    def serialize(self) -> bytes:
        return (
            self.identity_public_key
            + self.ephemeral_public_key
            + self.session_id
            + self.random_nonce
            + self.signature
        )

    @classmethod
    def deserialize(cls, data: bytes) -> HelloAckPayload:
        if len(data) < cls._SIZE:
            raise PacketError(f"HelloAckPayload too short: {len(data)} < {cls._SIZE}")
        return cls(
            identity_public_key=data[0:32],
            ephemeral_public_key=data[32:64],
            session_id=data[64:72],
            random_nonce=data[72:88],
            signature=data[88:152],
        )


@dataclass
class DataPayload:
    """DATA packet payload — encrypted message."""

    session_id: bytes  # 8
    sequence_number: int  # 4 (unsigned)
    ciphertext: bytes
    auth_tag: bytes  # 16

    _HEADER_SIZE = 8 + 4 + 4 + 16  # session_id + seq + ct_len + tag = 32

    def serialize(self) -> bytes:
        return (
            self.session_id
            + struct.pack(">I", self.sequence_number)
            + struct.pack(">I", len(self.ciphertext))
            + self.ciphertext
            + self.auth_tag
        )

    @classmethod
    def deserialize(cls, data: bytes) -> DataPayload:
        if len(data) < 28:  # 8 + 4 + 4 + 0 + 16 minimum (empty ciphertext + tag)
            raise PacketError(f"DataPayload too short: {len(data)}")
        session_id = data[0:8]
        seq = struct.unpack(">I", data[8:12])[0]
        ct_len = struct.unpack(">I", data[12:16])[0]
        if len(data) < 16 + ct_len + 16:
            raise PacketError("DataPayload truncated ciphertext/tag")
        ciphertext = data[16 : 16 + ct_len]
        auth_tag = data[16 + ct_len : 16 + ct_len + 16]
        return cls(
            session_id=session_id,
            sequence_number=seq,
            ciphertext=ciphertext,
            auth_tag=auth_tag,
        )


@dataclass
class AckPayload:
    """ACK packet payload."""

    session_id: bytes  # 8
    ack_sequence_number: int  # 4

    _SIZE = 12

    def serialize(self) -> bytes:
        return self.session_id + struct.pack(">I", self.ack_sequence_number)

    @classmethod
    def deserialize(cls, data: bytes) -> AckPayload:
        if len(data) < cls._SIZE:
            raise PacketError(f"AckPayload too short: {len(data)} < {cls._SIZE}")
        return cls(
            session_id=data[0:8],
            ack_sequence_number=struct.unpack(">I", data[8:12])[0],
        )


@dataclass
class EndpointUpdatePayload:
    """ENDPOINT_UPDATE payload — signed endpoint change notification."""

    peer_id: bytes  # 32
    endpoint_ip: str
    endpoint_port: int  # 2
    version_counter: int  # 8 (unsigned)
    signature: bytes  # 64

    def serialize(self) -> bytes:
        try:
            addr = ipaddress.ip_address(self.endpoint_ip)
        except ValueError as e:
            raise PacketError(f"Invalid IP address: {self.endpoint_ip}") from e

        if isinstance(addr, ipaddress.IPv4Address):
            ip_type = 0x04
            ip_bytes = addr.packed  # 4 bytes
        else:
            ip_type = 0x06
            ip_bytes = addr.packed  # 16 bytes

        return (
            self.peer_id
            + struct.pack(">B", ip_type)
            + ip_bytes
            + struct.pack(">H", self.endpoint_port)
            + struct.pack(">Q", self.version_counter)
            + self.signature
        )

    @classmethod
    def deserialize(cls, data: bytes) -> EndpointUpdatePayload:
        if len(data) < 32 + 1:
            raise PacketError("EndpointUpdatePayload too short")
        peer_id = data[0:32]
        ip_type = data[32]
        offset = 33

        if ip_type == 0x04:
            if len(data) < offset + 4:
                raise PacketError("EndpointUpdate truncated IPv4")
            ip_str = str(ipaddress.IPv4Address(data[offset : offset + 4]))
            offset += 4
        elif ip_type == 0x06:
            if len(data) < offset + 16:
                raise PacketError("EndpointUpdate truncated IPv6")
            ip_str = str(ipaddress.IPv6Address(data[offset : offset + 16]))
            offset += 16
        else:
            raise PacketError(f"Unknown IP type: 0x{ip_type:02x}")

        if len(data) < offset + 2 + 8 + 64:
            raise PacketError("EndpointUpdate truncated port/counter/sig")

        port = struct.unpack(">H", data[offset : offset + 2])[0]
        offset += 2
        counter = struct.unpack(">Q", data[offset : offset + 8])[0]
        offset += 8
        sig = data[offset : offset + 64]

        return cls(
            peer_id=peer_id,
            endpoint_ip=ip_str,
            endpoint_port=port,
            version_counter=counter,
            signature=sig,
        )


@dataclass
class ResolveRequestPayload:
    """RESOLVE_REQUEST payload."""

    request_id: bytes  # 16
    target_peer_id: bytes  # 32
    ttl: int  # 1 byte, max 5

    _SIZE = 16 + 32 + 1  # 49

    def serialize(self) -> bytes:
        clamped = min(self.ttl, MAX_TTL)
        return self.request_id + self.target_peer_id + struct.pack(">B", clamped)

    @classmethod
    def deserialize(cls, data: bytes) -> ResolveRequestPayload:
        if len(data) < cls._SIZE:
            raise PacketError(
                f"ResolveRequestPayload too short: {len(data)} < {cls._SIZE}"
            )
        ttl = data[48]
        return cls(
            request_id=data[0:16],
            target_peer_id=data[16:48],
            ttl=min(ttl, MAX_TTL),
        )


@dataclass
class PingPayload:
    """PING payload."""

    session_id: bytes  # 8
    ping_id: bytes  # 8

    _SIZE = 16

    def serialize(self) -> bytes:
        return self.session_id + self.ping_id

    @classmethod
    def deserialize(cls, data: bytes) -> PingPayload:
        if len(data) < cls._SIZE:
            raise PacketError(f"PingPayload too short: {len(data)} < {cls._SIZE}")
        return cls(session_id=data[0:8], ping_id=data[8:16])


@dataclass
class PongPayload:
    """PONG payload (echoes ping_id)."""

    session_id: bytes  # 8
    ping_id: bytes  # 8

    _SIZE = 16

    def serialize(self) -> bytes:
        return self.session_id + self.ping_id

    @classmethod
    def deserialize(cls, data: bytes) -> PongPayload:
        if len(data) < cls._SIZE:
            raise PacketError(f"PongPayload too short: {len(data)} < {cls._SIZE}")
        return cls(session_id=data[0:8], ping_id=data[8:16])


@dataclass
class SyncPayload:
    """SYNC payload — I_HAVE_UP_TO(sequence_number) for chat synchronization."""

    session_id: bytes  # 8
    sequence_number: int  # 4 (highest sequence we've received)

    _SIZE = 12

    def serialize(self) -> bytes:
        return self.session_id + struct.pack(">I", self.sequence_number)

    @classmethod
    def deserialize(cls, data: bytes) -> SyncPayload:
        if len(data) < cls._SIZE:
            raise PacketError(f"SyncPayload too short: {len(data)} < {cls._SIZE}")
        return cls(
            session_id=data[0:8],
            sequence_number=struct.unpack(">I", data[8:12])[0],
        )


@dataclass
class AppAckPayload:
    """APP_ACK payload — application-level acknowledgement (message displayed)."""

    session_id: bytes  # 8
    ack_sequence_number: int  # 4

    _SIZE = 12

    def serialize(self) -> bytes:
        return self.session_id + struct.pack(">I", self.ack_sequence_number)

    @classmethod
    def deserialize(cls, data: bytes) -> AppAckPayload:
        if len(data) < cls._SIZE:
            raise PacketError(f"AppAckPayload too short: {len(data)} < {cls._SIZE}")
        return cls(
            session_id=data[0:8],
            ack_sequence_number=struct.unpack(">I", data[8:12])[0],
        )


# ---------------------------------------------------------------------------
# Helper: wrap a typed payload into a Packet
# ---------------------------------------------------------------------------

def make_packet(
    packet_type: PacketType,
    payload_obj: (
        HelloPayload
        | HelloAckPayload
        | DataPayload
        | AckPayload
        | EndpointUpdatePayload
        | ResolveRequestPayload
        | PingPayload
        | PongPayload
        | SyncPayload
        | AppAckPayload
    ),
) -> Packet:
    """Convenience: build a ``Packet`` from a typed payload."""
    return Packet(
        version=PROTOCOL_VERSION,
        packet_type=packet_type,
        payload=payload_obj.serialize(),
    )
