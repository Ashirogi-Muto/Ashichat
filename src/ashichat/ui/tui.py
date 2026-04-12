"""AshiChat Textual TUI — main application."""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header

from ashichat.logging_setup import get_logger
from ashichat.ui.components import (
    ChatView,
    MessageInput,
    PeerManageDialog,
    QuitConfirmDialog,
    Sidebar,
)

log = get_logger(__name__)


class AshiChatApp(App):
    """Terminal UI for AshiChat."""

    CSS_PATH = "styles.tcss"
    TITLE = "AshiChat v0.1.0"

    BINDINGS = [
        Binding("q", "request_quit", "Quit", priority=True),
        Binding("tab", "focus_next", "Next", priority=True),
        Binding("escape", "unfocus", "Unfocus", priority=True),
        Binding("i", "show_invite", "Invite", priority=True),
        Binding("p", "show_profile", "Profile", priority=True),
        Binding("s", "show_settings", "Settings", priority=True),
        Binding("m", "manage_peer", "Manage", priority=True),
    ]

    def __init__(self, node=None) -> None:
        super().__init__()
        self.node = node
        self._active_peer_id: bytes | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Sidebar(id="sidebar")
        yield ChatView(id="chat-view")
        yield MessageInput(id="message-input")
        yield Footer()

    async def on_mount(self) -> None:
        """Subscribe to node state changes if node is provided."""
        if self.node:
            self.node.on_message_received(self._handle_incoming)
            self.node.on_peer_state_changed(self._handle_state_change)
            self.node.on_peers_changed(self._handle_peers_changed)
            await self.refresh_peers_from_node()
        else:
            self.query_one("#chat-view", ChatView).add_system_message("Node not attached.")
        log.info("TUI mounted")

    def _handle_incoming(self, peer_id: bytes, plaintext: bytes) -> None:
        self.call_from_thread(self._do_handle_incoming, peer_id, plaintext)

    def _do_handle_incoming(self, peer_id: bytes, plaintext: bytes) -> None:
        sidebar = self.query_one("#sidebar", Sidebar)
        chat = self.query_one("#chat-view", ChatView)
        chat.add_message(peer_id, plaintext.decode("utf-8", errors="replace"), incoming=True)
        if sidebar.get_active_peer_id() != peer_id:
            sidebar.increment_unread(peer_id)

    def _handle_state_change(self, peer_id: bytes, old, new) -> None:
        self.call_from_thread(self._do_handle_state_change, peer_id, old, new)

    def _do_handle_state_change(self, peer_id: bytes, old, new) -> None:
        sidebar = self.query_one("#sidebar", Sidebar)
        sidebar.update_peer_state(peer_id, new)

    def _handle_peers_changed(self) -> None:
        self.call_from_thread(self._schedule_peer_refresh)

    def _schedule_peer_refresh(self) -> None:
        if not self.node:
            return
        self.run_worker(
            self.refresh_peers_from_node(),
            name="refresh-peers",
            group="peer-refresh",
            exclusive=True,
        )

    async def refresh_peers_from_node(self) -> None:
        """Load peers from storage into sidebar, preserving active selection when possible."""
        if not self.node:
            return
        peers = await self.node.get_known_peers()
        sidebar = self.query_one("#sidebar", Sidebar)
        rebuilt: list[dict] = []
        for p in peers:
            state = self.node.peer_states.get_state(p.peer_id)
            state_str = state.value if state is not None else ("archived" if p.archived else "disconnected")
            rebuilt.append(
                {
                    "peer_id": p.peer_id,
                    "name": p.nickname or p.peer_id.hex()[:8],
                    "state": state_str,
                    "archived": p.archived,
                }
            )
        sidebar.replace_peers(rebuilt)
        self._set_active_peer(sidebar.get_active_peer_id())

    async def on_sidebar_peer_selected(self, message: Sidebar.PeerSelected) -> None:
        self._set_active_peer(message.peer_id)

    def _set_active_peer(self, peer_id: bytes | None) -> None:
        self._active_peer_id = peer_id
        sidebar = self.query_one("#sidebar", Sidebar)
        name = sidebar.get_active_peer_name()
        chat = self.query_one("#chat-view", ChatView)
        chat.set_active_peer(peer_id, name)
        if peer_id:
            sidebar.clear_unread(peer_id)

    async def action_show_invite(self) -> None:
        from ashichat.ui.components import InviteDialog
        await self.push_screen(InviteDialog())

    async def action_show_profile(self) -> None:
        from ashichat.ui.components import ProfileDialog
        await self.push_screen(ProfileDialog())

    async def action_show_settings(self) -> None:
        from ashichat.ui.components import SettingsDialog
        await self.push_screen(SettingsDialog())

    async def action_manage_peer(self) -> None:
        sidebar = self.query_one("#sidebar", Sidebar)
        peer_id = sidebar.get_active_peer_id()
        if not peer_id:
            self.query_one("#chat-view", ChatView).add_system_message("No active contact to manage.")
            return
        state = self.node.peer_states.get_state(peer_id) if self.node else None
        archived = (state.value == "archived") if state is not None else False
        await self.push_screen(PeerManageDialog(peer_id, sidebar.get_active_peer_name() or peer_id.hex()[:8], archived))

    async def action_request_quit(self) -> None:
        undelivered = self.node.queue_manager.undelivered_count() if self.node else 0
        if undelivered <= 0:
            self.exit()
            return

        should_quit = await self.push_screen_wait(QuitConfirmDialog(undelivered))
        if should_quit:
            self.exit()
