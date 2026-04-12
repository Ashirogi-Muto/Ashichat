"""TUI widgets for AshiChat."""

from __future__ import annotations

import socket
import time
from pathlib import Path
from typing import Any

from textual.app import ComposeResult
from textual.containers import ScrollableContainer, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Input, Label, ListItem, ListView, Static
from textual.widget import Widget

from ashichat.logging_setup import get_logger

log = get_logger(__name__)


def _guess_local_endpoint(app) -> tuple[str, int] | None:
    """Best-effort endpoint hint for invite generation."""
    if not (hasattr(app, "node") and app.node and app.node.config):
        return None
    port = app.node.config.network.udp_port
    ip: str | None = None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
    except OSError:
        pass

    if not ip or ip.startswith("127."):
        try:
            ip = socket.gethostbyname(socket.gethostname())
        except OSError:
            ip = None

    if not ip or ip.startswith("127."):
        return None
    return (ip, port)


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
    """Contact list with search, selection, and unread counters."""

    class PeerSelected(Message):
        def __init__(self, peer_id: bytes) -> None:
            self.peer_id = peer_id
            super().__init__()

    DEFAULT_CSS = """
    Sidebar {
        width: 28;
        dock: left;
        background: #000000;
        padding: 1;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._peers: dict[bytes, dict[str, Any]] = {}
        self._active_peer_id: bytes | None = None
        self._filter: str = ""
        self._visible_ids: list[bytes] = []

    def compose(self) -> ComposeResult:
        yield Static("  Contacts", classes="sidebar-title")
        yield Input(placeholder="Search...", id="contact-search")
        yield ListView(id="contact-list")

    def add_peer(
        self,
        peer_id: bytes,
        name: str,
        state: str = "disconnected",
        archived: bool = False,
    ) -> None:
        self._peers[peer_id] = {
            "name": name,
            "state": state,
            "unread": 0,
            "archived": archived,
        }
        if self._active_peer_id is None:
            self._active_peer_id = peer_id
        self._refresh_list()

    def update_peer_state(self, peer_id: bytes, state: Any) -> None:
        state_str = state.value if hasattr(state, "value") else str(state)
        if peer_id not in self._peers:
            self.add_peer(peer_id, peer_id.hex()[:8], state_str, archived=state_str == "archived")
            return
        self._peers[peer_id]["state"] = state_str
        self._peers[peer_id]["archived"] = state_str == "archived"
        self._refresh_list()

    def update_peer_name(self, peer_id: bytes, name: str) -> None:
        if peer_id in self._peers:
            self._peers[peer_id]["name"] = name
            self._refresh_list()

    def remove_peer(self, peer_id: bytes) -> None:
        self._peers.pop(peer_id, None)
        if self._active_peer_id == peer_id:
            self._active_peer_id = next(iter(self._peers), None)
        self._refresh_list()

    def replace_peers(self, peers: list[dict[str, Any]]) -> None:
        """Replace sidebar data from a storage snapshot."""
        current_active = self._active_peer_id
        self._peers.clear()
        for p in peers:
            peer_id = p["peer_id"]
            self._peers[peer_id] = {
                "name": p["name"],
                "state": p["state"],
                "unread": int(p.get("unread", 0)),
                "archived": bool(p.get("archived", False)),
            }
        if current_active in self._peers:
            self._active_peer_id = current_active
        elif self._peers:
            self._active_peer_id = next(iter(self._peers))
        else:
            self._active_peer_id = None
        self._refresh_list()

    def increment_unread(self, peer_id: bytes) -> None:
        if peer_id in self._peers:
            self._peers[peer_id]["unread"] += 1
            self._refresh_list()

    def clear_unread(self, peer_id: bytes) -> None:
        if peer_id in self._peers:
            self._peers[peer_id]["unread"] = 0
            self._refresh_list()

    def get_active_peer_id(self) -> bytes | None:
        return self._active_peer_id

    def get_active_peer_name(self) -> str | None:
        if self._active_peer_id and self._active_peer_id in self._peers:
            return str(self._peers[self._active_peer_id]["name"])
        return None

    def set_active_peer(self, peer_id: bytes | None) -> None:
        self._active_peer_id = peer_id
        if peer_id:
            self.clear_unread(peer_id)
        self._refresh_list()

    async def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "contact-search":
            return
        self._filter = event.value.strip().lower()
        self._refresh_list()

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        item_id = event.item.id or ""
        if not item_id.startswith("peer-"):
            return
        try:
            peer_id = bytes.fromhex(item_id[len("peer-") :])
        except ValueError:
            return
        self.set_active_peer(peer_id)
        self.post_message(self.PeerSelected(peer_id))

    def _sorted_peer_ids(self) -> list[bytes]:
        items = list(self._peers.items())
        items.sort(
            key=lambda kv: (
                0 if kv[0] == self._active_peer_id else 1,
                -int(kv[1].get("unread", 0)),
                str(kv[1].get("name", "")).lower(),
            )
        )
        return [pid for pid, _ in items]

    def _refresh_list(self) -> None:
        try:
            lv = self.query_one("#contact-list", ListView)
            lv.clear()
            self._visible_ids = []
            for pid in self._sorted_peer_ids():
                info = self._peers[pid]
                name = str(info["name"])
                if self._filter and self._filter not in name.lower():
                    continue

                icon = _STATUS_ICONS.get(str(info["state"]), "? ")
                unread = f" ({info['unread']})" if int(info.get("unread", 0)) > 0 else ""
                active = " >" if pid == self._active_peer_id else "  "
                archived = " [A]" if info.get("archived") else ""
                label = f"{active}{icon}{name}{unread}{archived}"
                lv.append(ListItem(Label(label), id=f"peer-{pid.hex()}"))
                self._visible_ids.append(pid)

            if self._active_peer_id in self._visible_ids:
                lv.index = self._visible_ids.index(self._active_peer_id)
        except Exception:
            pass


class ChatView(Widget):
    """Scrollable message history with per-peer threads."""

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
        self._messages_by_peer: dict[bytes, list[dict[str, str]]] = {}
        self._system_messages: list[dict[str, str]] = []
        self._active_peer_id: bytes | None = None
        self._active_peer_name: str = "No contact selected"

    def compose(self) -> ComposeResult:
        yield ScrollableContainer(id="message-container")

    def set_active_peer(self, peer_id: bytes | None, peer_name: str | None = None) -> None:
        self._active_peer_id = peer_id
        self._active_peer_name = peer_name or (peer_id.hex()[:8] if peer_id else "No contact selected")
        self._render_current()

    def add_system_message(self, text: str) -> None:
        ts = time.strftime("%H:%M")
        self._system_messages.append({"ts": ts, "sender": "System", "text": text, "kind": "system"})
        self._render_current()

    def add_message(self, peer_id: bytes, text: str, incoming: bool = True) -> None:
        ts = time.strftime("%H:%M")
        sender = peer_id.hex()[:8] if incoming else "You"
        kind = "incoming" if incoming else "outgoing"
        self._messages_by_peer.setdefault(peer_id, []).append(
            {"ts": ts, "sender": sender, "text": text, "kind": kind}
        )
        if self._active_peer_id == peer_id:
            self._render_current()

    def _render_current(self) -> None:
        try:
            container = self.query_one("#message-container", ScrollableContainer)
            container.remove_children()
            header = Static(f"Conversation: {self._active_peer_name}", classes="msg-header")
            container.mount(header)

            for msg in self._system_messages[-5:]:
                container.mount(
                    Static(
                        f"[{msg['ts']}] {msg['sender']}: {msg['text']}",
                        classes="msg-system",
                    )
                )

            if self._active_peer_id is None:
                container.mount(Static("Select a contact from the sidebar to start chatting.", classes="msg-system"))
                container.scroll_end(animate=False)
                return

            messages = self._messages_by_peer.get(self._active_peer_id, [])
            for msg in messages[-300:]:
                cls = "msg-incoming" if msg["kind"] == "incoming" else "msg-outgoing"
                container.mount(Static(f"[{msg['ts']}] {msg['sender']}: {msg['text']}", classes=cls))

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

        app = self.app
        chat = app.query_one("#chat-view", ChatView)
        sidebar = app.query_one("#sidebar", Sidebar)
        active_peer = sidebar.get_active_peer_id()
        if active_peer is None:
            chat.add_system_message("No active contact selected.")
            return

        if hasattr(app, "node") and app.node:
            try:
                await app.node.send_message(active_peer, text)
            except Exception as e:
                chat.add_system_message(f"Send failed: {e}")
                return

        chat.add_message(active_peer, text, incoming=False)


class InviteDialog(ModalScreen):
    """Pure CLI-style screen for invites."""

    DEFAULT_CSS = """
    InviteDialog {
        align: center middle;
    }
    #invite-container {
        width: 70;
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
        self.query_one("#cli-input", Input).focus()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        cmd = event.value.strip()
        event.input.value = ""
        out = self.query_one("#cli-output", Static)

        if self._state == "MENU":
            if cmd == "1":
                app = self.app
                if hasattr(app, "node") and app.node and app.node.identity:
                    from ashichat.invite import generate_invite, generate_invite_readable

                    endpoint_hint = _guess_local_endpoint(app)
                    c85 = generate_invite(app.node.identity.public_key, endpoint=endpoint_hint)
                    c32 = generate_invite_readable(app.node.identity.public_key, endpoint=endpoint_hint)
                    endpoint_note = (
                        f"\nEndpoint hint: {endpoint_hint[0]}:{endpoint_hint[1]}"
                        if endpoint_hint
                        else "\nEndpoint hint: unavailable"
                    )
                    out.update(
                        f"--- Generated Invite ---\n\nBase85:\n{c85}\n\nBase32:\n{c32}\n{endpoint_note}\n\nPress Enter to return."
                    )
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
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
            from ashichat.identity import derive_peer_id
            from ashichat.invite import parse_invite

            # 1) Parse and validate invite format.
            try:
                data = parse_invite(cmd)
            except Exception as e:
                out.update(f"[ERR] Invalid invite format: {e}\n\nPress Enter to return.")
                self._state = "WAIT"
                return

            # 2) Add contact to local state/storage.
            try:
                app = self.app
                if not (hasattr(app, "node") and app.node and app.node.identity):
                    out.update("[ERR] Node not running.\n\nPress Enter to return.")
                    self._state = "WAIT"
                    return

                pub_raw = data.public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
                pub_key = Ed25519PublicKey.from_public_bytes(pub_raw)
                peer_id = derive_peer_id(pub_key)

                if peer_id == app.node.identity.peer_id:
                    out.update("[ERR] Cannot add your own invite.\n\nPress Enter to return.")
                    self._state = "WAIT"
                    return

                nickname = peer_id.hex()[:8]
                await app.node.storage.add_peer(peer_id, pub_raw, nickname)
                saved = await app.node.storage.get_peer(peer_id)
                if saved is None:
                    raise RuntimeError("peer insert verification failed")

                if app.node.peer_table:
                    app.node.peer_table.add_direct_peer(
                        peer_id,
                        pub_raw,
                        nickname=nickname,
                        endpoint=data.endpoint,
                    )

                # Fallback UI update even if refresh path fails.
                try:
                    sidebar = app.query_one("#sidebar", Sidebar)
                    sidebar.add_peer(peer_id, nickname, state="disconnected", archived=False)
                    sidebar.set_active_peer(peer_id)
                    chat = app.query_one("#chat-view", ChatView)
                    chat.set_active_peer(peer_id, nickname)
                except Exception:
                    pass

                if data.endpoint is not None:
                    await app.node.connect_to_peer(peer_id, data.endpoint)

                if hasattr(app, "refresh_peers_from_node"):
                    await app.refresh_peers_from_node()

                immediate = (
                    "Connection attempt started."
                    if data.endpoint is not None
                    else "No endpoint hint in invite; waiting for manual/overlay discovery."
                )
                out.update(
                    f"[OK] Contact added!\nPeer: {peer_id.hex()[:8]}\n{immediate}\n\nPress Enter to return."
                )
            except Exception as e:
                out.update(f"[ERR] Invite parsed but contact add failed: {e}\n\nPress Enter to return.")
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

    def _get_base_dir(self) -> Path:
        app = self.app
        if hasattr(app, "node") and app.node and app.node.config:
            return app.node.config.base_dir
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
                out.update("Invalid option.\n\nPress Enter to return to menu.")
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
    """CLI-style settings screen with inline validation and change summary."""

    DEFAULT_CSS = """
    SettingsDialog {
        align: center middle;
    }
    #settings-container {
        width: 82;
        height: 24;
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

    _DEFAULTS = {
        "udp_port": 9000,
        "max_peers": 500,
        "overlay_k": 50,
        "message_log_limit_mb": 100,
        "max_log_rotations": 3,
        "log_level": "INFO",
    }

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
        self._original = dict(self._draft)
        self._refresh_menu()
        self.query_one("#settings-input", Input).focus()

    def _get_base_dir(self) -> Path:
        app = self.app
        if hasattr(app, "node") and app.node and app.node.config:
            return app.node.config.base_dir
        return Path.home() / ".ashichat"

    def _has_changes(self) -> bool:
        return self._draft != self._original

    def _validate_value(self, key: str, value: str) -> tuple[bool, str | int]:
        try:
            if key == "log_level":
                lv = value.upper()
                valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
                if lv not in valid:
                    return (False, "log_level must be DEBUG/INFO/WARNING/ERROR/CRITICAL")
                return (True, lv)

            iv = int(value)
            if key == "udp_port" and not (1 <= iv <= 65535):
                return (False, "udp_port must be 1..65535")
            if key in {"max_peers", "overlay_k", "message_log_limit_mb", "max_log_rotations"} and iv < 1:
                return (False, f"{key} must be >= 1")
            return (True, iv)
        except ValueError:
            return (False, "value must be numeric")

    def _refresh_menu(self) -> None:
        out = self.query_one("#settings-output", Static)
        restart_line = (
            "\n[!] Changes pending restart." if self._has_changes() else ""
        )
        out.update(
            "--- Settings ---\n\n"
            f"1. UDP Port: {self._draft['udp_port']} (default {self._DEFAULTS['udp_port']})\n"
            f"2. Max Peers: {self._draft['max_peers']} (default {self._DEFAULTS['max_peers']})\n"
            f"3. Overlay K: {self._draft['overlay_k']} (default {self._DEFAULTS['overlay_k']})\n"
            f"4. Message Log Limit MB: {self._draft['message_log_limit_mb']} (default {self._DEFAULTS['message_log_limit_mb']})\n"
            f"5. Max Log Rotations: {self._draft['max_log_rotations']} (default {self._DEFAULTS['max_log_rotations']})\n"
            f"6. Log Level: {self._draft['log_level']} (default {self._DEFAULTS['log_level']})\n\n"
            "7. Save and close\n"
            "8. Close without saving\n"
            f"{restart_line}\n"
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
                from ashichat.config import DebugConfig, NetworkConfig, StorageConfig, load_config, update_config

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
                out.update("Invalid option.\n\nPress Enter to return to menu.")
                self._state = "WAIT"

        elif self._state == "EDIT":
            assert self._edit_key is not None
            ok, parsed_or_err = self._validate_value(self._edit_key, cmd)
            if not ok:
                out.update(f"[ERR] {parsed_or_err}\n\nPress Enter to continue.")
                self._state = "WAIT"
                return
            self._draft[self._edit_key] = parsed_or_err
            self._state = "MENU"
            self._edit_key = None
            self._refresh_menu()

        elif self._state == "WAIT":
            self._state = "MENU"
            self._refresh_menu()


class PeerManageDialog(ModalScreen):
    """Manage selected peer (rename/archive/remove)."""

    DEFAULT_CSS = """
    PeerManageDialog {
        align: center middle;
    }
    #peer-manage-container {
        width: 72;
        height: 18;
        background: #000000;
        border: none;
        padding: 1 2;
    }
    #peer-manage-output {
        height: 1fr;
        background: #000000;
        color: #ffffff;
    }
    #peer-manage-input {
        dock: bottom;
        background: #000000;
        color: #ffffff;
        border: none;
    }
    """

    def __init__(self, peer_id: bytes, peer_name: str, archived: bool = False) -> None:
        super().__init__()
        self.peer_id = peer_id
        self.peer_name = peer_name
        self.archived = archived

    def compose(self) -> ComposeResult:
        with Vertical(id="peer-manage-container"):
            yield Static("", id="peer-manage-output")
            yield Input(placeholder="> ", id="peer-manage-input")

    def on_mount(self) -> None:
        self._state = "MENU"
        self._refresh_menu()
        self.query_one("#peer-manage-input", Input).focus()

    def _refresh_menu(self) -> None:
        action = "Unarchive" if self.archived else "Archive"
        self.query_one("#peer-manage-output", Static).update(
            "--- Manage Peer ---\n\n"
            f"Peer: {self.peer_name} ({self.peer_id.hex()[:8]})\n"
            f"State: {'archived' if self.archived else 'active'}\n\n"
            "1. Rename\n"
            f"2. {action}\n"
            "3. Remove contact\n"
            "4. Close\n"
        )

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        cmd = event.value.strip()
        event.input.value = ""
        out = self.query_one("#peer-manage-output", Static)

        if self._state == "MENU":
            if cmd == "1":
                out.update("Enter new nickname:")
                self._state = "RENAME"
            elif cmd == "2":
                if not (hasattr(self.app, "node") and self.app.node):
                    self.dismiss()
                    return
                await self.app.node.set_peer_archived(self.peer_id, not self.archived)
                self.archived = not self.archived
                if hasattr(self.app, "refresh_peers_from_node"):
                    await self.app.refresh_peers_from_node()
                self._refresh_menu()
            elif cmd == "3":
                out.update("Type DELETE to confirm removal:")
                self._state = "DELETE"
            elif cmd == "4":
                self.dismiss()
            else:
                out.update("Invalid option.\n\nPress Enter to return.")
                self._state = "WAIT"

        elif self._state == "RENAME":
            if not cmd:
                out.update("Nickname cannot be empty.\n\nPress Enter to return.")
                self._state = "WAIT"
                return
            if hasattr(self.app, "node") and self.app.node:
                await self.app.node.rename_peer(self.peer_id, cmd)
                if hasattr(self.app, "refresh_peers_from_node"):
                    await self.app.refresh_peers_from_node()
            self.dismiss()

        elif self._state == "DELETE":
            if cmd == "DELETE":
                if hasattr(self.app, "node") and self.app.node:
                    await self.app.node.remove_peer(self.peer_id)
                    if hasattr(self.app, "refresh_peers_from_node"):
                        await self.app.refresh_peers_from_node()
                self.dismiss()
            else:
                out.update("Removal cancelled.\n\nPress Enter to return.")
                self._state = "WAIT"

        elif self._state == "WAIT":
            self._state = "MENU"
            self._refresh_menu()


class QuitConfirmDialog(ModalScreen[bool]):
    """Confirmation modal when undelivered messages exist."""

    DEFAULT_CSS = """
    QuitConfirmDialog {
        align: center middle;
    }
    #quit-confirm {
        width: 64;
        height: 10;
        background: #000000;
        border: none;
        padding: 1 2;
    }
    """

    def __init__(self, undelivered_count: int) -> None:
        super().__init__()
        self.undelivered_count = undelivered_count

    def compose(self) -> ComposeResult:
        with Vertical(id="quit-confirm"):
            yield Static(
                "--- Quit Confirmation ---\n\n"
                f"You have {self.undelivered_count} undelivered message(s).\n"
                "1. Quit anyway\n"
                "2. Cancel"
            )
            yield Input(placeholder="> ", id="quit-confirm-input")

    def on_mount(self) -> None:
        self.query_one("#quit-confirm-input", Input).focus()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        cmd = event.value.strip()
        if cmd == "1":
            self.dismiss(True)
        else:
            self.dismiss(False)
