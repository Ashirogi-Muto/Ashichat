# 6. Storage and Synchronization

AshiChat is "Local-First". There are no cloud backups or central servers holding your data. All metadata and messages are stored on your local disk.

## 6.1 Local Storage Architecture

AshiChat utilizes two storage mechanisms:
1. **SQLite (`ashichat.db`):** Used exclusively for relational metadata.
2. **Encrypted Log Files:** Used for storing the actual message payloads.

### SQLite Tables
- **Peers:** Tracks `peer_id`, `public_key`, `last_known_endpoint`, `version_counter`, `last_seen`, and aliases.
- **MessageQueue:** Tracks the status of outgoing messages (`queued`, `pending`, `delivered`, `acknowledged`). *Note: It does not store the message text.*

### Encrypted Message Logs
Each contact has a dedicated encrypted log file located at:
`~/.ashichat/messages/<peer_id>.log`

Every line in this file is a discrete, AES-256-GCM encrypted message blob. 
- **Encryption at Rest:** Because messages are encrypted before they hit the disk, physical access to the device does not immediately compromise chat history (assuming the application key is not in memory).
- **Crash Safety:** Messages are written to a temporary file, flushed via `fsync`, and then atomically moved, guaranteeing the logs are never corrupted by an unexpected power loss.

## 6.2 Offline Queues

Because there is no central server, AshiChat handles offline messaging via a persistent local queue.

If Alice sends a message to Bob but Bob is offline:
1. The message is encrypted and appended to Bob's local log file.
2. The SQLite `MessageQueue` marks the sequence number as `queued`.
3. Alice's node will periodically attempt to route the message (up to 7 days).
4. When Bob eventually connects and handshakes, the queued packets are transmitted.
5. Bob replies with an `ACK` packet, and Alice marks the message as `delivered`.

## 6.3 Log Synchronization Protocol

AshiChat messages are strictly ordered using monotonically increasing sequence numbers. Because logs are append-only per sender, state reconciliation is simple and conflict-free.

### The `I_HAVE_UP_TO` Mechanism

When a connection is established (either a reconnect or a multi-device sync), the nodes exchange their current state.

Alice sends to Bob:
`I_HAVE_UP_TO(alice_seq=150, bob_seq=42)`

Bob looks at his local logs. 
- If Bob's counter for Alice is `150`, he knows he has all her messages.
- If Bob has sent messages up to sequence `50`, he realizes Alice is missing sequences `43` through `50`.
- Bob immediately reads the encrypted blobs from `43` to `50` from his disk and transmits them.

Because the blobs are cryptographically signed and encrypted by the origin, they are the canonical source of truth. 

*(See `02_identity_and_multi_device.md` for how this exact mechanism is used to sync multiple devices belonging to the same user).*
