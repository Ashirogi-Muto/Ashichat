# 1. AshiChat Architecture Overview

## 1.1 Philosophy

AshiChat is a decentralized, invite-only, identity-based, peer-to-peer encrypted terminal messaging protocol.
It operates without central bootstrap servers, global directories, or mandatory relays. Its core philosophy revolves around:

- **Strictly Private:** Connect only via cryptographic invites.
- **Serverless:** The network is purely a mesh of active peers.
- **Trust-Based:** Strict mutual authentication. No unsolicited messaging.
- **Local-First Storage:** All messages and metadata are stored on your device.

## 1.2 System Topology

AshiChat nodes maintain two primary peer lists:
- **DirectPeers:** Explicitly trusted contacts (added via invite).
- **OverlayPeers:** A bounded, random subset of known active peers (max 50) used for routing and network resilience.

### Packet Routing

There are no dedicated router nodes. Every node participates in the overlay mesh.
Messages between peers use **UDP** (primary) with optional TCP fallback. If Alice wants to message Bob but they are not directly connected, Alice issues a `RESOLVE_REQUEST` to her overlay peers to discover Bob's current endpoint.

## 1.3 Design Trade-offs

AshiChat prioritizes lightweight P2P over guaranteed delivery under all network conditions. 
- **No Global Reachability:** Total network partitions will isolate peers.
- **No Offline Inbox Server:** If a peer is offline, messages are queued locally by the sender and retried when the peer comes back online.
- **Bounded Scalability:** The network scales infinitely in total size because each node only tracks a constant $O(K)$ number of peers, preventing memory bloat.
