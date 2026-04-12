"""Node orchestrator — central coordinator for AshiChat.

Wires together all subsystems: identity, transport, session, storage,
heartbeat, peer state, overlay, resolution, rate limiting, NAT, and queue.
"""

from __future__ import annotations

import asyncio
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
    make_packet,
)
from ashichat.peer_state import PeerState, PeerStateManager
from ashichat.queue_manager import QueueManager
from ashichat.rate_limiter import GlobalRateLimiter, PeerRateLimiter
from ashichat.reconnect import ReconnectManager
from ashichat.resolution import ResolutionManager
from ashichat.session import Session, SessionRegistry
from ashichat.storage import MessageLog, StorageManager
from ashichat.transport_udp import UDPTransport, start_udp_listener

log = get_logger(__name__)


class Node:
    """Central orchestrator — manages all subsystems."""

    def __init__(self, config: AshiChatConfig) -> None:
        self.config = config
        self.identity: LocalIdentity | None = None
        self.transport: UDPTransport | None = None
        self.session_registry = SessionRegistry()
        self.storage = StorageManager()
        self.message_log: MessageLog | None = None
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

        self._transport_obj: asyncio.DatagramTransport | None = None
        self._running = False

    # -- Event registration --------------------------------------------------

    def on_message_received(self, callback: Callable) -> None:
        self._on_message_received.append(callback)

    def on_peer_state_changed(self, callback: Callable) -> None:
        self._on_peer_state_changed.append(callback)
        self.peer_states.on_state_change(
            lambda pid, old, new: callback(pid, old, new)
        )

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

        # 3. Peer table
        self.peer_table = PeerTable(self.identity.peer_id)
        peers = await self.storage.get_all_peers()
        for p in peers:
            self.peer_table.add_direct_peer(
                p.peer_id, p.public_key, p.nickname,
                _parse_endpoint(p.last_known_endpoint) if p.last_known_endpoint else None,
            )
            if p.archived:
                await self.peer_states.update_state(p.peer_id, PeerState.ARCHIVED)

        # 4. Overlay
        self.overlay = OverlayManager(
            self.peer_table,
            self.config.network.overlay_k,
        )
        self.overlay.select_overlay()

        # 5. Resolution
        self.resolver = ResolutionManager(
            local_peer_id=self.identity.peer_id,
            get_overlay_fn=self.overlay.get_overlay,
            get_endpoint_fn=self.peer_table.get_endpoint,
            send_fn=self._send_to,
        )

        # 6. Heartbeat
        self.heartbeat = HeartbeatManager(
            send_fn=self._send_to,
            on_suspect=lambda pid: asyncio.create_task(
                self.peer_states.update_state(pid, PeerState.SUSPECT)
            ),
            on_disconnect=lambda pid: asyncio.create_task(
                self.peer_states.update_state(pid, PeerState.DISCONNECTED)
            ),
        )

        # 7. NAT
        self.nat = NATTraversal()

        # 8. UDP listener
        self._transport_obj, self.transport = await start_udp_listener(
            self.config.network.udp_port,
            self.handle_packet,
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
        handlers = {
            PacketType.HELLO: self._handle_hello,
            PacketType.HELLO_ACK: self._handle_hello_ack,
            PacketType.DATA: self._handle_data,
            PacketType.ACK: self._handle_ack,
            PacketType.PING: self._handle_ping,
            PacketType.PONG: self._handle_pong,
            PacketType.RESOLVE_REQUEST: self._handle_resolve_request,
            PacketType.ENDPOINT_UPDATE: self._handle_endpoint_update,
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
        self.session_registry.register(session)
        await self.storage.save_session_state(
            session.session_id, session.remote_peer_id,
            session.send_sequence, session.recv_sequence,
        )
        await self.peer_states.update_state(keys.remote_peer_id, PeerState.CONNECTED)
        self.heartbeat.register_peer(keys.remote_peer_id, keys.session_id, addr)
        await self.heartbeat.start_heartbeat(keys.remote_peer_id)
        log.info("Session established with %s (receiver)", keys.remote_peer_id.hex()[:8])

    async def _handle_hello_ack(self, packet: Packet, addr: tuple[str, int]) -> None:
        # The handshake state would normally be tracked — simplified here
        pass

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

        # Notify UI
        for cb in self._on_message_received:
            try:
                cb(session.remote_peer_id, plaintext)
            except Exception:
                log.exception("Message callback error")

        self.heartbeat.record_traffic(session.remote_peer_id)

    async def _handle_ack(self, packet: Packet, addr: tuple[str, int]) -> None:
        ack = AckPayload.deserialize(packet.payload)
        self.queue_manager.handle_ack(ack)

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
        if self.resolver:
            await self.resolver.handle_resolve_request(req, addr)

    async def _handle_endpoint_update(self, packet: Packet, addr: tuple[str, int]) -> None:
        update = EndpointUpdatePayload.deserialize(packet.payload)
        if self.resolver:
            await self.resolver.handle_endpoint_update(update)

    # -- High-level messaging ------------------------------------------------

    async def send_message(self, peer_id: bytes, text: str) -> str | None:
        """Send a message to a peer. Returns message_id."""
        session = self.session_registry.get_by_peer_id(peer_id)
        msg_id = self.queue_manager.enqueue(peer_id, text.encode("utf-8"))

        if session is None:
            log.info("Peer %s not connected — message queued", peer_id.hex()[:8])
            return msg_id

        result = self.queue_manager.build_data_packet(msg_id, session)
        if result is None:
            return msg_id

        pkt, seq = result
        # Update send_sequence in DB BEFORE sending
        await self.storage.update_send_sequence(session.session_id, session.send_sequence)

        endpoint = self.peer_table.get_endpoint(peer_id)
        if endpoint:
            self._send_to(pkt, endpoint)

        return msg_id

    async def rename_peer(self, peer_id: bytes, nickname: str) -> None:
        nickname = nickname.strip()
        if not nickname:
            raise ValueError("nickname cannot be empty")
        await self.storage.update_peer_nickname(peer_id, nickname)
        entry = self.peer_table.get_entry(peer_id) if self.peer_table else None
        if entry:
            entry.nickname = nickname

    async def set_peer_archived(self, peer_id: bytes, archived: bool) -> None:
        await self.storage.set_peer_archived(peer_id, archived)
        target = PeerState.ARCHIVED if archived else PeerState.DISCONNECTED
        current = self.peer_states.get_state(peer_id)
        if current == target:
            return
        try:
            await self.peer_states.update_state(peer_id, target)
        except ValueError:
            if archived and current != PeerState.DISCONNECTED:
                await self.peer_states.update_state(peer_id, PeerState.DISCONNECTED)
                await self.peer_states.update_state(peer_id, PeerState.ARCHIVED)
            elif not archived and current == PeerState.ARCHIVED:
                await self.peer_states.update_state(peer_id, PeerState.DISCONNECTED)

    async def remove_peer(self, peer_id: bytes) -> None:
        await self.storage.remove_peer(peer_id)
        if self.peer_table:
            self.peer_table.remove_peer(peer_id)
        session = self.session_registry.get_by_peer_id(peer_id)
        if session is not None:
            self.session_registry.remove(session.session_id)

    async def get_known_peers(self):
        """Return current peers from persistent storage."""
        return await self.storage.get_all_peers()

    # -- Internal helpers ----------------------------------------------------

    def _send_to(self, packet: Packet, addr: tuple[str, int]) -> None:
        if self.transport:
            self.transport.send_packet(packet, addr)


def _parse_endpoint(ep: str) -> tuple[str, int] | None:
    """Parse 'ip:port' string to tuple."""
    try:
        host, port_str = ep.rsplit(":", 1)
        return (host, int(port_str))
    except (ValueError, AttributeError):
        return None
