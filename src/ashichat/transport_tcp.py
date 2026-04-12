"""TCP fallback transport — NOT IMPLEMENTED in v1.

Stub with interface compatibility.  Raises ``NotImplementedError`` on use.
"""

from __future__ import annotations

from ashichat.packet import Packet


class TCPTransport:
    """TCP fallback transport — not implemented in v1."""

    async def connect(self, addr: tuple[str, int]) -> None:
        raise NotImplementedError("TCP fallback not implemented in v1")

    async def send_packet(self, packet: Packet, addr: tuple[str, int]) -> None:
        raise NotImplementedError("TCP fallback not implemented in v1")

    def close(self) -> None:
        pass
