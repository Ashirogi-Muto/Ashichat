"""Token-bucket rate limiter for AshiChat.

Per-peer limits:
    RESOLVE_REQUEST: 10/min
    ENDPOINT_UPDATE: 20/min
    DATA:            100/sec

Global limits:
    Forwarded RESOLVE_REQUEST: 50/min
"""

from __future__ import annotations

import time
from collections import defaultdict

from ashichat.packet import PacketType


class TokenBucket:
    """Classic token-bucket rate limiter."""

    def __init__(self, rate: float, capacity: int) -> None:
        self._rate = rate        # tokens per second
        self._capacity = capacity
        self._tokens = float(capacity)
        self._last_refill = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._last_refill = now

    def consume(self, tokens: int = 1) -> bool:
        """Return ``True`` if tokens available, ``False`` if rate limited."""
        self._refill()
        if self._tokens >= tokens:
            self._tokens -= tokens
            return True
        return False


# Per-peer rate limits: (rate tokens/sec, burst capacity)
_PEER_LIMITS: dict[PacketType, tuple[float, int]] = {
    PacketType.RESOLVE_REQUEST: (10 / 60, 10),    # 10/min
    PacketType.ENDPOINT_UPDATE: (20 / 60, 20),     # 20/min
    PacketType.DATA: (100.0, 100),                  # 100/sec
}


class PeerRateLimiter:
    """Per-peer token-bucket rate limiter."""

    def __init__(self) -> None:
        self._buckets: dict[bytes, dict[PacketType, TokenBucket]] = defaultdict(dict)

    def check(self, peer_id: bytes, packet_type: PacketType) -> bool:
        """Return ``True`` if the packet is allowed under rate limits."""
        limits = _PEER_LIMITS.get(packet_type)
        if limits is None:
            return True  # unlimited type

        peer_buckets = self._buckets[peer_id]
        if packet_type not in peer_buckets:
            rate, cap = limits
            peer_buckets[packet_type] = TokenBucket(rate, cap)

        return peer_buckets[packet_type].consume()


class GlobalRateLimiter:
    """Global rate limits (not per-peer)."""

    def __init__(self) -> None:
        # Max 50 forwarded RESOLVE_REQUEST/min
        self._resolve_forward = TokenBucket(rate=50 / 60, capacity=50)

    def check_resolve_forward(self) -> bool:
        """Return ``True`` if a forwarded RESOLVE is allowed."""
        return self._resolve_forward.consume()
