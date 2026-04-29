# ASHICHAT — COMPLETE PROJECT DESCRIPTION


## 1. Project Overview

AshiChat is a decentralized, invite-only, identity-based, peer-to-peer encrypted terminal messaging protocol and client. It is designed to operate without centralized bootstrap servers, without global discovery, and without mandatory relays. It prioritizes privacy, scalability, lightweight design, and explicit engineering tradeoffs over guaranteed global reachability.

The system is built in Python for the initial implementation and is structured modularly to allow future extension. It must remain lightweight and avoid heavy dependencies such as bundled Tor or embedded databases beyond SQLite.

**Note:** For the comprehensive and highly detailed protocol specifications, including byte-level framing, please refer to the `docs/` directory.

The core philosophy of AshiChat is:

- No unsolicited messaging
- No global peer discovery
- No centralized bootstrap dependency
- No public directory
- Invite-only trust graph
- Probabilistic eventual reconnection
- Bounded overlay scalability
- Explicitly documented failure tradeoffs

AshiChat is not intended to guarantee global recovery under total graph churn. This is an explicit design decision in favor of decentralization and lightweight operation.

### 1.1. Protocol Versioning

AshiChat operates under explicit protocol versioning. All wire packets begin with a version byte. Version `0x01` represents AshiChat v1. Any packet received with an unsupported version must be silently dropped.

Backward compatibility is not guaranteed in v1. Future versions must increment the version byte.

## 2. Identity Model

Identity is divided into **Master Identity** and **Device Identity**:

1.  **Master Identity (Root):** Generated from a mnemonic seed phrase. The `Root Public Key` uniquely defines the user. **Peer ID = SHA-256(root_public_key)**.
2.  **Device Identity:** Each device generates its own local Ed25519 keypair. The `Device Public Key` is signed by the Master Identity.

When a peer sends a message to a `Peer ID`, the overlay routes the message to any active device that can prove its device key is signed by the `Root Public Key`.

Identity files are stored locally under:

`~/.ashichat/identity/`

Identity is persistent. **Device rotation is supported** (you can provision a new device with the Master Identity), but the Root Identity is immutable.

All peer recognition and verification is cryptographic and based on root key ownership. Devices belonging to the same Root Identity are implicitly trusted and sync with each other.

## 3. Invite-Based Trust Model

Peer addition occurs only via invite code.

Invite format:

`ashichat://v1:<base58_encoded_public_key>`

Invites contain only the public key (no IP, no endpoint). Endpoint hints may optionally be included but are not required.

Invite codes can become stale naturally. Reconnection is handled via stored endpoint memory and overlay resolution.

No global discovery is supported. No unsolicited connection attempts are allowed.

### 3.1. Strict Mutual Authentication

AshiChat uses strict mutual authentication.

- Nodes **MUST** reject any handshake from unknown public keys not already present in `DirectPeers`.
- There is **no trust-on-first-use (TOFU)**.

## 4. Messaging Architecture

AshiChat uses layered architecture:

### 4.1. Wire Protocol Structure

All network packets conform to a binary framing structure:

```byte
struct Packet {
    uint8   version            // Always 0x01 for v1
    uint8   packet_type        // Enum
    uint16  payload_length
    bytes   payload
}
```

Packet types:

- `0x01` HELLO
- `0x02` HELLO_ACK
- `0x03` DATA
- `0x04` ACK
- `0x05` ENDPOINT_UPDATE
- `0x06` RESOLVE_REQUEST
- `0x07` PING
- `0x08` PONG

Packets exceeding defined resource limits must be dropped immediately. No plaintext JSON is permitted over the wire.

**Application Layer:**

- Chat handling
- Message persistence
- Offline queue
- Synchronization

**Session Layer:**

- Handshake
- Ephemeral key exchange (X25519)
- AES-256-GCM encryption
- Sequence numbers
- ACK handling

**Overlay Layer:**

