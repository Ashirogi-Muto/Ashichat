# 2. Identity and Multi-Device Sync

AshiChat utilizes a cryptographic identity model. Unlike centralized messengers, there are no phone numbers or usernames. A user is defined purely by cryptographic keys.

## 2.1 Master Identity (Root)

The core user identity is the **Master Identity**.
- **Derivation:** Generated from a secure mnemonic seed phrase.
- **Root Public Key:** An Ed25519 public key that uniquely identifies the user.
- **Peer ID:** `SHA-256(Root_Public_Key)`. This is the public identifier shared via invites.

The Master Identity is immutable. If a user loses their seed phrase, they lose their identity.

## 2.2 Device Identity

Because users may have multiple devices (e.g., a laptop and a VPS), each device generates its own local **Device Identity**.
- **Device Key:** A local Ed25519 keypair.
- **Signature Link:** The Device Public Key is cryptographically signed by the Master Identity.
- **Revocation:** A device can be revoked by publishing a revocation packet signed by the Master Identity, effectively invalidating the Device Key.

### Connection Authentication
When Device A connects to a peer, it presents:
1. Its Device Public Key.
2. The signature from the Master Identity proving the device is authorized.

The peer verifies the signature against the known Root Public Key of the user.

## 2.3 Multi-Device Synchronization

AshiChat treats a user's multiple devices as a unified entity via a P2P sync protocol.

### Discovery
Devices sharing the same Master Identity automatically discover each other over the overlay network by querying for their own shared `Peer ID`. 

### State Reconciliation (`I_HAVE_UP_TO`)
When two devices belonging to the same user connect, they perform an implicit sync:
1. They authenticate each other via their Master Identity signatures.
2. They exchange `I_HAVE_UP_TO(sequence_number)` packets for **all** known peers.
3. Because AshiChat message logs are append-only, any missing encrypted messages are transmitted from the device that has them to the device that doesn't.

This process ensures that offline queues and historical chat logs are eventually consistent across all devices belonging to a user, with zero central server involvement.
