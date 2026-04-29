# 5. Overlay Routing and NAT Traversal

AshiChat is designed to connect peers across the internet without central rendezvous servers. It achieves this using a bounded overlay mesh and aggressive UDP hole punching.

## 5.1 The Overlay Mesh

Each node maintains a local view of the network split into two categories:
1. **DirectPeers:** Explicitly trusted contacts authorized via invite. These are permanent and do not count toward overlay caps.
2. **OverlayPeers:** A dynamic, randomly sampled subset of the network. Max 50 peers.

Nodes constantly rotate their `OverlayPeers` (10% churn every 10 minutes) and drop inactive peers. Because the state size is fixed at $O(K)$, a node uses minimal memory regardless of the total network size.

## 5.2 Recursive Endpoint Resolution

If Alice needs to message Bob, but she does not have a current, valid IP/Port for him, she initiates a resolution over the overlay.

**The `RESOLVE_REQUEST` Packet:**
- `request_id` (16 bytes)
- `target_peer_id` (32 bytes): SHA-256 of Bob's public key.
- `ttl` (1 byte): Time-To-Live (Max 5).

Alice sends this to her `OverlayPeers`. 
When an intermediate peer receives the request:
1. They check if they know the endpoint for `target_peer_id`.
2. If they do, they reply directly to Alice with an `ENDPOINT_UPDATE`.
3. If they don't, and `ttl > 0`, they decrement `ttl` and forward the request to *their* `OverlayPeers` (excluding the sender).
4. `request_id` is cached for 5 minutes to prevent routing loops.

## 5.3 Endpoint Updates

When a peer's IP changes, they broadcast an `ENDPOINT_UPDATE` to their direct and overlay peers.

**The `ENDPOINT_UPDATE` Packet:**
- `peer_id` (32 bytes)
- `endpoint_ip` (4 bytes IPv4 or 16 bytes IPv6)
- `endpoint_port` (2 bytes)
- `version_counter` (8 bytes): Monotonically increasing counter to prevent replay of old endpoints.
- `signature` (64 bytes): Signed by the identity key to prevent spoofing.

## 5.4 NAT Traversal (Hole Punching)

AshiChat assumes most users are behind home routers (NAT). 
When Alice discovers Bob's endpoint via resolution, she begins UDP Hole Punching:
- Alice and Bob send simultaneous bursts of UDP packets to each other every 200ms for 3 seconds.
- There are no STUN/TURN servers.
- The router state tables open up allowing inbound UDP traffic from the specified IP/Port.

> **Limitation:** If both Alice and Bob are behind strict Symmetric NATs, UDP hole punching will fail. Because AshiChat v1 does not bundle relay servers, they will remain disconnected. This is an explicit design trade-off prioritizing decentralization.