- DirectPeers (explicit contacts)
- OverlayPeers (bounded random subset)
- Endpoint propagation
- Recursive resolution

**Transport Layer:**

- UDP primary
- Optional TCP fallback

### 4.2. DATA Packet

**Payload:**

- `session_id` (8 bytes)
- `sequence_number` (4 bytes)
- `ciphertext_length` (4 bytes)
- `ciphertext`
- `auth_tag` (16 bytes)

**Nonce:**

`nonce = session_id (8 bytes) || sequence_number (4 bytes)`

AES-256-GCM encrypts plaintext.

**Associated data:**

`packet_type || sequence_number`

- Sequence number is per-session and monotonic.
- Sequence numbers are stored in SQLite.
- **`sequence_number` MUST NEVER repeat within a session.**
- **`session_id` MUST be generated using cryptographically secure randomness.**
- **`session_id` MUST NEVER repeat across sessions.**
- **Reject if `sequence_number` <= `last_received_sequence`.**

### 4.3. ACK Packet

**Payload:**

- `session_id`
- `ack_sequence_number`

**Queue states:**

- `queued` (not yet attempted)
- `pending` (in-flight)
- `delivered` (ACK received)
- `acknowledged` (application-level confirm)
- `failed`

**Failure Logic:**
- If no ACK within **30 seconds** → revert to `queued`.
- Max retry attempts: **5**.
- After 5 failures → mark `failed`.

**Delivered requires explicit ACK packet.**

### 4.4. NAT Traversal

**Basic UDP hole punching:**

- Simultaneous UDP packet bursts every 200ms for 3 seconds.
- Store reflected endpoint from peer.
- **No external STUN server.**
- Symmetric NAT may fail.
- **No relay in v1.** (Documented limitation)

**Reflection rule:**
- The source IP:port observed from **any authenticated packet** is treated as authoritative endpoint.

## 5. Storage Model

SQLite is used for metadata only.

**Tables:**

- **Peers:**
  - `peer_id`
  - `public_key`
  - `last_known_endpoint`
  - `version_counter`
  - `last_seen`
  - `nickname`

- **MessageQueue:**
  - `message_id`
  - `receiver`
  - `status` (pending, sent, delivered, acknowledged)
  - `created_at`

Messages are **NOT** stored in SQLite.

Encrypted message logs are stored per peer:

`~/.ashichat/messages/<peer_id>.log`

Each line contains a fully encrypted message blob.

Message ID is derived from a locally maintained atomic counter, not file line index, to avoid concurrency issues.

All messages are encrypted before disk write.

Encrypted-at-rest is mandatory.

### 5.1. Storage and Crash Consistency

**Log rotation:**

- 100MB per file
- Keep 3 rotations

**Atomic write process:**

1. Encrypt message.
2. Append to temp file.
3. `fsync`.
4. Rename atomic.
5. Update SQLite queue in transaction.

Ensures durability.

## 6. Security Model

**Crypto primitives:**

- **Identity:** Ed25519
- **Key exchange:** X25519
- **Encryption:** AES-256-GCM
- **Key derivation:** HKDF-SHA256

All endpoint updates are signed:

`sign(private_key, endpoint || version_counter)`

Version counter is monotonic per node and replaces wall-clock timestamps to prevent clock drift inconsistencies.

**Threat model includes protection against:**

- Endpoint spoofing
- Replay attacks
- Unauthorized impersonation
- Tampering
- Malicious overlay injection

**System does not protect against:**

- Device compromise
- Traffic correlation
- Total network partition

### 6.1. Handshake Protocol

**HELLO (Initiator → Receiver)**

Payload:

- `identity_public_key` (32 bytes)
- `ephemeral_public_key` (32 bytes, X25519)
- `random_nonce` (16 bytes)
- `signature` (64 bytes)

**Signature:**

`signature = sign(identity_private, ephemeral_public_key || random_nonce)`

**Receiver behavior:**

1. Verify identity exists in `DirectPeers`. If unknown, drop silently.
2. **Verify signature.**
3. Generate own ephemeral key.
4. Compute `shared_secret`.

