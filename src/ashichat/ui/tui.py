"""AshiChat Textual TUI — main application."""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header

from ashichat.logging_setup import get_logger
from ashichat.ui.components import ChatView, MessageInput, Sidebar

log = get_logger(__name__)


class AshiChatApp(App):
    """Terminal UI for AshiChat."""

    CSS_PATH = "styles.tcss"
    TITLE = "AshiChat v0.1.0"

    BINDINGS = [
        Binding("q", "quit", "Quit", priority=True),
        Binding("tab", "focus_next", "Next", priority=True),
        Binding("escape", "unfocus", "Unfocus", priority=True),
        Binding("i", "show_invite", "Invite", priority=True),
        Binding("p", "show_profile", "Profile", priority=True),
        Binding("s", "show_settings", "Settings", priority=True),
    ]

    def __init__(self, node=None) -> None:
        super().__init__()
        self.node = node

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
        log.info("TUI mounted")

    def _handle_incoming(self, peer_id: bytes, plaintext: bytes) -> None:
        self.call_from_thread(self._do_handle_incoming, peer_id, plaintext)

    def _do_handle_incoming(self, peer_id: bytes, plaintext: bytes) -> None:
        chat = self.query_one("#chat-view", ChatView)
        chat.add_message(peer_id, plaintext.decode("utf-8", errors="replace"), incoming=True)

    def _handle_state_change(self, peer_id: bytes, old, new) -> None:
        self.call_from_thread(self._do_handle_state_change, peer_id, old, new)

    def _do_handle_state_change(self, peer_id: bytes, old, new) -> None:
        sidebar = self.query_one("#sidebar", Sidebar)
        sidebar.update_peer_state(peer_id, new)

    async def action_show_invite(self) -> None:
        from ashichat.ui.components import InviteDialog
        await self.push_screen(InviteDialog())

    async def action_show_profile(self) -> None:
        from ashichat.ui.components import ProfileDialog
        await self.push_screen(ProfileDialog())

    async def action_show_settings(self) -> None:
        from ashichat.ui.components import SettingsDialog
        await self.push_screen(SettingsDialog())
