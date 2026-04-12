"""Async UDP transport for AshiChat.

Uses ``asyncio.DatagramProtocol`` for non-blocking I/O.
Receives raw bytes → deserializes → dispatches to a packet handler.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Callable, Any

from ashichat.logging_setup import get_logger
from ashichat.packet import Packet, PacketError

log = get_logger(__name__)


class UDPTransport(asyncio.DatagramProtocol):
    """Async UDP protocol that dispatches parsed packets."""

    def __init__(
        self,
        packet_handler: Callable[[Packet, tuple[str, int]], Any],
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self._packet_handler = packet_handler
        self._transport: asyncio.DatagramTransport | None = None
        self._loop = loop

    # -- Protocol callbacks --------------------------------------------------

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:  # type: ignore[override]
        self._transport = transport
        sock = transport.get_extra_info("sockname")
        log.info("UDP listener bound to %s", sock)

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            packet = Packet.deserialize(data)
        except PacketError as e:
            log.debug("Dropped malformed packet from %s: %s", addr, e)
            return

        # Schedule handler as a task so it doesn't block the protocol
        loop = self._loop or asyncio.get_event_loop()
        if inspect.iscoroutinefunction(self._packet_handler):
            loop.create_task(self._packet_handler(packet, addr))
        else:
            self._packet_handler(packet, addr)

    def error_received(self, exc: Exception) -> None:
        log.warning("UDP error: %s", exc)

    def connection_lost(self, exc: Exception | None) -> None:
        if exc:
            log.warning("UDP connection lost: %s", exc)

    # -- Public send interface -----------------------------------------------

    def send_packet(self, packet: Packet, addr: tuple[str, int]) -> None:
        """Serialize and send a packet via UDP (fire-and-forget)."""
        if self._transport is None:
            log.error("Cannot send — transport not ready")
            return
        self._transport.sendto(packet.serialize(), addr)

    def close(self) -> None:
        """Close the underlying transport."""
        if self._transport is not None:
            self._transport.close()


# ---------------------------------------------------------------------------
# Lifecycle helpers
# ---------------------------------------------------------------------------

async def start_udp_listener(
    port: int,
    packet_handler: Callable[[Packet, tuple[str, int]], Any],
    bind_addr: str = "0.0.0.0",
) -> tuple[asyncio.DatagramTransport, UDPTransport]:
    """Bind a UDP listener and return ``(transport, protocol)``."""
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: UDPTransport(packet_handler, loop),
        local_addr=(bind_addr, port),
    )
    return transport, protocol


async def stop_udp_listener(protocol: UDPTransport) -> None:
    """Gracefully shut down the listener."""
    protocol.close()