**HELLO_ACK (Receiver → Initiator)**

Payload:

- `identity_public_key` (32 bytes)
- `ephemeral_public_key` (32 bytes)
- `random_nonce` (16 bytes)
- `signature` (64 bytes)

Signature covers:

`sign(identity_private, receiver_ephemeral_pub || initiator_ephemeral_pub || initiator_nonce)`

**Initiator verifies:**

1. Public key matches known peer.
2. Signature valid.
3. Compute `shared_secret`.

### 6.2. Session Key Derivation

```
shared_secret = X25519(local_ephemeral, remote_ephemeral)

session_key = HKDF(
    input_key_material = shared_secret,
    salt = initiator_nonce || receiver_nonce,
    info = "ashichat-session-v1"
)
```

**Derived outputs:**

- `encryption_key` (32 bytes)
- `session_id` (64-bit random identifier)

**`session_id` generation:**
- **Generated via cryptographically secure random.**
- **Unique per handshake.**
- **Never reused.**

Session keys are **rotated only upon reconnection** (new handshake).

No mid-session key rotation in v1.

## 7. Reconnection Strategy

On connection loss:

- Attempt reconnect using `last_known_endpoint`

If failure:

1. Mark endpoint stale
2. Initiate resolution

Resolution uses recursive TTL-limited queries.

### 7.1. Heartbeat Protocol

**PING payload:**

- `session_id`
- `ping_id` (8 bytes)

**PONG echoes same `ping_id`.**

**Heartbeat behavior:**

- Idle ping interval: **10 seconds**
- After 1 minute idle → **20 seconds**
- After 5 minutes idle → **60 seconds**
- Reset to 10 seconds on traffic

**Failure suspicion:**

- 3 missed pings → **SUSPECT**
- 6 missed pings → **DISCONNECTED**
- **Any valid authenticated packet resets suspicion counter.**

### 7.2. Peer State Machine

**States:**

- `DISCONNECTED`
- `CONNECTING`
- `CONNECTED`
- `IDLE`
- `SUSPECT`
- `RESOLVING`
- `FAILED`
- `ARCHIVED`

**Transitions defined deterministically:**

- `CONNECTING` → `CONNECTED` on handshake success
- `CONNECTED` → `IDLE` after inactivity
- `IDLE` → `SUSPECT` after missed heartbeats
- `SUSPECT` → `DISCONNECTED` after threshold
- `DISCONNECTED` → `RESOLVING` if reconnect fails
- `DISCONNECTED` > 7 days → `ARCHIVED` (still retry daily)
- **`ARCHIVED` is not terminal.** It is a cosmetic / UI-level classification.

### 7.3. Reconnection Backoff

**Exponential backoff:**

`1s → 2s → 4s → 8s → 16s → 32s → 1m → 5m → 15m → 1h → 6h cap.`

- Continue indefinitely at 6h intervals.
- After 7 days offline → mark `ARCHIVED` but retry every 24h.

## 8. Overlay Design (Scalable + Privacy-Focused)

Each node maintains:

- **DirectPeers:**
  - Explicitly trusted contacts
  - **Not counted toward overlay cap**
  - Permanent

- **OverlayPeers:**
  - Bounded random rotating subset
  - Default K = 30
  - Max = 50
  - **At least 60% priority indirect peers**
  - **Remaining filled randomly**
  - Constant-size state

**Maintenance:**

- **Rotate 10% every 10 minutes**
- **Drop peers inactive > 1 hour**
- Peer table max size = **500 entries**

**Eviction priority when full:**
1. Oldest inactive non-DirectPeers
2. Peers not in overlay
3. Lowest version freshness

No full graph replication.
No fixed hop radius.
No DHT.

OverlayPeers are selected from:

- Recently active peers
- Random sampling from known peers
- Pruned via LRU + inactivity

This ensures O(K) memory per node regardless of total network size.

