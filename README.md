# AshiChat

AshiChat is a decentralized, serverless, peer-to-peer, encrypted terminal messaging system built in Python.

## Core Features

- **Decentralized & Serverless**: No central authority, no metadata harvesting.
- **P2P Mesh Network**: Uses UDP hole punching and a bounded overlay network implementation for communication.
- **Strong Cryptography**: Ed25519 for Identity, X25519 + HKDF-SHA256 + AES-256-GCM for Perfect Forward Secrecy messaging.
- **Terminal UI**: Responsive and feature-rich TUI built on the `Textual` framework.
- **Crash-Safe Storage**: SQLite backend and encrypted log files.

## Project Structure

This project implements a custom minimal viable protocol stack built completely from scratch over raw UDP datagrams.

* **Layer 1: Primitives** (`crypto.py`, `identity.py`): Provides keypairs, ECDH, shared secrets, nonces, HKDF derivation, and AEAD encryption.
* **Layer 2: Wire Protocol** (`packet.py`): Extremely strict binary parser for our 8 distinct packet types, dropping anything invalid.
* **Layer 3: Authentication** (`handshake.py`, `session.py`): Strict mutual authentication logic (no TOFU) enforcing cryptographic identities.
* **Layer 4: Transport & Routing** (`transport_udp.py`, `nat.py`): Non-blocking sockets, adaptive retransmission, STUN-less UDP hole punching. 
* **Layer 5: Orchestration** (`node.py`, `peer_state.py`, `heartbeat.py`): Connects sub-systems, supervises heartbeat, overlay routing, node states.
* **Layer 6: UI** (`ui/tui.py`): The terminal frontend interface.

## Installation and Usage (Plug and Play)

AshiChat is designed to be **completely zero-config and plug-and-play**. There are no databases to configure, no keys to manually generate, and no servers to set up.

Upon first run, AshiChat will automatically:
1. Generate an Ed25519 identity keypair (`~/.ashichat/identity.key`).
2. Create and migrate a local SQLite database for session state (`~/.ashichat/data/`).
3. Set up encrypted message log files (`~/.ashichat/messages/`).

### Quick Start
Requires Python 3.11 or higher (developed against 3.14).

1. **Install the package:**
    ```bash
    pip install .
    ```
    *(For development, use `pip install -e ".[dev]" ` instead).*

2. **Run the application:**
    ```bash
    ashichat
    ```
    This launches the Terminal UI (TUI). 

3. **Connect with peers:**
    - Press `i` in the TUI to view your unique Invite Code.
    - Share this code. When someone connects, AshiChat automatically performs mutual authentication, P2P hole-punching, and establishes an encrypted session.

## Testing

A robust suite of 176 tests (incorporating positive, negative, and chaos resilience tests) are included. Test with pytest:

```bash
pytest tests/
```
