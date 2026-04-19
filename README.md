# AshiChat

AshiChat is a decentralized, invite-only, serverless, peer-to-peer encrypted terminal messaging system built in Python. It is designed for users who prioritize privacy, sovereignty, and direct connections over convenience or global discoverability.

## Core Philosophy

Unlike mainstream messaging platforms, AshiChat has **zero central servers**. There is no global directory, and no way to search for users. 
- **Strictly Private:** Connect only via cryptographic invites.
- **Serverless:** The network is formed purely by the mesh of connected peers.
- **Trust-Based:** Strict mutual authentication with no Trust-On-First-Use (TOFU).
- **Local-First:** All data lives on your device, with messages end-to-end encrypted and stored locally.

## Key Features

- **Strong Cryptography:** Ed25519 for Identity, X25519 + HKDF-SHA256 + AES-256-GCM for Perfect Forward Secrecy messaging.
- **P2P Mesh Network:** Uses custom binary framing over UDP, STUN-less UDP hole punching, and a bounded overlay network implementation for communication.
- **Terminal UI:** Responsive and feature-rich TUI built on the `Textual` framework.
- **Crash-Safe Storage:** SQLite backend for metadata and encrypted log files for message content, ensuring atomic writes.
- **Offline Queue:** Messages are queued and retried automatically when a peer comes online (up to 7 days).

## Installation and Usage (Plug and Play)

AshiChat is designed to be **completely zero-config and plug-and-play**. There are no databases to configure, no keys to manually generate, and no servers to set up.

Upon first run, AshiChat will automatically:
1. Generate an Ed25519 identity keypair (`~/.ashichat/identity/`).
2. Create and migrate a local SQLite database for session state.
3. Set up encrypted message log files (`~/.ashichat/messages/`).

### Quick Start
Requires **Python 3.11** or higher.

1. **Install the package:**
    ```bash
    pip install .
    ```
    *(For development, use `pip install -e ".[dev]"` instead)*.

2. **Run the application:**
    ```bash
    ashichat
    ```
    This launches the Terminal UI (TUI). 

3. **Connect with peers:**
    - Press `i` in the TUI to view your unique Invite Code.
    - Share this code. When someone connects, AshiChat automatically performs mutual authentication, P2P hole-punching, and establishes an encrypted session.

## Project Structure

This project implements a custom minimal viable protocol stack built completely from scratch over raw UDP datagrams.

* **Layer 1: Primitives** (`crypto.py`, `identity.py`): Provides keypairs, ECDH, shared secrets, nonces, HKDF derivation, and AEAD encryption.
* **Layer 2: Wire Protocol** (`packet.py`): Extremely strict binary parser for our distinct packet types, dropping anything invalid.
* **Layer 3: Authentication** (`handshake.py`, `session.py`): Strict mutual authentication logic enforcing cryptographic identities.
* **Layer 4: Transport & Routing** (`transport_udp.py`, `nat.py`): Non-blocking sockets, adaptive retransmission, STUN-less UDP hole punching. 
* **Layer 5: Orchestration** (`node.py`, `peer_state.py`, `heartbeat.py`): Connects sub-systems, supervises heartbeat, overlay routing, node states.
* **Layer 6: UI** (`ui/tui.py`): The terminal frontend interface.

## System Limitations (By Design)

AshiChat prioritizes decentralization and explicit engineering tradeoffs over guaranteed global reachability.
- **No Global Reachability:** If you and your friends are completely partitioned from the rest of the mesh, you cannot talk.
- **No Offline Inbox:** If a peer is offline, you hold the message until they return. There is no cloud server to hold it for them.
- **Mobile Hostile:** Designed for always-on terminals/VPS/desktops, not mobile phones with aggressive background killing.
- **Symmetric NAT Constraints:** If all peers are behind symmetric NAT without successful hole punching, manual invite reconnection is required.

## Testing

A robust suite of tests (incorporating positive, negative, and chaos resilience tests) are included. Core networking, crypto, and logic are fully UI-agnostic and testable headlessly. Test with pytest:

```bash
pytest tests/