## 9. Endpoint Propagation

Hybrid push/pull model.

### 9.1. Endpoint Updates Payload

**ENDPOINT_UPDATE payload:**

- `peer_id` (32 bytes)
- `endpoint_ip`
- `endpoint_port`
- `version_counter` (8 bytes)
- `signature` (64 bytes)

**Signature:**
`sign(identity_private, endpoint || version_counter || "ashichat-endpoint-v1")`

**Rules:**

- Accept only if `version_counter` > `stored_version`.
- Reject equal or lower.
- Arbitrary jumps allowed.

**Push:**

When node changes IP:

1. Increment `version_counter`
2. Sign endpoint update
3. Send `ENDPOINT_UPDATE` to DirectPeers + OverlayPeers

**Pull:**

Triggered by `RESOLVE_REQUEST`.

## 10. Recursive Resolution Protocol

Packet types:

- **ENDPOINT_UPDATE:**
  - `peer_id`
  - `endpoint`
  - `version_counter`
  - `signature`

- **RESOLVE_REQUEST:**
  - `request_id` (16 bytes)
  - `target_peer_id` (32 bytes)
  - `ttl` (1 byte)

**When receiving `RESOLVE_REQUEST`:**

- If endpoint known → respond with `ENDPOINT_UPDATE`.
- Else if `ttl > 0`:
  - decrement `ttl`
  - forward to OverlayPeers (excluding sender).
  - **Do not forward back to origin.**
  - **Do not forward if `target_peer_id` == self.**

- Cache `request_id` for **5 minutes**.
- **TTL max = 5.**

Deduplicate requests via `request_id` cache.

This creates expanding ring resolution with bounded propagation.

Resolution is probabilistic and eventually consistent.

## 11. Chat Synchronization

On reconnect between two distinct peers (e.g., Alice and Bob):

Nodes exchange:

`I_HAVE_UP_TO(sequence_number)`

Missing encrypted messages are transmitted. Each sender maintains independent sequence counter. Conflict-free because messages are append-only per sender. If local message log deleted: Peer resends entire missing history. Encrypted blobs are canonical source of truth.

### 11.1. Multi-Device Sync

Devices sharing the same **Master Identity** automatically discover each other over the overlay network using their shared `Peer ID`.
- Upon connection, devices verify their mutual Master Identity signatures.
- They execute the standard `I_HAVE_UP_TO` synchronization for **all** peer message logs.
- Because AshiChat message logs are append-only and cryptographically tied to sequence numbers, devices seamlessly converge on the same unified chat history and offline queue state without needing a central server.

## 12. Automatic Startup Behavior

When script runs:

1. Load identity
2. Load peer table
3. Start listener
4. Start reconnect loop
5. Start overlay maintenance
6. Start queue retry engine
7. Launch terminal UI

No manual commands required.

## 13. Failure Acceptance

AshiChat does **NOT** guarantee recovery if:

- Entire connected component changes IP simultaneously
- No overlapping reachable nodes remain
- **All peers behind symmetric NAT without hole punching success**

Manual invite reconnect is required in such cases.

This is deliberate tradeoff for decentralization.

## 14. Scalability Properties

**Memory per node:** O(K)

**Bandwidth:**

- Minimal push updates
- TTL-limited resolution
- No full table replication

**Lookup:**

- Probabilistic
- Eventually consistent

Scales reasonably to thousands of nodes without central authority.

### 14.1. Rate Limiting

**Token bucket per peer:**

- Max 10 `RESOLVE_REQUEST`/min
- Max 20 `ENDPOINT_UPDATE`/min
- Max 100 `DATA` packets/sec

**Global:**

- Max 50 forwarded `RESOLVE`/min

Excess dropped silently.

### 14.2. Resource Limits

- Max message size: **64KB**
- Max concurrent sessions: **100**
- Max resolve cache entries: **1000**
- Max overlay peers: **50**
- Max peer table size: **500**
- Max TTL: **5**

## 15. Privacy Characteristics

