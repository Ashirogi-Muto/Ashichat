"""Node orchestrator — central coordinator for AshiChat.

Wires together all subsystems: identity, transport, session, storage,
heartbeat, peer state, overlay, resolution, rate limiting, NAT, and queue.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ashichat.config import AshiChatConfig
from ashichat.heartbeat import HeartbeatManager
from ashichat.identity import load_or_generate_identity, LocalIdentity
from ashichat.logging_setup import get_logger
from ashichat.nat import NATTraversal
from ashichat.overlay import OverlayManager, PeerTable
from ashichat.packet import (
    AckPayload,
    DataPayload,
    EndpointUpdatePayload,
    HelloAckPayload,
    HelloPayload,
    Packet,
    PacketType,
    PingPayload,
    PongPayload,
    ResolveRequestPayload,
    SyncPayload,
    AppAckPayload,
    make_packet,
)
from ashichat.peer_state import PeerState, PeerStateManager
from ashichat.queue_manager import QueueManager
from ashichat.rate_limiter import GlobalRateLimiter, PeerRateLimiter
from ashichat.reconnect import ReconnectManager
from ashichat.resolution import ResolutionManager
from ashichat.session import Session, SessionRegistry
from ashichat.storage import MessageLog, OutboxStore, StorageManager
from ashichat.transport_udp import UDPTransport, start_udp_listener
from ashichat.handshake import create_hello, process_hello_ack
from ashichat.identity import derive_peer_id
from ashichat.invite import parse_invite
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

log = get_logger(__name__)


@dataclass(frozen=True)
class ContactAddResult:
    peer_id: bytes
    endpoint: tuple[str, int] | None
    connection_started: bool


class Node:
    """Central orchestrator — manages all subsystems."""

    def __init__(self, config: AshiChatConfig) -> None:
        self.config = config
        self.identity: LocalIdentity | None = None
        self.transport: UDPTransport | None = None
        self.session_registry = SessionRegistry()
        self.storage = StorageManager()
        self.message_log: MessageLog | None = None
        self.outbox: OutboxStore | None = None
        self.queue_manager = QueueManager()
        self.heartbeat: HeartbeatManager | None = None
        self.peer_states = PeerStateManager()
        self.peer_table: PeerTable | None = None
        self.overlay: OverlayManager | None = None
        self.resolver: ResolutionManager | None = None
        self.rate_limiter = PeerRateLimiter()
        self.global_limiter = GlobalRateLimiter()
        self.nat: NATTraversal | None = None
        self.reconnect = ReconnectManager()

        # Event callbacks for UI
        self._on_message_received: list[Callable] = []
        self._on_peer_state_changed: list[Callable] = []
        self._on_peers_changed: list[Callable[[], None]] = []
        self._on_connection_event: list[Callable[[str], None]] = []

        self._transport_obj: asyncio.DatagramTransport | None = None
        self._running = False
        self._pending_hello_by_peer: dict[bytes, object] = {}
        self._hello_retry_tasks: dict[bytes, asyncio.Task] = {}
        self._local_endpoint_version = 0

    # -- Event registration --------------------------------------------------

    def on_message_received(self, callback: Callable) -> None:
        self._on_message_received.append(callback)

    def on_peer_state_changed(self, callback: Callable) -> None:
        self._on_peer_state_changed.append(callback)
        self.peer_states.on_state_change(
            lambda pid, old, new: callback(pid, old, new)
        )

    def on_peers_changed(self, callback: Callable[[], None]) -> None:
        self._on_peers_changed.append(callback)

    def on_connection_event(self, callback: Callable[[str], None]) -> None:
        """Register callback for connection status messages (for UI feedback)."""
        self._on_connection_event.append(callback)

    # -- Lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Startup sequence.

        1. Load identity (generate on first run)
        2. Initialize storage (SQLite + message log)
        3. Load peer table from storage
        4. Load session sequence numbers from DB (source of truth)
        5. Start UDP listener
        6. Start reconnect loop
        7. Start overlay maintenance
        8. Start queue retry engine
        """
        log.info("Node starting...")

        # 1. Identity
        self.identity = load_or_generate_identity(self.config.base_dir)

        # 2. Storage
        db_path = self.config.base_dir / "data" / "ashichat.db"
        await self.storage.initialize(db_path)
        self.message_log = MessageLog(
            self.config.base_dir / "messages",
            self.config.storage.message_log_limit_mb,
            self.config.storage.max_log_rotations,
        )
        private_raw = self.identity.private_key.private_bytes(
            Encoding.Raw,
            PrivateFormat.Raw,
            NoEncryption(),
        )
        import hashlib as _hashlib
        outbox_key = _hashlib.sha256(private_raw + b"ashichat-outbox-v1").digest()
        self.outbox = OutboxStore(self.config.base_dir / "data" / "outbox", outbox_key)
        self._local_endpoint_version = await self.storage.get_local_endpoint_version()

        # 3. Peer table
        self.peer_table = PeerTable(self.identity.peer_id)
        peers = await self.storage.get_all_peers()
        for p in peers:
            self.peer_table.add_direct_peer(
                p.peer_id, p.public_key, p.nickname,
                _parse_endpoint(p.last_known_endpoint) if p.last_known_endpoint else None,
            )
            entry = self.peer_table.get_entry(p.peer_id)
            if entry is not None:
                entry.version_counter = p.version_counter
                entry.endpoint_signature = p.endpoint_signature
            if p.archived:
                await self.peer_states.update_state(p.peer_id, PeerState.ARCHIVED)

        # 3b. Restore session sequence floors from DB (source of truth).
        # We cannot restore full sessions (the encryption key is ephemeral),
        # but we store the last-known sequence numbers as floors. When a fresh
        # handshake creates a new session for a peer, the floors are applied
        # so sequence numbers never go backwards.
        self._sequence_floors: dict[bytes, tuple[int, int]] = {}  # peer_id → (send, recv)
        saved_sessions = await self.storage.load_all_sessions()
        for sr in saved_sessions:
            if self.peer_table.is_known(sr.peer_id):
                self._sequence_floors[sr.peer_id] = (sr.send_sequence, sr.recv_sequence)
                log.info(
                    "Loaded sequence floors for %s: send=%d recv=%d",
                    sr.peer_id.hex()[:8], sr.send_sequence, sr.recv_sequence,
                )

        # 3c. Reload queued outbound messages from SQLite into QueueManager
        saved_queue = await self.storage.load_all_queued_messages()
        for qr in saved_queue:
            plaintext = b""
            if self.outbox is not None:
                plaintext = await self.outbox.load_message(qr.message_id) or b""
            if not plaintext and qr.plaintext:
                plaintext = qr.plaintext
                if self.outbox is not None:
                    await self.outbox.store_message(qr.message_id, plaintext)
                await self.storage.clear_message_plaintext(qr.message_id)
            if not plaintext:
                log.warning(
                    "Queued message %s has no recoverable payload; marking failed",
                    qr.message_id[:8],
                )
                await self.storage.update_message_status(qr.message_id, "failed")
                continue
            self.queue_manager.restore_entry(
                qr.message_id, qr.receiver, qr.status, qr.retry_count,
                plaintext=plaintext,
                sequence_number=qr.sequence_number,
            )
        if saved_queue:
            log.info("Restored %d message records from DB", len(saved_queue))

        # 4. Overlay
        self.overlay = OverlayManager(
            self.peer_table,
            self.config.network.overlay_k,
        )
        self.overlay.select_overlay()
        await self.overlay.start_rotation_loop()

        # 5. Resolution
        self.resolver = ResolutionManager(
            local_peer_id=self.identity.peer_id,
            get_overlay_fn=self.overlay.get_overlay,
            get_endpoint_fn=self.peer_table.get_endpoint,
            send_fn=self._send_to,
            verify_endpoint_sig_fn=self._verify_endpoint_signature,
            update_endpoint_fn=self._apply_endpoint_update,
        )

        # 6. Heartbeat
        self.heartbeat = HeartbeatManager(
            send_fn=self._send_to,
            on_suspect=lambda pid: asyncio.create_task(
                self.peer_states.update_state(pid, PeerState.SUSPECT)
            ),
            on_disconnect=lambda pid: asyncio.create_task(
                self._handle_peer_disconnect(pid)
            ),
        )

        # 7. NAT
        self.nat = NATTraversal()

        # 8. UDP listener
        self._transport_obj, self.transport = await start_udp_listener(
            self.config.network.udp_port,
            self.handle_packet,
        )

        # 9. Reconnect loop
        self.reconnect.set_connect_fn(self._reconnect_peer)
        self.reconnect.set_archive_fn(
            lambda pid: self.set_peer_archived(pid, True)
        )
        await self.reconnect.start()

        # 10. Queue retry engine
        await self.queue_manager.start_retry_loop(
            resend_fn=self._resend_message,
            on_failed_fn=self._on_message_failed,
        )

        self._running = True
        log.info(
            "Node started — peer_id=%s, port=%d, %d known peers",
            self.identity.fingerprint(),
            self.config.network.udp_port,
            self.peer_table.size(),
        )

    async def stop(self) -> None:
        """Graceful shutdown: flush state, close transport, close DB."""
        self._running = False
        log.info("Node stopping...")

        await self.queue_manager.stop_retry_loop()
        if self.reconnect:
            await self.reconnect.stop()
        if self.heartbeat:
            await self.heartbeat.stop_all()
        if self.overlay:
            await self.overlay.stop()
        if self.transport:
            self.transport.close()
        if self.storage:
            # Flush all session states to DB
            for session in self.session_registry.all_sessions():
                await self.storage.save_session_state(
                    session.session_id,
                    session.remote_peer_id,
                    session.send_sequence,
                    session.recv_sequence,
                )
            await self.storage.close()

        log.info("Node stopped")

    # -- Core dispatch -------------------------------------------------------

    async def handle_packet(self, packet: Packet, addr: tuple[str, int]) -> None:
        """Central packet dispatcher."""
        log.debug("Received %s from %s:%d", packet.packet_type.name, addr[0], addr[1])

        # Per-peer rate limiting for types that have limits
        if packet.packet_type in (
            PacketType.DATA,
            PacketType.RESOLVE_REQUEST,
            PacketType.ENDPOINT_UPDATE,
        ):
            # Derive peer_id from source addr: check active sessions
            peer_id = self._peer_id_from_addr(addr)
            if peer_id is not None:
                if not self.rate_limiter.check(peer_id, packet.packet_type):
                    log.debug(
                        "Rate limited %s from %s",
                        packet.packet_type.name, peer_id.hex()[:8],
                    )
                    return

        handlers = {
            PacketType.HELLO: self._handle_hello,
            PacketType.HELLO_ACK: self._handle_hello_ack,
            PacketType.DATA: self._handle_data,
            PacketType.ACK: self._handle_ack,
            PacketType.PING: self._handle_ping,
            PacketType.PONG: self._handle_pong,
            PacketType.RESOLVE_REQUEST: self._handle_resolve_request,
            PacketType.ENDPOINT_UPDATE: self._handle_endpoint_update,
            PacketType.SYNC: self._handle_sync,
            PacketType.APP_ACK: self._handle_app_ack,
        }

        handler = handlers.get(packet.packet_type)
        if handler:
            try:
                await handler(packet, addr)
            except Exception:
                log.exception("Error handling %s from %s", packet.packet_type.name, addr)

    # -- Handlers ------------------------------------------------------------

    async def _handle_hello(self, packet: Packet, addr: tuple[str, int]) -> None:
        from ashichat.handshake import process_hello

        hello = HelloPayload.deserialize(packet.payload)
        remote_pub = Ed25519PublicKey.from_public_bytes(hello.identity_public_key)
        remote_peer_id = derive_peer_id(remote_pub)

        # Simultaneous HELLO guard: if we already have a session with this
        # peer, skip.  If we both sent HELLO at the same time, the node with
        # the *lower* peer_id yields (does not process the incoming HELLO) and
        # instead waits for its own HELLO_ACK to arrive.
        existing = self.session_registry.get_by_peer_id(remote_peer_id)
        if existing is not None:
            log.info("Already have session with %s — ignoring HELLO", remote_peer_id.hex()[:8])
            return

        if remote_peer_id in self._pending_hello_by_peer:
            # Both sides sent HELLO simultaneously.  Tiebreak: lower peer_id
            # becomes the initiator (waits for ACK), higher peer_id responds.
            if self.identity.peer_id < remote_peer_id:
                log.info("Simultaneous HELLO with %s — we have lower ID, staying initiator",
                         remote_peer_id.hex()[:8])
                return
            else:
                # We yield our initiator role; process their HELLO as receiver.
                self._pending_hello_by_peer.pop(remote_peer_id, None)
                log.info("Simultaneous HELLO with %s — we yield initiator role",
                         remote_peer_id.hex()[:8])

        # Auto-trust peers that can complete a valid invite-derived handshake.
        # This enables reciprocal contact creation on first connect.
        if remote_peer_id not in self.peer_table.get_direct_peer_ids():
            await self.storage.add_peer(
                remote_peer_id,
                hello.identity_public_key,
                endpoint=addr,
            )
            saved = await self.storage.get_peer(remote_peer_id)
            self.peer_table.add_direct_peer(
                remote_peer_id,
                hello.identity_public_key,
                nickname=saved.nickname if saved is not None else None,
                endpoint=addr,
            )
            self._notify_peers_changed()
            log.info("Auto-added new peer from HELLO: %s", remote_peer_id.hex()[:8])
        else:
            entry = self.peer_table.get_entry(remote_peer_id)
            if entry is not None:
                entry.endpoint = addr

        await self.storage.remember_endpoint(remote_peer_id, addr)

        known = self.peer_table.get_direct_peer_ids()
        result = process_hello(packet, known, self.identity)
        if result is None:
            return

        ack_pkt, keys = result
        self._send_to(ack_pkt, addr)

        # Register session
        session = Session(
            session_id=keys.session_id,
            encryption_key=keys.encryption_key,
            remote_peer_id=keys.remote_peer_id,
        )
        self._apply_sequence_floors(session)
        self.session_registry.register(session)
        await self.storage.save_session_state(
            session.session_id, session.remote_peer_id,
            session.send_sequence, session.recv_sequence,
        )
        await self._mark_peer_connected(keys.remote_peer_id)
        self.reconnect.record_connect(keys.remote_peer_id)
        self.heartbeat.register_peer(keys.remote_peer_id, keys.session_id, addr)
        await self.heartbeat.start_heartbeat(keys.remote_peer_id)
        await self._flush_queued_for_peer(keys.remote_peer_id)
        self._notify_connection_event(
            f"Session established with {keys.remote_peer_id.hex()[:8]} (receiver)"
        )
        log.info("Session established with %s (receiver)", keys.remote_peer_id.hex()[:8])
        # Send I_HAVE_UP_TO for chat synchronization
        await self._send_sync(session, addr)

    async def _handle_hello_ack(self, packet: Packet, addr: tuple[str, int]) -> None:
        # Derive remote peer_id from the ACK payload to do a NAT-safe lookup.
        ack_preview = HelloAckPayload.deserialize(packet.payload)
        remote_pub = Ed25519PublicKey.from_public_bytes(ack_preview.identity_public_key)
        remote_peer_id = derive_peer_id(remote_pub)

        state = self._pending_hello_by_peer.pop(remote_peer_id, None)
        if state is None:
            log.warning("No pending HELLO for peer %s — ignoring HELLO_ACK", remote_peer_id.hex()[:8])
            return

        # If we already got a session (e.g. from simultaneous HELLO), skip.
        if self.session_registry.get_by_peer_id(remote_peer_id) is not None:
            log.info("Already have session with %s — ignoring HELLO_ACK", remote_peer_id.hex()[:8])
            return

        known = self.peer_table.get_direct_peer_ids()
        keys = process_hello_ack(packet, state, known)
        if keys is None:
            return

        session = Session(
            session_id=keys.session_id,
            encryption_key=keys.encryption_key,
            remote_peer_id=keys.remote_peer_id,
        )
        self._apply_sequence_floors(session)
        self.session_registry.register(session)
        await self.storage.save_session_state(
            session.session_id,
            session.remote_peer_id,
            session.send_sequence,
            session.recv_sequence,
        )
        await self._mark_peer_connected(keys.remote_peer_id)
        self.reconnect.record_connect(keys.remote_peer_id)
        await self.storage.remember_endpoint(keys.remote_peer_id, addr)
        if self.peer_table:
            entry = self.peer_table.get_entry(keys.remote_peer_id)
            if entry:
                entry.endpoint = addr
        self.heartbeat.register_peer(keys.remote_peer_id, keys.session_id, addr)
        await self.heartbeat.start_heartbeat(keys.remote_peer_id)
        await self._flush_queued_for_peer(keys.remote_peer_id)
        # Cancel retry task since we got the ACK
        retry = self._hello_retry_tasks.pop(keys.remote_peer_id, None)
        if retry and not retry.done():
            retry.cancel()
        self._notify_connection_event(
            f"Session established with {keys.remote_peer_id.hex()[:8]} (initiator)"
        )
        log.info("Session established with %s (initiator)", keys.remote_peer_id.hex()[:8])
        # Send I_HAVE_UP_TO for chat synchronization
        await self._send_sync(session, addr)

    async def _handle_data(self, packet: Packet, addr: tuple[str, int]) -> None:
        dp = DataPayload.deserialize(packet.payload)
        session = self.session_registry.get_by_session_id(dp.session_id)
        if session is None:
            return

        plaintext = QueueManager.decrypt_data_packet(dp, session)
        if plaintext is None:
            return

        # Update recv_sequence in DB BEFORE sending ACK
        await self.storage.update_recv_sequence(session.session_id, session.recv_sequence)

        # Send ACK
        ack_pkt = QueueManager.build_ack(dp.session_id, dp.sequence_number)
        self._send_to(ack_pkt, addr)

        # Persist to message log
        await self.message_log.append_message(session.remote_peer_id, dp.ciphertext + dp.auth_tag)
        await self.storage.remember_endpoint(session.remote_peer_id, addr)
        if self.peer_table:
            entry = self.peer_table.get_entry(session.remote_peer_id)
            if entry is not None:
                entry.endpoint = addr

        # Notify UI
        for cb in self._on_message_received:
            try:
                cb(session.remote_peer_id, plaintext)
            except Exception:
                log.exception("Message callback error")

        self.heartbeat.record_traffic(session.remote_peer_id)

        # Send APP_ACK — message was displayed (application-level ack)
        app_ack_pkt = make_packet(
            PacketType.APP_ACK,
            AppAckPayload(
                session_id=dp.session_id,
                ack_sequence_number=dp.sequence_number,
            ),
        )
        self._send_to(app_ack_pkt, addr)

    async def _handle_ack(self, packet: Packet, addr: tuple[str, int]) -> None:
        ack = AckPayload.deserialize(packet.payload)
        msg_id = self.queue_manager.handle_ack(ack)
        if msg_id:
            await self.storage.update_message_status(msg_id, "delivered")

    async def _handle_ping(self, packet: Packet, addr: tuple[str, int]) -> None:
        ping = PingPayload.deserialize(packet.payload)
        pong = make_packet(PacketType.PONG, PongPayload(
            session_id=ping.session_id, ping_id=ping.ping_id
        ))
        self._send_to(pong, addr)

    async def _handle_pong(self, packet: Packet, addr: tuple[str, int]) -> None:
        pong = PongPayload.deserialize(packet.payload)
        if self.heartbeat:
            await self.heartbeat.handle_pong(pong)

    async def _handle_resolve_request(self, packet: Packet, addr: tuple[str, int]) -> None:
        req = ResolveRequestPayload.deserialize(packet.payload)
        if not self.global_limiter.check_resolve_forward():
            return

        # If we ARE the target, respond with our own endpoint.
        # Use the addr they reached us at — this is our externally-visible IP
        # from the requester's perspective (learned via reflection).
        if req.target_peer_id == self.identity.peer_id:
            bound_port = self._transport_obj.get_extra_info("sockname")[1] if getattr(self, "_transport_obj", None) else self.config.network.udp_port
            our_ep = (addr[0], bound_port)
            update_pkt = self._build_endpoint_update(our_ep)
            if update_pkt:
                self._send_to(update_pkt, addr)
            return

        # If we know the target's latest signed endpoint record, answer
        # directly with that cached ENDPOINT_UPDATE.
        if self.peer_table:
            entry = self.peer_table.get_entry(req.target_peer_id)
            if (
                entry is not None
                and entry.endpoint is not None
                and entry.endpoint_signature is not None
                and entry.version_counter > 0
            ):
                update_pkt = make_packet(
                    PacketType.ENDPOINT_UPDATE,
                    EndpointUpdatePayload(
                        peer_id=req.target_peer_id,
                        endpoint_ip=entry.endpoint[0],
                        endpoint_port=entry.endpoint[1],
                        version_counter=entry.version_counter,
                        signature=entry.endpoint_signature,
                    ),
                )
                self._send_to(update_pkt, addr)
                return

        # Fallback: if we only know a stale/raw endpoint without a signed
        # record, forward to the target so it can answer for itself.
        known_ep = self.peer_table.get_endpoint(req.target_peer_id) if self.peer_table else None
        if known_ep is not None:
            fwd_pkt = make_packet(PacketType.RESOLVE_REQUEST, req)
            self._send_to(fwd_pkt, known_ep)
            return

        # Otherwise, forward via the resolver (expanding-ring search)
        if self.resolver:
            await self.resolver.handle_resolve_request(req, addr)

    async def _handle_endpoint_update(self, packet: Packet, addr: tuple[str, int]) -> None:
        update = EndpointUpdatePayload.deserialize(packet.payload)
        if self.resolver:
            await self.resolver.handle_endpoint_update(update)

    async def _handle_sync(self, packet: Packet, addr: tuple[str, int]) -> None:
        """Handle incoming I_HAVE_UP_TO sync — retransmit any missed messages."""
        sync = SyncPayload.deserialize(packet.payload)
        session = self.session_registry.get_by_session_id(sync.session_id)
        if session is None:
            return

        peer_highest = sync.sequence_number
        # Find messages we sent with sequence > peer_highest and resend
        for msg_id, entry in list(self.queue_manager._queue.items()):
            if entry["receiver"] != session.remote_peer_id:
                continue
            seq = entry.get("sequence_number")
            if seq is not None and seq > peer_highest:
                pkt = self.queue_manager.rebuild_for_sync(msg_id, session)
                if pkt:
                    self._send_to(pkt, addr)
                    log.info(
                        "Resent msg %s (seq %d) to %s after SYNC",
                        msg_id[:8], seq, session.remote_peer_id.hex()[:8],
                    )

    async def _handle_app_ack(self, packet: Packet, addr: tuple[str, int]) -> None:
        """Handle application-level ACK — message was displayed by the peer."""
        app_ack = AppAckPayload.deserialize(packet.payload)
        session = self.session_registry.get_by_session_id(app_ack.session_id)
        if session is None:
            return

        # Find the message with this sequence and transition to ACKNOWLEDGED
        msg_id = self.queue_manager.acknowledge_by_sequence(
            session.remote_peer_id, app_ack.ack_sequence_number,
        )
        if msg_id:
            await self.storage.update_message_status(msg_id, "acknowledged")
            log.info(
                "Message %s ACKNOWLEDGED by %s",
                msg_id[:8], session.remote_peer_id.hex()[:8],
            )

    async def _send_sync(self, session: Session, addr: tuple[str, int]) -> None:
        """Send I_HAVE_UP_TO to peer after session establishment."""
        sync_pkt = make_packet(
            PacketType.SYNC,
            SyncPayload(
                session_id=session.session_id,
                sequence_number=session.recv_sequence,
            ),
        )
        self._send_to(sync_pkt, addr)

    # -- High-level messaging ------------------------------------------------

    async def send_message(self, peer_id: bytes, text: str) -> str | None:
        """Send a message to a peer. Returns message_id."""
        session = self.session_registry.get_by_peer_id(peer_id)
        msg_id = self.queue_manager.enqueue(peer_id, text.encode("utf-8"))

        # Persist to SQLite queue (including plaintext for restart safety)
        import time as _time
        plaintext_bytes = text.encode("utf-8")
        if self.outbox is not None:
            await self.outbox.store_message(msg_id, plaintext_bytes)
        await self.storage.enqueue_message(msg_id, peer_id, _time.time())

        if session is None:
            log.info("Peer %s not connected — message queued", peer_id.hex()[:8])
            endpoint = self.peer_table.get_endpoint(peer_id) if self.peer_table else None
            if endpoint:
                await self.connect_to_peer(peer_id, endpoint)
            elif self.resolver:
                # Attempt resolution via overlay network
                resolved = await self.resolver.resolve_peer(peer_id)
                if resolved:
                    await self.connect_to_peer(peer_id, resolved)
            return msg_id

        result = self.queue_manager.build_data_packet(msg_id, session)
        if result is None:
            return msg_id

        pkt, seq = result
        # Update send_sequence in DB BEFORE sending
        await self.storage.update_send_sequence(session.session_id, session.send_sequence)
        await self.storage.update_message_sequence(msg_id, seq)
        # Update queue status to pending
        await self.storage.update_message_status(msg_id, "pending")

        endpoint = self.peer_table.get_endpoint(peer_id)
        if endpoint:
            self._send_to(pkt, endpoint)

        return msg_id

    async def add_contact_from_invite(self, invite_code: str) -> ContactAddResult:
        """Parse an invite, persist the peer, and start a connection if possible."""
        if self.identity is None or self.peer_table is None:
            raise RuntimeError("node not running")

        data = parse_invite(invite_code)
        pub_raw = data.public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
        peer_id = derive_peer_id(Ed25519PublicKey.from_public_bytes(pub_raw))
        if peer_id == self.identity.peer_id:
            raise ValueError("cannot add your own invite")

        await self.storage.add_peer(peer_id, pub_raw, endpoint=data.endpoint)
        saved = await self.storage.get_peer(peer_id)
        if saved is None:
            raise RuntimeError("peer insert verification failed")

        entry = self.peer_table.get_entry(peer_id)
        if entry is None:
            self.peer_table.add_direct_peer(
                peer_id,
                pub_raw,
                nickname=saved.nickname,
                endpoint=data.endpoint or _parse_endpoint(saved.last_known_endpoint),
            )
        else:
            entry.is_direct = True
            entry.public_key = pub_raw
            if saved.nickname is not None:
                entry.nickname = saved.nickname
            if data.endpoint is not None:
                entry.endpoint = data.endpoint

        self._notify_peers_changed()

        connection_started = False
        if data.endpoint is not None:
            await self.connect_to_peer(peer_id, data.endpoint)
            connection_started = True

        return ContactAddResult(
            peer_id=peer_id,
            endpoint=data.endpoint,
            connection_started=connection_started,
        )

    async def connect_to_peer(self, peer_id: bytes, endpoint: tuple[str, int]) -> None:
        """Initiate HELLO to peer endpoint with retry."""
        if self.identity is None or self.transport is None:
            log.warning("Cannot connect — node not fully started")
            return
        if self.session_registry.get_by_peer_id(peer_id) is not None:
            log.info("Already connected to %s", peer_id.hex()[:8])
            return
        # Don't send duplicate HELLOs
        if peer_id in self._pending_hello_by_peer:
            log.info("Already have pending HELLO for %s", peer_id.hex()[:8])
            return

        hello_pkt, state = create_hello(self.identity)
        state.target_peer_id = peer_id
        self._pending_hello_by_peer[peer_id] = state
        await self.storage.remember_endpoint(peer_id, endpoint)
        if self.peer_table:
            entry = self.peer_table.get_entry(peer_id)
            if entry:
                entry.endpoint = endpoint
        try:
            await self.peer_states.update_state(peer_id, PeerState.CONNECTING)
        except ValueError:
            pass

        # NAT hole punch: fire-and-forget background burst to open NAT.
        # Don't await — send HELLO immediately, the burst runs concurrently.
        if self.nat:
            asyncio.create_task(self.nat.punch_hole(endpoint, self._send_to))

        # Send first HELLO immediately, then launch retry task
        self._send_to(hello_pkt, endpoint)
        log.info("HELLO sent to %s at %s:%d (attempt 1)", peer_id.hex()[:8], endpoint[0], endpoint[1])
        self._notify_connection_event(
            f"Sending HELLO to {peer_id.hex()[:8]} at {endpoint[0]}:{endpoint[1]}..."
        )

        # Background retry task
        async def _retry_hello():
            try:
                for attempt in range(2, 8):  # attempts 2-7 (total 7 including the first)
                    await asyncio.sleep(3)
                    # Stop if session was established or state consumed
                    if self.session_registry.get_by_peer_id(peer_id) is not None:
                        return
                    if peer_id not in self._pending_hello_by_peer:
                        return
                    self._send_to(hello_pkt, endpoint)
                    log.info("HELLO retry to %s (attempt %d/7)", peer_id.hex()[:8], attempt)
                    self._notify_connection_event(
                        f"HELLO retry {attempt}/7 to {peer_id.hex()[:8]}..."
                    )
                # All retries exhausted
                if self.session_registry.get_by_peer_id(peer_id) is None:
                    self._pending_hello_by_peer.pop(peer_id, None)
                    log.warning("HELLO to %s timed out after 7 attempts", peer_id.hex()[:8])
                    self._notify_connection_event(
                        f"Connection to {peer_id.hex()[:8]} failed — no response after 7 attempts."
                    )
                    try:
                        await self.peer_states.update_state(peer_id, PeerState.DISCONNECTED)
                    except ValueError:
                        pass
                    self.reconnect.record_disconnect(peer_id)
            except asyncio.CancelledError:
                pass

        # Cancel any existing retry for this peer
        old_task = self._hello_retry_tasks.pop(peer_id, None)
        if old_task and not old_task.done():
            old_task.cancel()
        self._hello_retry_tasks[peer_id] = asyncio.create_task(_retry_hello())

    async def _flush_queued_for_peer(self, peer_id: bytes) -> None:
        """Send queued messages immediately once a session exists."""
        session = self.session_registry.get_by_peer_id(peer_id)
        if session is None:
            return
        endpoint = self.peer_table.get_endpoint(peer_id) if self.peer_table else None
        if endpoint is None:
            return

        for msg_id in self.queue_manager.get_queued_for_peer(peer_id):
            result = self.queue_manager.build_data_packet(msg_id, session)
            if result is None:
                continue
            pkt, _seq = result
            await self.storage.update_send_sequence(session.session_id, session.send_sequence)
            await self.storage.update_message_sequence(msg_id, _seq)
            await self.storage.update_message_status(msg_id, "pending")
            self._send_to(pkt, endpoint)

    async def _resend_message(self, msg_id: str, peer_id: bytes) -> None:
        """Re-encrypt and re-send a timed-out message."""
        session = self.session_registry.get_by_peer_id(peer_id)
        if session is None:
            return
        endpoint = self.peer_table.get_endpoint(peer_id) if self.peer_table else None
        if endpoint is None:
            return
        result = self.queue_manager.build_data_packet(msg_id, session)
        if result is None:
            return
        pkt, _seq = result
        await self.storage.update_send_sequence(session.session_id, session.send_sequence)
        await self.storage.update_message_sequence(msg_id, _seq)
        await self.storage.update_message_status(msg_id, "pending")
        self._send_to(pkt, endpoint)

    async def _on_message_failed(self, msg_id: str, peer_id: bytes) -> None:
        """Handle a message that exhausted all retries."""
        await self.storage.update_message_status(msg_id, "failed")

    def _apply_sequence_floors(self, session: Session) -> None:
        """Apply saved sequence floors to a new session to prevent reuse.

        After a restart, the old session's encryption key is gone but we
        remember the last-used sequence numbers.  A fresh handshake starts
        sequences at 0, so we bump them above the saved floor values.
        """
        floors = getattr(self, "_sequence_floors", {})
        floor = floors.get(session.remote_peer_id)
        if floor is None:
            return
        send_floor, recv_floor = floor
        if send_floor > session.send_sequence:
            session.send_sequence = send_floor
        if recv_floor > session.recv_sequence:
            session.recv_sequence = recv_floor
        log.info(
            "Applied sequence floors for %s: send≥%d recv≥%d",
            session.remote_peer_id.hex()[:8], send_floor, recv_floor,
        )
        # Consume the floor — only apply once per peer per restart
        del floors[session.remote_peer_id]

    async def rename_peer(self, peer_id: bytes, nickname: str) -> None:
        nickname = nickname.strip()
        if not nickname:
            raise ValueError("nickname cannot be empty")
        await self.storage.update_peer_nickname(peer_id, nickname)
        entry = self.peer_table.get_entry(peer_id) if self.peer_table else None
        if entry:
            entry.nickname = nickname
        self._notify_peers_changed()

    async def set_peer_archived(self, peer_id: bytes, archived: bool) -> None:
        await self.storage.set_peer_archived(peer_id, archived)
        target = PeerState.ARCHIVED if archived else PeerState.DISCONNECTED
        current = self.peer_states.get_state(peer_id)
        if current == target:
            self._notify_peers_changed()
            return
        try:
            await self.peer_states.update_state(peer_id, target)
        except ValueError:
            if archived and current != PeerState.DISCONNECTED:
                await self.peer_states.update_state(peer_id, PeerState.DISCONNECTED)
                await self.peer_states.update_state(peer_id, PeerState.ARCHIVED)
            elif not archived and current == PeerState.ARCHIVED:
                await self.peer_states.update_state(peer_id, PeerState.DISCONNECTED)
        self._notify_peers_changed()

    async def remove_peer(self, peer_id: bytes) -> None:
        if self.outbox:
            for message_id, entry in list(self.queue_manager._queue.items()):
                if entry["receiver"] == peer_id:
                    await self.outbox.delete_message(message_id)
        await self.storage.remove_peer(peer_id)
        if self.peer_table:
            self.peer_table.remove_peer(peer_id)
        session = self.session_registry.get_by_peer_id(peer_id)
        if session is not None:
            self.session_registry.remove(session.session_id)
        self._notify_peers_changed()

    async def get_known_peers(self):
        """Return current peers from persistent storage."""
        return await self.storage.get_all_peers()

    # -- Internal helpers ----------------------------------------------------

    def _peer_id_from_addr(self, addr: tuple[str, int]) -> bytes | None:
        """Resolve a source address to a peer_id via sessions or peer table."""
        # Check heartbeat peer info (fastest — maps addr to peer)
        if self.heartbeat:
            for pid, (_, peer_addr) in self.heartbeat._peer_info.items():
                if peer_addr == addr:
                    return pid
        # Fallback: check peer table entries
        if self.peer_table:
            for entry in self.peer_table.get_direct_peers():
                if entry.endpoint == addr:
                    return entry.peer_id
        return None

    def _send_to(self, packet: Packet, addr: tuple[str, int]) -> None:
        if self.transport:
            self.transport.send_packet(packet, addr)

    def _build_endpoint_update(
        self, endpoint: tuple[str, int], peer_id: bytes | None = None,
    ) -> Packet | None:
        """Build a signed ENDPOINT_UPDATE packet.

        If *peer_id* is ``None``, advertises our own endpoint.
        """
        if self.identity is None:
            return None
        target_id = peer_id or self.identity.peer_id
        version = 0
        if target_id == self.identity.peer_id:
            self._local_endpoint_version += 1
            version = self._local_endpoint_version
            asyncio.ensure_future(
                self.storage.set_local_endpoint_version(self._local_endpoint_version)
            )
        elif self.peer_table:
            entry = self.peer_table.get_entry(target_id)
            if entry:
                version = entry.version_counter

        ip_str, port = endpoint
        # Build signature: sign(identity_private, endpoint || version_counter || info)
        import struct as _struct
        sig_data = (
            ip_str.encode("utf-8")
            + _struct.pack(">H", port)
            + _struct.pack(">Q", version)
            + b"ashichat-endpoint-v1"
        )
        signature = self.identity.sign(sig_data)

        update = EndpointUpdatePayload(
            peer_id=target_id,
            endpoint_ip=ip_str,
            endpoint_port=port,
            version_counter=version,
            signature=signature,
        )
        return make_packet(PacketType.ENDPOINT_UPDATE, update)

    def _verify_endpoint_signature(self, update: EndpointUpdatePayload) -> bool:
        """Verify the Ed25519 signature on an ENDPOINT_UPDATE."""
        if not self.peer_table:
            return False
        entry = self.peer_table.get_entry(update.peer_id)
        if entry is None:
            return False
        try:
            remote_pub = Ed25519PublicKey.from_public_bytes(entry.public_key)
        except Exception:
            return False
        import struct as _struct
        from ashichat.identity import verify_signature
        sig_data = (
            update.endpoint_ip.encode("utf-8")
            + _struct.pack(">H", update.endpoint_port)
            + _struct.pack(">Q", update.version_counter)
            + b"ashichat-endpoint-v1"
        )
        return verify_signature(remote_pub, sig_data, update.signature)

    def _apply_endpoint_update(
        self,
        peer_id: bytes,
        endpoint: tuple[str, int],
        version: int,
        signature: bytes | None = None,
    ) -> None:
        """Persist a verified endpoint update to peer table and queue DB update."""
        if self.peer_table:
            entry = self.peer_table.get_entry(peer_id)
            if entry and version > entry.version_counter:
                entry.endpoint = endpoint
                entry.version_counter = version
                if signature is not None:
                    entry.endpoint_signature = signature
        # Schedule async DB write (fire-and-forget from sync context)
        asyncio.ensure_future(
            self.storage.update_endpoint(
                peer_id,
                f"{endpoint[0]}:{endpoint[1]}",
                version,
                signature,
            )
        )

    async def broadcast_endpoint_update(self, endpoint: tuple[str, int]) -> None:
        """Push a signed ENDPOINT_UPDATE to all DirectPeers + OverlayPeers.

        Called when our node detects its own IP has changed.
        """
        pkt = self._build_endpoint_update(endpoint)
        if pkt is None:
            return

        # Send to all direct peers
        if self.peer_table:
            for entry in self.peer_table.get_direct_peers():
                if entry.endpoint:
                    self._send_to(pkt, entry.endpoint)

        # Send to overlay peers
        if self.overlay:
            for pid in self.overlay.get_overlay():
                ep = self.peer_table.get_endpoint(pid) if self.peer_table else None
                if ep:
                    self._send_to(pkt, ep)

        log.info("Broadcast endpoint update to all peers: %s:%d", *endpoint)

    def _notify_peers_changed(self) -> None:
        for cb in self._on_peers_changed:
            try:
                cb()
            except Exception:
                log.exception("Peer list callback error")

    def _notify_connection_event(self, message: str) -> None:
        """Push a connection status message to UI listeners."""
        for cb in self._on_connection_event:
            try:
                cb(message)
            except Exception:
                log.exception("Connection event callback error")

    async def _mark_peer_connected(self, peer_id: bytes) -> None:
        """Move a peer to CONNECTED, handling any prior state gracefully."""
        current = self.peer_states.get_state(peer_id)
        if current == PeerState.CONNECTED:
            return
        try:
            if current == PeerState.DISCONNECTED:
                await self.peer_states.update_state(peer_id, PeerState.CONNECTING)
            elif current == PeerState.ARCHIVED:
                await self.peer_states.update_state(peer_id, PeerState.CONNECTING)
            elif current == PeerState.RESOLVING:
                await self.peer_states.update_state(peer_id, PeerState.CONNECTING)
            elif current == PeerState.FAILED:
                await self.peer_states.update_state(peer_id, PeerState.DISCONNECTED)
                await self.peer_states.update_state(peer_id, PeerState.CONNECTING)
            # Now transition CONNECTING -> CONNECTED
            await self.peer_states.update_state(peer_id, PeerState.CONNECTED)
        except ValueError:
            log.warning(
                "State transition error for %s (current=%s) — forcing CONNECTED",
                peer_id.hex()[:8], current.value,
            )
            # Force the state machine to CONNECTED by resetting it
            machine = self.peer_states.ensure_machine(peer_id)
            machine._state = PeerState.CONNECTING
            machine._last_change = __import__('time').time()
            await self.peer_states.update_state(peer_id, PeerState.CONNECTED)
        self._notify_peers_changed()

    async def _handle_peer_disconnect(self, peer_id: bytes) -> None:
        """Handle heartbeat-detected disconnect: update state + enqueue for reconnect."""
        try:
            await self.peer_states.update_state(peer_id, PeerState.DISCONNECTED)
        except ValueError:
            pass
        # Remove stale session
        session = self.session_registry.get_by_peer_id(peer_id)
        if session is not None:
            self.session_registry.remove(session.session_id)
        # Enqueue for reconnection backoff loop
        self.reconnect.record_disconnect(peer_id)
        self._notify_peers_changed()

    async def _reconnect_peer(self, peer_id: bytes) -> None:
        """Called by ReconnectManager loop to attempt reconnection to a peer."""
        if self.session_registry.get_by_peer_id(peer_id) is not None:
            # Already connected
            self.reconnect.record_connect(peer_id)
            return

        attempt_count = self.reconnect.get_attempt_count(peer_id)
        endpoint = self.peer_table.get_endpoint(peer_id) if self.peer_table else None
        if self.resolver and (endpoint is None or attempt_count > 0):
            try:
                await self.peer_states.update_state(peer_id, PeerState.RESOLVING)
            except ValueError:
                pass
            resolved = await self.resolver.resolve_peer(
                peer_id,
                force_network=endpoint is not None,
            )
            if resolved is not None:
                endpoint = resolved
                await self.storage.remember_endpoint(peer_id, endpoint)
                if self.peer_table:
                    entry = self.peer_table.get_entry(peer_id)
                    if entry is not None:
                        entry.endpoint = endpoint
            elif endpoint is None:
                try:
                    await self.peer_states.update_state(peer_id, PeerState.FAILED)
                except ValueError:
                    pass
                log.info("No resolvable endpoint for %s", peer_id.hex()[:8])
                return

        if endpoint is None:
            log.info("No endpoint for %s — cannot reconnect", peer_id.hex()[:8])
            return
        await self.connect_to_peer(peer_id, endpoint)


def _parse_endpoint(ep: str) -> tuple[str, int] | None:
    """Parse 'ip:port' string to tuple."""
    try:
        host, port_str = ep.rsplit(":", 1)
        return (host, int(port_str))
    except (ValueError, AttributeError):
        return None
