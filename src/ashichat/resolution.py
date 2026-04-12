"""Recursive peer resolution for AshiChat.

TTL-limited expanding ring resolution with request deduplication.
Resolution is probabilistic and eventually consistent.
"""

from __future__ import annotations

import asyncio
import os
import time

from ashichat.logging_setup import get_logger
from ashichat.packet import (
    EndpointUpdatePayload,
    PacketType,
    ResolveRequestPayload,
    make_packet,
)

log = get_logger(__name__)

MAX_TTL = 5
CACHE_TTL = 300  # 5 minutes
MAX_CACHE = 1000
RESOLVE_TIMEOUT = 10.0  # seconds


class ResolutionManager:
    """Handles RESOLVE_REQUEST forwarding and ENDPOINT_UPDATE verification."""

    def __init__(
        self,
        local_peer_id: bytes,
        get_overlay_fn,  # () -> list[bytes]
        get_endpoint_fn,  # (peer_id) -> tuple | None
        send_fn,  # (packet, addr)
        verify_endpoint_sig_fn=None,  # (update) -> bool
        update_endpoint_fn=None,  # (peer_id, endpoint, version) -> None
    ) -> None:
        self._local_id = local_peer_id
        self._get_overlay = get_overlay_fn
        self._get_endpoint = get_endpoint_fn
        self._send = send_fn
        self._verify_sig = verify_endpoint_sig_fn
        self._update_endpoint = update_endpoint_fn

        # request_id → timestamp (dedup cache)
        self._request_cache: dict[bytes, float] = {}
        # target_peer_id → Future (pending resolutions)
        self._pending: dict[bytes, asyncio.Future] = {}

    async def resolve_peer(
        self, target_peer_id: bytes
    ) -> tuple[str, int] | None:
        """Initiate resolution for a peer.  Returns endpoint or None."""
        # 1. Check local
        ep = self._get_endpoint(target_peer_id)
        if ep is not None:
            return ep

        # 2. Send RESOLVE_REQUEST to overlay
        request_id = os.urandom(16)
        resolve = ResolveRequestPayload(
            request_id=request_id,
            target_peer_id=target_peer_id,
            ttl=MAX_TTL,
        )
        pkt = make_packet(PacketType.RESOLVE_REQUEST, resolve)

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[target_peer_id] = fut

        overlay = self._get_overlay()
        for pid in overlay:
            ep = self._get_endpoint(pid)
            if ep:
                self._send(pkt, ep)

        # 3. Wait for response
        try:
            return await asyncio.wait_for(fut, timeout=RESOLVE_TIMEOUT)
        except asyncio.TimeoutError:
            log.info("Resolution timeout for %s", target_peer_id.hex()[:8])
            return None
        finally:
            self._pending.pop(target_peer_id, None)

    async def handle_resolve_request(
        self,
        request: ResolveRequestPayload,
        sender_addr: tuple[str, int],
    ) -> None:
        """Process an incoming RESOLVE_REQUEST."""
        # 1. Dedup
        self._cleanup_cache()
        if request.request_id in self._request_cache:
            return
        self._request_cache[request.request_id] = time.time()

        # Enforce cache size
        if len(self._request_cache) > MAX_CACHE:
            oldest = sorted(self._request_cache, key=self._request_cache.get)
            for k in oldest[: len(self._request_cache) - MAX_CACHE]:
                del self._request_cache[k]

        # 2. Do we know the target?
        if request.target_peer_id == self._local_id:
            # Respond with our own endpoint (caller handles this)
            return

        ep = self._get_endpoint(request.target_peer_id)
        if ep is not None:
            # We know them — respond (endpoint update sent by caller)
            return

        # 3. Forward if TTL > 0
        if request.ttl > 0:
            fwd = ResolveRequestPayload(
                request_id=request.request_id,
                target_peer_id=request.target_peer_id,
                ttl=request.ttl - 1,
            )
            fwd_pkt = make_packet(PacketType.RESOLVE_REQUEST, fwd)

            overlay = self._get_overlay()
            for pid in overlay:
                peer_ep = self._get_endpoint(pid)
                if peer_ep and peer_ep != sender_addr:
                    self._send(fwd_pkt, peer_ep)

    async def handle_endpoint_update(
        self, update: EndpointUpdatePayload
    ) -> bool:
        """Process an ENDPOINT_UPDATE.  Returns True if accepted."""
        # 1. Verify signature (if verifier provided)
        if self._verify_sig and not self._verify_sig(update):
            log.warning("Invalid endpoint update signature from %s", update.peer_id.hex()[:8])
            return False

        # 2. Update storage
        if self._update_endpoint:
            self._update_endpoint(
                update.peer_id,
                (update.endpoint_ip, update.endpoint_port),
                update.version_counter,
            )

        # 3. Complete pending resolution
        fut = self._pending.get(update.peer_id)
        if fut and not fut.done():
            fut.set_result((update.endpoint_ip, update.endpoint_port))

        return True

    def _cleanup_cache(self) -> None:
        """Remove cache entries older than 5 minutes."""
        cutoff = time.time() - CACHE_TTL
        expired = [k for k, v in self._request_cache.items() if v < cutoff]
        for k in expired:
            del self._request_cache[k]
