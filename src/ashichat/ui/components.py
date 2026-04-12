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


class ProfileDialog(ModalScreen):
    """CLI-style profile screen."""

    DEFAULT_CSS = """
    ProfileDialog {
        align: center middle;
    }
    #profile-container {
        width: 80;
        height: 20;
        background: #000000;
        border: none;
        padding: 1 2;
    }
    #profile-output {
        height: 1fr;
        background: #000000;
        color: #ffffff;
    }
    #profile-input {
        dock: bottom;
        background: #000000;
        color: #ffffff;
        border: none;
    }
    #profile-input:focus {
        border: none;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="profile-container"):
            yield Static("", id="profile-output")
            yield Input(placeholder="> ", id="profile-input")

    def on_mount(self) -> None:
        self._state = "MENU"
        self._refresh_menu()
        self.query_one("#profile-input", Input).focus()

    def _get_base_dir(self):
        app = self.app
        if hasattr(app, "node") and app.node and app.node.config:
            return app.node.config.base_dir
        from pathlib import Path
        return Path.home() / ".ashichat"

    def _refresh_menu(self) -> None:
        from ashichat.config import load_config

        cfg = load_config(base_dir=self._get_base_dir())
        app = self.app
        fp = "N/A"
        peer = "N/A"
        pub = "N/A"
        if hasattr(app, "node") and app.node and app.node.identity:
            ident = app.node.identity
            fp = ident.fingerprint()
            peer = ident.peer_id.hex()
            pub = ident.public_key_bytes.hex()

        nickname = cfg.profile.nickname or "(unset)"
        out = self.query_one("#profile-output", Static)
        out.update(
            "--- Profile ---\n\n"
            f"Nickname: {nickname}\n"
            f"Fingerprint: {fp}\n"
            f"Peer ID: {peer}\n"
            f"Public Key: {pub}\n\n"
            "1. Set nickname\n"
            "2. Clear nickname\n"
            "3. Close\n\n"
            "Type a number and press Enter."
        )

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        cmd = event.value.strip()
        event.input.value = ""
        out = self.query_one("#profile-output", Static)

        if self._state == "MENU":
            if cmd == "1":
                out.update("Enter nickname (max 32 chars):")
                self._state = "SET_NICK"
            elif cmd == "2":
                from ashichat.config import ProfileConfig, update_config

                try:
                    update_config(self._get_base_dir(), profile=ProfileConfig(nickname=""))
                    self._refresh_menu()
                except Exception as e:
                    out.update(f"[ERR] Could not clear nickname: {e}\n\nPress Enter to continue.")
                    self._state = "WAIT"
            elif cmd == "3":
                self.dismiss()
            else:
                out.update(
                    "Invalid option.\n\n"
                    "Press Enter to return to menu."
                )
                self._state = "WAIT"
        elif self._state == "SET_NICK":
            from ashichat.config import ProfileConfig, update_config

            try:
                update_config(self._get_base_dir(), profile=ProfileConfig(nickname=cmd))
                self._state = "MENU"
                self._refresh_menu()
            except Exception as e:
                out.update(f"[ERR] Invalid nickname: {e}\n\nPress Enter to continue.")
                self._state = "WAIT"
        elif self._state == "WAIT":
            self._state = "MENU"
            self._refresh_menu()


class SettingsDialog(ModalScreen):
    """CLI-style settings screen."""

    DEFAULT_CSS = """
    SettingsDialog {
        align: center middle;
    }
    #settings-container {
        width: 80;
        height: 22;
        background: #000000;
        border: none;
        padding: 1 2;
    }
    #settings-output {
        height: 1fr;
        background: #000000;
        color: #ffffff;
    }
    #settings-input {
        dock: bottom;
        background: #000000;
        color: #ffffff;
        border: none;
    }
    #settings-input:focus {
        border: none;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="settings-container"):
            yield Static("", id="settings-output")
            yield Input(placeholder="> ", id="settings-input")

    def on_mount(self) -> None:
        from ashichat.config import load_config

        self._state = "MENU"
        self._edit_key: str | None = None
        self._base_dir = self._get_base_dir()
        cfg = load_config(base_dir=self._base_dir)
        self._draft = {
            "udp_port": cfg.network.udp_port,
            "max_peers": cfg.network.max_peers,
            "overlay_k": cfg.network.overlay_k,
            "message_log_limit_mb": cfg.storage.message_log_limit_mb,
            "max_log_rotations": cfg.storage.max_log_rotations,
            "log_level": cfg.debug.log_level,
        }
        self._refresh_menu()
        self.query_one("#settings-input", Input).focus()

    def _get_base_dir(self):
        app = self.app
        if hasattr(app, "node") and app.node and app.node.config:
            return app.node.config.base_dir
        from pathlib import Path
        return Path.home() / ".ashichat"

    def _refresh_menu(self) -> None:
        out = self.query_one("#settings-output", Static)
        out.update(
            "--- Settings ---\n\n"
            f"1. UDP Port: {self._draft['udp_port']}\n"
            f"2. Max Peers: {self._draft['max_peers']}\n"
            f"3. Overlay K: {self._draft['overlay_k']}\n"
            f"4. Message Log Limit MB: {self._draft['message_log_limit_mb']}\n"
            f"5. Max Log Rotations: {self._draft['max_log_rotations']}\n"
            f"6. Log Level: {self._draft['log_level']}\n\n"
            "7. Save and close\n"
            "8. Close without saving\n\n"
            "Restart AshiChat after saving for changes to take effect."
        )

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        cmd = event.value.strip()
        event.input.value = ""
        out = self.query_one("#settings-output", Static)

        if self._state == "MENU":
            if cmd in {"1", "2", "3", "4", "5", "6"}:
                field_map = {
                    "1": ("udp_port", "Enter UDP port (1-65535):"),
                    "2": ("max_peers", "Enter max peers (>=1):"),
                    "3": ("overlay_k", "Enter overlay K (>=1):"),
                    "4": ("message_log_limit_mb", "Enter log limit MB (>=1):"),
                    "5": ("max_log_rotations", "Enter max rotations (>=1):"),
                    "6": ("log_level", "Enter log level: DEBUG/INFO/WARNING/ERROR/CRITICAL"),
                }
                self._edit_key, prompt = field_map[cmd]
                out.update(prompt)
                self._state = "EDIT"
            elif cmd == "7":
                from ashichat.config import (
                    DebugConfig,
                    NetworkConfig,
                    StorageConfig,
                    load_config,
                    update_config,
                )

                try:
                    current = load_config(base_dir=self._base_dir)
                    new_cfg = update_config(
                        self._base_dir,
                        network=NetworkConfig(
                            udp_port=int(self._draft["udp_port"]),
                            max_peers=int(self._draft["max_peers"]),
                            overlay_k=int(self._draft["overlay_k"]),
                        ),
                        storage=StorageConfig(
                            message_log_limit_mb=int(self._draft["message_log_limit_mb"]),
                            max_log_rotations=int(self._draft["max_log_rotations"]),
                        ),
                        debug=DebugConfig(log_level=str(self._draft["log_level"]).upper()),
                        profile=current.profile,
                    )
                    if hasattr(self.app, "node") and self.app.node:
                        self.app.node.config = new_cfg
                    self.dismiss()
                except Exception as e:
                    out.update(f"[ERR] Could not save settings: {e}\n\nPress Enter to continue.")
                    self._state = "WAIT"
            elif cmd == "8":
                self.dismiss()
            else:
                out.update(
                    "Invalid option.\n\n"
                    "Press Enter to return to menu."
                )
                self._state = "WAIT"
        elif self._state == "EDIT":
            assert self._edit_key is not None
            try:
                key = self._edit_key
                if key == "log_level":
                    self._draft[key] = cmd.upper()
                else:
                    self._draft[key] = int(cmd)
                self._state = "MENU"
                self._edit_key = None
                self._refresh_menu()
            except ValueError:
                out.update("[ERR] Invalid numeric value.\n\nPress Enter to continue.")
                self._state = "WAIT"
        elif self._state == "WAIT":
            self._state = "MENU"
            self._refresh_menu()
