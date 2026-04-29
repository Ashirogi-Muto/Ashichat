# AshiChat — Project Summary

## What is AshiChat?
AshiChat is a decentralized, invite-only, peer-to-peer (P2P) terminal-based messaging system. It is designed for users who prioritize privacy, sovereignty, and direct connections over convenience or global discoverability.

Unlike Signal or Telegram, AshiChat has **zero central servers**. There is no "phone book," no global directory, and no way to search for users. You can only communicate with people who have explicitly invited you.

## Core Philosophy
1.  **Strictly Private:** No public directory. No "find my friends." Connect only via cryptographic invites.
2.  **Serverless:** No bootstrap servers. The network is formed purely by the mesh of connected peers.
3.  **Trust-Based:** You only connect to people you trust (or people they trust, to a limited degree).
4.  **Ephemeral & Persistent:** Messages are end-to-end encrypted and stored locally. Identity is permanent but revocable only by abandoning it.

## Key Features

### 🔐 Identity & Security
-   **Identity:** Users have a single **Root Identity**, with individual devices holding distinct **Device Keys** signed by the root. Users are identified by public keys, not phone numbers or IPs.
-   **Encryption:** All messages are **E2E encrypted** (AES-256-GCM) with Perfect Forward Secrecy (session keys rotate on reconnect).
-   **Mutual Auth:** Strict mutual authentication. No Trust-On-First-Use (TOFU). You must know a peer's key to talk to them.
-   **Metadata Privacy:** Packet headers are minimized. Traffic analysis is mitigated by the P2P mesh structure.

### 🌐 Networking & Resilience
-   **Transport:** Primarily **UDP** with custom binary framing. Optional TCP fallback.
-   **NAT Traversal:** Built-in hole punching to connect peers behind home routers without central STUN servers.
-   **Overlay Mesh:** Nodes maintain a small "active" view of the network (max 50 peers) to route messages without needing a full map of the internet.
-   **Probabilistic Routing:** Messages find their destination via "gossip" and recursive resolution, not a central switchboard.

### 💾 Storage & Reliability
-   **Local-First:** All data lives on your device.
-   **Databases:**
    -   **SQLite:** Stores metadata (peer list, queue status).
    -   **Encrypted Logs:** Message content is plain-file appended (encrypted at rest).
-   **Crash Safety:** Atomic writes ensure data integrity even if the power fails.
-   **Offline Queue:** Messages are queued and retried automatically when a peer comes online (up to 7 days).
-   **Multi-Device Sync:** Devices belonging to the same user automatically discover each other and seamlessly sync chat history and queues using P2P log replication.

### 🖥️ User Experience
-   **Interface:** A rich **Terminal User Interface (TUI)** built with `Textual`. Mouse support, scrolling, and status indicators.
-   **Configuration:** Simple `TOML` configuration file (`~/.ashichat/config.toml`).
-   **Zero-Config Startup:** Auto-generates keys and identity on first run.

## Technical Architecture

### Stack
-   **Language:** Python 3.11+
-   **Async Core:** `asyncio` for high-concurrency P2P networking.
-   **Cryptography:** Standard `cryptography` library (no experimental crypto).
-   **UI:** `Textual` (Modern TUI framework).

### Data Flow
1.  **Alice** wants to msg **Bob**.
2.  Alice's node checks if Bob is connected.
3.  **If Connected:** Sends encrypted `DATA` packet directly via UDP.
4.  **If Disconnected:**
    -   Alice sends `RESOLVE_REQUEST` to her mesh peers ("Do you know Bob?").
    -   Peers route the request through the overlay.
    -   If Bob is found, his current IP is returned (signed by Bob).
    -   Alice connects, shakes hands, and delivers the message.

## Limitations (By Design)
-   **No Global Reachability:** If you and your friends are completely partitioned from the rest of the mesh, you cannot talk.
-   **No Offline Inbox:** If Bob is offline, Alice holds the message until he returns. There is no cloud server to hold it for him.
-   **Mobile Hostile:** Designed for always-on terminals/VPS/desktops, not mobile phones with aggressive background killing.

## Project Status
-   **Current State:** Requirements Hardened. Detailed protocol specifications are located in the `docs/` directory.
-   **Next Step:** Implementation Phase 0 (Bootstrap).