- Invite-only trust
- No public directory
- No unsolicited discovery
- No global peer listing
- Limited overlay visibility
- Signed endpoint records only for known peers
- Metadata leakage minimized by bounded overlay.

## 16. Extensibility

**Future optional modules:**

- Structured DHT plugin
- Tor transport plugin
- Relay fallback module
- Rust networking core rewrite

**Core architecture must remain modular.**

## 17. Technical Stack & Dependencies

### 17.1. Cryptography Library

**Use:** `cryptography` (the standard Python package)

- **Ed25519:** `cryptography.hazmat.primitives.asymmetric.ed25519`
- **X25519:** `cryptography.hazmat.primitives.asymmetric.x25519`
- **AESGCM:** `cryptography.hazmat.primitives.ciphers.aead.AESGCM`
- **HKDF:** `cryptography.hazmat.primitives.kdf.hkdf.HKDF`

**Rationale:**
- Broader ecosystem support.
- Explicit HKDF control.
- Widely used in production Python systems.
- Better long-term maintainability.

### 17.2. Async Framework

**Use:** `asyncio` (Non-negotiable)

- `asyncio.create_task` for background tasks.
- `asyncio.DatagramProtocol` for UDP.
- `asyncio.StreamReader/Writer` for optional TCP fallback.

**Reasons:**
- Concurrent peers.
- Parallel bursts for UDP hole punching.
- Reconnection backoff loops.
- Heartbeat scheduling.
- Overlay rotation tasks.
- Rate limiter timers.

Architecture must be fully async-native.

## 18. Project Structure

Modular layout is required.

```
ashichat/
│
├── src/
│   └── ashichat/
│       ├── __init__.py
│       ├── config.py
│       ├── identity.py
│       ├── crypto.py
│       ├── handshake.py
│       ├── session.py
│       ├── packet.py
│       ├── transport_udp.py
│       ├── transport_tcp.py
│       ├── nat.py
│       ├── overlay.py
│       ├── resolution.py
│       ├── peer_state.py
│       ├── heartbeat.py
│       ├── queue_manager.py
│       ├── storage.py
│       ├── rate_limiter.py
│       ├── reconnect.py
│       ├── node.py
│       └── ui/
│           ├── tui.py
│           └── components.py
│
├── tests/
│   ├── test_crypto.py
│   ├── test_handshake.py
│   ├── test_packet.py
│   ├── test_overlay.py
│   ├── test_resolution.py
│   ├── test_rate_limiter.py
│   └── test_storage.py
│
├── main.py
├── pyproject.toml
└── README.md
```

This ensures separation of concerns and testability.

## 19. Implementation Phases

**Phase 0 — Project Bootstrap**
- Setup `pyproject.toml`, venv, logging, config loader.
- Setup directory structure and test harness.
- No network.

**Phase 1 — Crypto Primitives Layer**
- Ed25519 identity, Key save/load.
- X25519 ephemeral, HKDF, AESGCM wrappers.
- Nonce builder, Signature helpers.
- **Tests:** Sign/verify, ECDH equality, HKDF determinism, Roundtrip encryption.

**Phase 2 — Packet Encoding/Decoding**
- Binary `Packet` struct, Serialize/deserialize.
- Payload length validation.
- Malformed packet rejection.
- **Tests:** Fuzzing, Truncation handling.

**Phase 3 — Handshake Implementation**
- `HELLO`, `HELLO_ACK`, Signature verification.
- Session key & ID derivation.
- **Tests:** Successful handshake, Replay rejection, Invalid signature.

**Phase 4 — UDP Transport Layer**
- Async UDP listener, Packet dispatching.
- Connection tracking, Session registry.
- **Tests:** Loopback handshake, Concurrent attempts.

**Phase 5 — DATA & ACK Flow**
- Session encryption, Sequence increment.
- Queue state transitions, Replay rejection.
- **Tests:** Out-of-order rejection, Duplicate rejection, Timeout logic.

