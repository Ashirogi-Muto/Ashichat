"""TUI widgets for AshiChat."""

from __future__ import annotations

import time
from typing import Any

from textual.app import ComposeResult
from textual.containers import ScrollableContainer, Vertical, Horizontal
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Input,
    Label,
    ListItem,
    ListView,
    OptionList,
    Static,
    TextArea,
)
from textual.widget import Widget

from ashichat.logging_setup import get_logger

log = get_logger(__name__)


# Status indicators — ASCII only for Windows terminal compatibility
_STATUS_ICONS = {
    "connected": "[+] ",
    "connecting": "[~] ",
    "disconnected": "[-] ",
    "idle": "[-] ",
    "suspect": "[?] ",
    "resolving": "[~] ",
    "failed": "[x] ",
    "archived": "[.] ",
}


class Sidebar(Widget):
    """Contact list with status indicators."""

    DEFAULT_CSS = """
    Sidebar {
        width: 28;
        dock: left;
        background: #000000;
        border: none;
        padding: 1;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._peers: dict[bytes, dict] = {}

    def compose(self) -> ComposeResult:
        yield Static("  Contacts", classes="sidebar-title")
        yield ListView(id="contact-list")

    def add_peer(self, peer_id: bytes, name: str, state: str = "disconnected") -> None:
        self._peers[peer_id] = {"name": name, "state": state, "unread": 0}
        self._refresh_list()

    def update_peer_state(self, peer_id: bytes, state: Any) -> None:
        state_str = state.value if hasattr(state, "value") else str(state)
        if peer_id not in self._peers:
            self.add_peer(peer_id, peer_id.hex()[:8], state_str)
        else:
            self._peers[peer_id]["state"] = state_str
            self._refresh_list()

    def _refresh_list(self) -> None:
        try:
            lv = self.query_one("#contact-list", ListView)
            lv.clear()
            for pid, info in self._peers.items():
                icon = _STATUS_ICONS.get(info["state"], "? ")
                name = info["name"]
                unread = f" ({info['unread']})" if info.get("unread", 0) > 0 else ""
                lv.append(ListItem(Label(f"{icon}{name}{unread}")))
        except Exception:
            pass


class ChatView(Widget):
    """Scrollable message history."""

    DEFAULT_CSS = """
    ChatView {
        width: 1fr;
        height: 1fr;
        background: #000000;
        border: none;
        padding: 1;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._messages: list[dict] = []

    def compose(self) -> ComposeResult:
        yield ScrollableContainer(id="message-container")

    def add_message(
        self, peer_id: bytes, text: str, incoming: bool = True
    ) -> None:
        ts = time.strftime("%H:%M")
        sender = peer_id.hex()[:8] if incoming else "You"
        self._messages.append({"ts": ts, "sender": sender, "text": text})

        try:
            container = self.query_one("#message-container", ScrollableContainer)
            container.mount(Static(f"[{ts}] {sender}: {text}"))
            container.scroll_end(animate=False)
        except Exception:
            pass


class MessageInput(Widget):
    """Text input field at the bottom."""

    DEFAULT_CSS = """
    MessageInput {
        dock: bottom;
        height: 3;
        padding: 0 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Input(placeholder="Type a message...", id="msg-input")

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        event.input.value = ""

        # Send through node if available
        app = self.app
        if hasattr(app, "node") and app.node:
            from ashichat.peer_state import PeerState
            peers = app.node.peer_states.all_peers()
            for pid, state in peers.items():
                if state == PeerState.CONNECTED:
                    app.node.send_message(pid, text)

        # For now just display locally
        try:
            chat = app.query_one("#chat-view", ChatView)
            chat.add_message(b"\x00" * 32, text, incoming=False)
        except Exception:
            pass


class InviteDialog(ModalScreen):
    """Pure CLI-style screen for Invites."""

    DEFAULT_CSS = """
    InviteDialog {
        align: center middle;
    }
    #invite-container {
        width: 60;
        height: 20;
        background: #000000;
        border: none;
        padding: 1 2;
    }
    #cli-output {
        height: 1fr;
        background: #000000;
        color: #ffffff;
    }
    #cli-input {
        dock: bottom;
        background: #000000;
        color: #ffffff;
        border: none;
    }
    #cli-input:focus {
        border: none;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="invite-container"):
            yield Static(self._get_menu_text(), id="cli-output")
            yield Input(placeholder="> ", id="cli-input")

    def _get_menu_text(self) -> str:
        return (
            "--- Invite Management ---\n\n"
            "1. Generate new Invite\n"
            "2. Accept existing Invite\n"
            "3. Close\n\n"
            "Type a number (1-3) and press Enter."
        )

    def on_mount(self) -> None:
        self._state = "MENU"
        self.query_one("#cli-input").focus()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        cmd = event.value.strip()
        event.input.value = ""
        out = self.query_one("#cli-output", Static)

        if self._state == "MENU":
            if cmd == "1":
                app = self.app
                if hasattr(app, "node") and app.node and app.node.identity:
                    from ashichat.invite import generate_invite, generate_invite_readable
                    c85 = generate_invite(app.node.identity.public_key)
                    c32 = generate_invite_readable(app.node.identity.public_key)
                    out.update(f"--- Generated Invite ---\n\nBase85:\n{c85}\n\nBase32:\n{c32}\n\nPress Enter to return.")
                else:
                    out.update("Error: Node not running.\n\nPress Enter to return.")
                self._state = "WAIT"
            elif cmd == "2":
                out.update("--- Accept Invite ---\n\nPaste the invite code and press Enter:")
                self._state = "ACCEPT"
            elif cmd == "3":
                self.dismiss()
            else:
                out.update(self._get_menu_text() + f"\n\n[!] Invalid option: '{cmd}'")

        elif self._state == "WAIT":
            out.update(self._get_menu_text())
            self._state = "MENU"

        elif self._state == "ACCEPT":
            if not cmd:
                out.update(self._get_menu_text())
                self._state = "MENU"
                return
                
            try:
                from ashichat.invite import parse_invite
                data = parse_invite(cmd)
                out.update(f"[OK] Invite parsed!\nKey: {data.public_key.public_bytes_raw().hex()[:16]}...\n\nPress Enter to return.")
            except Exception as e:
                out.update(f"[ERR] Invalid Code: {e}\n\nPress Enter to return.")
            self._state = "WAIT"
