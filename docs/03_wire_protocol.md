# 3. Wire Protocol Specification

AshiChat uses a custom binary framing protocol. All packets sent over the wire (UDP or TCP) adhere to this strict structure. No plaintext JSON or strings are allowed at the framing layer.

## 3.1 Packet Structure

Every packet starts with a 4-byte header followed by a variable-length payload.

```byte
struct Packet {
    uint8   version            // Protocol Version (Always 0x01 for AshiChat v1)
    uint8   packet_type        // Packet Enum
    uint16  payload_length     // Length of the payload in bytes (Max 65535)
    bytes   payload            // The packet payload
}
```

Any packet received with a `version` other than `0x01` must be silently dropped. Any packet exceeding defined resource limits (e.g., maximum packet size of 64KB) is immediately rejected.

## 3.2 Packet Types (Enum)

The `packet_type` field dictates how the `payload` is parsed.

| Code | Type | Description |
|------|------|-------------|
| `0x01` | `HELLO` | Handshake initiation containing ephemeral key and signature. |
| `0x02` | `HELLO_ACK` | Handshake response completing the session key derivation. |
| `0x03` | `DATA` | Encrypted application data (chat messages). |
| `0x04` | `ACK` | Delivery receipt for a `DATA` packet. |
| `0x05` | `ENDPOINT_UPDATE` | Broadcast of a peer's new IP/Port. |
| `0x06` | `RESOLVE_REQUEST` | Query asking the overlay for a peer's endpoint. |
| `0x07` | `PING` | Session keep-alive request. |
| `0x08` | `PONG` | Session keep-alive response. |

## 3.3 The DATA Packet

The `DATA` packet encapsulates the AES-256-GCM encrypted application payloads.

**Payload Structure:**
- `session_id` (8 bytes): Uniquely identifies the derived cryptographic session.
- `sequence_number` (4 bytes): Monotonically increasing counter to prevent replays.
- `ciphertext_length` (4 bytes): Length of the encrypted blob.
- `ciphertext` (variable): The AES-GCM encrypted data.
- `auth_tag` (16 bytes): The GCM authentication tag.

### Encryption Details
- **Cipher:** AES-256-GCM
- **Nonce:** `session_id (8 bytes) || sequence_number (4 bytes)`
- **Associated Data (AAD):** `packet_type || sequence_number`

If the `sequence_number` is less than or equal to the highest sequence number received in this session, the packet is treated as a replay attack and dropped.

## 3.4 The ACK Packet

The `ACK` packet confirms successful decryption and processing of a `DATA` packet.

**Payload Structure:**
- `session_id` (8 bytes)
- `ack_sequence_number` (4 bytes)

When a node sends a `DATA` packet, it buffers it in a `pending` queue.
- If an `ACK` is received, the message is marked `delivered`.
- If no `ACK` is received within 30 seconds, the packet reverts to the `queued` state and will be retried (up to 5 times before failure).