**Phase 6 — Storage & Crash Safety**
- SQLite schema, Atomic log append.
- Log rotation, Sequence persistence.
- **Tests:** Mid-write crash simulation, Log corruption recovery.

**Phase 7 — Heartbeat & State Machine**
- Ping scheduling, Suspicion threshold.
- Backoff scheduler, State transitions.
- **Tests:** Missed ping simulation, Rapid reconnect.

**Phase 8 — NAT Hole Punching**
- Burst logic, Simultaneous send.
- Endpoint learning, Retry fallback.

**Phase 9 — Overlay & Peer Table**
- Selection algorithm, Priority vs random fill.
- Rotation scheduler, Eviction logic.
- **Tests:** Max K enforcement, Eviction priority.

**Phase 10 — Recursive Resolution**
- Forwarding logic, TTL decrement.
- Request cache, Update merging.
- **Tests:** Loop prevention, TTL exhaustion.

**Phase 11 — Rate Limiter**
- Token bucket per peer, Global limiter.
- **Tests:** Flood scenario.

**Phase 12 — Terminal UI**
- Contact list, Chat scrollback, Input handling.

**Phase 13 — Chaos Testing**
- Simulate packet loss, duplication, disconnects.

## 20. Testing Strategy

### 20.1. Framework

**Use:** `pytest` (Standard)

**Reasons:**
- Cleaner syntax.
- Parametrization.
- Fixtures.
- Better async support (`pytest-asyncio`).

### 20.2. Test Priority Order

1. **Crypto & Packet Parsing:** Pure deterministic logic. Must be bulletproof.
2. **Handshake:** Connection establishment correctness.
3. **Session Encryption:** Data confidentiality and integrity.
4. **Storage Atomicity:** Crash resilience.
5. **Overlay & Resolution:** Network topology logic.
6. **NAT & Transport:** Network I/O.
7. **UI:** Human interaction last.

**UI-Agnostic Principle:**
- Core networking, crypto, and logic must be testable **without** importing any UI libraries.
- Headless testing is mandatory for CI.
- Crypto and packet parsing must be fully verified before any network code is written.

## 21. Configuration Specifications

### 21.1. Format

**Use:** `TOML`

**Rationale:**
- Consistent with `pyproject.toml`.
- Clean syntax with comment support.
- Native support in Python 3.11+ via `tomllib`.

### 21.2. File Location

User config file:
`~/.ashichat/config.toml`

**Example Structure:**
```toml
[network]
udp_port = 9000
max_peers = 500
overlay_k = 50

[storage]
message_log_limit_mb = 100
max_log_rotations = 3

[debug]
log_level = "INFO"
```

**Constraint:** Keep config minimal. No dynamic config reload in v1.

## 22. Logging Strategy

**Do NOT log to console.** `stdout/stderr` is reserved for TUI.

### 22.1. Rules

**File-only logging:**
`~/.ashichat/debug.log`

- **Separation:**
  - `debug.log` = plaintext operational logging.
  - `messages/*.log` = encrypted chat logs.

**Rotation Policy for Debug Log:**
- Max size: **10MB**
- Keep **5 rotations**
- Use `logging.handlers.RotatingFileHandler`

**Example:**
`debug.log`, `debug.log.1`, ...

### 22.2. Levels

Support: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`
Default: `INFO` (Configurable via `config.toml`)

## 23. TUI Architecture

### 23.1. Library

**Use:** `Textual`

**Reasons:**
- Async-native (built on `asyncio`).
- Clean widget-based architecture.
- Modern, app-like experience.
- Supports background tasks cleanly.

### 23.2. Integration

**Main Entry Point Pattern:**

```python
async def main():
    node = Node(...)
    await node.start()

    app = AshiChatApp(node)
    await app.run_async()
```

### 23.3. Layering Principle

**Core networking and crypto must be UI-agnostic.**
- UI subscribes to state changes.
- UI never directly mutates network layer internals.
- Strict separation of concerns.
