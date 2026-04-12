"""NAT traversal for AshiChat — UDP hole punching.

No external STUN server. Symmetric NAT may fail (documented limitation).
No relay in v1.
"""

from __future__ import annotations

import asyncio
import os

from ashichat.logging_setup import get_logger
from ashichat.packet import PacketType, PingPayload, make_packet

log = get_logger(__name__)

BURST_COUNT = 15
BURST_INTERVAL = 0.2  # 200ms
BURST_TOTAL = BURST_COUNT * BURST_INTERVAL  # 3 seconds


class NATTraversal:
    """UDP hole punching without external STUN servers."""

    def __init__(self, session_id: bytes | None = None) -> None:
        self._session_id = session_id or b"\x00" * 8

    async def punch_hole(
        self,
        target_addr: tuple[str, int],
        send_fn,  # send_fn(packet, addr)
        response_event: asyncio.Event | None = None,
    ) -> bool:
        """Send burst of PING packets to punch through NAT.

        Returns ``True`` if a response was received during the burst.
        """
        log.info("Starting hole punch to %s:%d", *target_addr)

        if response_event is None:
            response_event = asyncio.Event()

        for i in range(BURST_COUNT):
            ping = make_packet(
                PacketType.PING,
                PingPayload(session_id=self._session_id, ping_id=os.urandom(8)),
            )
            send_fn(ping, target_addr)

            # Check if we got a response
            try:
                await asyncio.wait_for(
                    response_event.wait(), timeout=BURST_INTERVAL
                )
                log.info("Hole punch succeeded on burst %d", i + 1)
                return True
            except asyncio.TimeoutError:
                continue

        log.warning("Hole punch to %s:%d failed after %d bursts", *target_addr, BURST_COUNT)
        return False

    @staticmethod
    def learn_endpoint(
        peer_id: bytes,
        observed_addr: tuple[str, int],
        current_addr: tuple[str, int] | None,
    ) -> bool:
        """Reflection rule: observed source IP:port from any authenticated
        packet is authoritative.

        Returns ``True`` if the endpoint changed.
        """
        if current_addr == observed_addr:
            return False
        log.info(
            "Endpoint learned for %s: %s:%d",
            peer_id.hex()[:8],
            *observed_addr,
        )
        return True
