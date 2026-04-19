# AshiChat

AshiChat is a decentralized, invite-only, identity-based, peer-to-peer encrypted terminal messaging protocol and client. It is designed to operate without centralized bootstrap servers, global discovery, or mandatory relays, prioritizing privacy, scalability, and explicit engineering tradeoffs over guaranteed global reachability.

## Core Philosophy

AshiChat operates on a strict trust and privacy model:
* **No Unsolicited Messaging:** You can only communicate with peers who have explicitly invited you.
* **No Global Directory:** There is no public phone book, search function, or global peer discovery.
* **Serverless Operation:** The network is formed purely by the mesh of connected peers without centralized bootstrap dependencies.
* **Strictly Authenticated:** The system enforces strict mutual authentication with no Trust-On-First-Use (TOFU).

## Architecture & Protocol Stack

AshiChat implements a custom minimal viable protocol stack built from scratch over raw UDP datagrams.

### The Network Layers
* **Layer 1: Primitives:** Ed25519 for long-term identity, X25519 for ephemeral key exchange, HKDF-SHA256 for key derivation, and AES-256-GCM for encryption.
* **Layer 2: Wire Protocol:** A strict binary parser governing 8 distinct packet types (`HELLO`, `HELLO_ACK`, `DATA`, `ACK`, `ENDPOINT_UPDATE`, `RESOLVE_REQUEST`, `PING`, `PONG`). Any malformed packets or unsupported protocol versions are silently dropped.
* **Layer 3: Authentication:** Handshakes require cryptographic verification of the peer's public key against a local known-peer list before session keys are derived.
* **Layer 4: Transport & NAT Traversal:** Primarily operates over UDP with built-in STUN-less hole punching utilizing simultaneous packet bursts.
* **Layer 5: Orchestration & Overlay:** Nodes maintain a bounded random rotating subset of active peers (max 50) for routing messages via a TTL-limited recursive resolution protocol, maintaining constant-size memory state regardless of network size.
* **Layer 6: UI:** A responsive, modern Terminal User Interface built on the `Textual` framework.

### Storage & Crash Consistency
AshiChat enforces a local-first, offline-capable storage model:
* **Metadata:** SQLite is used strictly for metadata such as peer lists, sequence numbers, and offline message queues.
* **Encrypted Message Logs:** Messages are encrypted at rest using AES-256-GCM and stored in plain-file appended logs per peer (`~/.ashichat/messages/<peer_id>.log`).
* **Atomicity:** Data integrity is guaranteed through atomic write processes (`fsync` and atomic renames) to prevent corruption during power failures.

## Security Model

* **Identity:** Peer identity is uniquely defined by an immutable Ed25519 keypair generated on first launch.
* **Perfect Forward Secrecy:** Session keys are rotated upon every reconnection via a new handshake.
* **Replay Protection:** Encrypted payloads include monotonic, session-specific sequence numbers. Packets with repeated or older sequence numbers are immediately rejected.
* **Signed Endpoints:** IP endpoint updates propagated through the mesh are cryptographically signed using the peer's identity key and validated via monotonic version counters to prevent clock drift inconsistencies and spoofing.

## Installation & Usage

AshiChat is zero-config and plug-and-play. On the first run, it automatically generates keys, migrates local SQLite databases, and establishes encrypted log directories.

### Prerequisites
* Python 3.11+.

### Quick Start

1.  **Install the application:**
    ```bash
    pip install .
    ```
    *(For development, use `pip install -e ".[dev]"`)*

2.  **Launch the TUI:**
    ```bash
    ashichat
    ```

3.  **Connect:**
    * Press `i` in the TUI to generate and view your unique Invite Code (format: `ashichat://v1:<base58_encoded_public_key>`).
    * Share this code with a trusted contact. Connection, mutual authentication, and P2P hole-punching are handled automatically.

---

## Network Configuration (Firewall Rules)

AshiChat requires inbound UDP traffic on port `9000` (by default) to successfully establish direct peer-to-peer connections and participate in the mesh overlay. 

While AshiChat utilizes simultaneous UDP bursts for hole-punching to traverse NATs, strict local OS firewalls will drop these inbound packets if not explicitly allowed.

### Linux

**Using UFW (Ubuntu/Debian):**
```bash
sudo ufw allow 9000/udp
```

**Using Firewalld (CentOS/RHEL/Fedora):**
```bash
sudo firewall-cmd --permanent --add-port=9000/udp
sudo firewall-cmd --reload
```

**Using iptables:**
```bash
sudo iptables -A INPUT -p udp --dport 9000 -j ACCEPT
```

### Windows

Open **PowerShell as Administrator** and execute the following to create an inbound allow rule:
```powershell
New-NetFirewallRule -DisplayName "AshiChat UDP 9000" -Direction Inbound -LocalPort 9000 -Protocol UDP -Action Allow
```

---

## Configuration

AshiChat generates a simple `TOML` configuration file located at `~/.ashichat/config.toml`.

Example structure:
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
*Note: Debug logs are written to `~/.ashichat/debug.log` and are never output to the console to preserve the TUI state.*

## System Limitations (By Design)

* If all connected nodes are entirely partitioned from the rest of the mesh, global reachability is impossible.
* There is no cloud inbox. If a recipient is offline, the sender's node queues the encrypted message locally and retries via an exponential backoff engine (up to 7 days).
* The protocol is designed for always-on terminals, VPS, or desktop environments, and is hostile to mobile OS background execution limits.
* If both peers are behind symmetric NATs and UDP hole punching fails, a manual invite reconnection is required.

## Development & Testing

AshiChat is strictly modular to separate UI, networking, and cryptography components.

### Project Layout
```text
ashichat/
├── src/ashichat/
│   ├── crypto.py, identity.py        # Layer 1
│   ├── packet.py                     # Layer 2
│   ├── handshake.py, session.py      # Layer 3
│   ├── transport_udp.py, nat.py      # Layer 4
│   ├── node.py, overlay.py           # Layer 5
│   └── ui/tui.py                     # Layer 6
├── tests/                            # Pytest suite
└── main.py
```

### Running Tests
A robust test suite verifying cryptographic equality, storage atomicity, loop prevention, and network parsing is included.
```bash
pytest tests/
```
