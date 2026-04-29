# 4. Cryptography and Handshake Protocol

AshiChat uses standard, well-vetted cryptographic primitives to ensure strict mutual authentication and Perfect Forward Secrecy (PFS). There is no "Trust-On-First-Use" (TOFU); peers must explicitly know each other's public keys.

## 4.1 Cryptographic Primitives
- **Identity Keys:** Ed25519 (Used for signing and peer identification).
- **Key Exchange:** X25519 (Ephemeral keys for session generation).
- **Symmetric Encryption:** AES-256-GCM (Authenticated encryption).
- **Key Derivation Function:** HKDF-SHA256.

## 4.2 Handshake Protocol

When Alice (Initiator) wants to connect to Bob (Receiver), they execute a 2-way handshake to establish a shared session key.

### Step 1: `HELLO` (Initiator -> Receiver)

**Payload Structure:**
- `identity_public_key` (32 bytes): Alice's Ed25519 public key.
- `ephemeral_public_key` (32 bytes): Alice's newly generated X25519 public key.
- `random_nonce` (16 bytes): Cryptographically secure random bytes.
- `signature` (64 bytes): Ed25519 signature over `(ephemeral_public_key || random_nonce)` signed by Alice's identity private key.

**Receiver Action:**
1. Bob checks if Alice's `identity_public_key` is in his `DirectPeers` list. If not, he drops the packet silently.
2. Bob verifies the `signature`.
3. If valid, Bob generates his own ephemeral X25519 key.

### Step 2: `HELLO_ACK` (Receiver -> Initiator)

**Payload Structure:**
- `identity_public_key` (32 bytes): Bob's Ed25519 public key.
- `ephemeral_public_key` (32 bytes): Bob's X25519 public key.
- `random_nonce` (16 bytes): Bob's secure random bytes.
- `signature` (64 bytes): Ed25519 signature over `(bob_ephemeral_pub || alice_ephemeral_pub || alice_nonce)` signed by Bob's identity private key.

**Initiator Action:**
1. Alice verifies Bob is in her `DirectPeers`.
2. Alice verifies the `signature` using Bob's identity public key.

## 4.3 Session Key Derivation

Both peers now possess each other's ephemeral public keys. They perform an Elliptic-Curve Diffie-Hellman (ECDH) exchange to compute a shared secret.

```
shared_secret = X25519(local_ephemeral_private, remote_ephemeral_public)
```

The symmetric session key is derived using HKDF-SHA256:

```
session_key = HKDF(
    input_key_material = shared_secret,
    salt = initiator_nonce || receiver_nonce,
    info = "ashichat-session-v1"
)
```

This derives two outputs:
- `encryption_key` (32 bytes): Used for AES-256-GCM.
- `session_id` (8 bytes): Sent in the header of all `DATA` packets to identify this context.

Session keys are never rotated mid-session in v1. They are only rotated upon a full reconnect/new handshake.
