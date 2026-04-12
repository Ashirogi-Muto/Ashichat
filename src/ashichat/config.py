"""Configuration loader for AshiChat.

Reads ~/.ashichat/config.toml (TOML format, parsed via tomllib).
Creates directory structure on first run. Config is loaded once at startup —
no dynamic reload in v1.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Config dataclasses (frozen — immutable after creation)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NetworkConfig:
    """Network-related settings."""
    udp_port: int = 9000
    max_peers: int = 500
    overlay_k: int = 50


@dataclass(frozen=True)
class StorageConfig:
    """Storage-related settings."""
    message_log_limit_mb: int = 100
    max_log_rotations: int = 3


@dataclass(frozen=True)
class DebugConfig:
    """Debug / logging settings."""
    log_level: str = "INFO"


@dataclass(frozen=True)
class ProfileConfig:
    """Local profile settings."""
    nickname: str = ""


@dataclass(frozen=True)
class AshiChatConfig:
    """Top-level application configuration."""
    network: NetworkConfig = field(default_factory=NetworkConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    debug: DebugConfig = field(default_factory=DebugConfig)
    profile: ProfileConfig = field(default_factory=ProfileConfig)
    base_dir: Path = field(default_factory=lambda: Path.home() / ".ashichat")


# ---------------------------------------------------------------------------
# Default TOML template (written on first run)
# ---------------------------------------------------------------------------

_DEFAULT_TOML = """\
# AshiChat configuration — https://github.com/AshiChat
# Edit values below. Restart AshiChat for changes to take effect.

[network]
udp_port = 9000
max_peers = 500
overlay_k = 50

[storage]
message_log_limit_mb = 100
max_log_rotations = 3

[debug]
log_level = "INFO"

[profile]
nickname = ""
"""


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _validate_port(port: int) -> None:
    if not (1 <= port <= 65535):
        raise ValueError(f"udp_port must be 1–65535, got {port}")


def _validate_positive(name: str, value: int) -> None:
    if value < 1:
        raise ValueError(f"{name} must be >= 1, got {value}")


def _validate_log_level(level: str) -> None:
    valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    if level.upper() not in valid:
        raise ValueError(f"log_level must be one of {valid}, got {level!r}")


def _validate_nickname(nickname: str) -> None:
    if len(nickname) > 32:
        raise ValueError("nickname must be <= 32 chars")
    if any(ord(ch) < 32 for ch in nickname):
        raise ValueError("nickname cannot contain control characters")


# ---------------------------------------------------------------------------
# Directory bootstrapping
# ---------------------------------------------------------------------------

def ensure_directory_structure(base_dir: Path) -> None:
    """Create the ~/.ashichat/ directory tree if it doesn't exist."""
    base_dir.mkdir(parents=True, exist_ok=True)
    (base_dir / "identity").mkdir(exist_ok=True)
    (base_dir / "messages").mkdir(exist_ok=True)
    (base_dir / "data").mkdir(exist_ok=True)

    config_path = base_dir / "config.toml"
    if not config_path.exists():
        config_path.write_text(_DEFAULT_TOML, encoding="utf-8")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _parse_network(raw: dict[str, Any]) -> NetworkConfig:
    port = raw.get("udp_port", 9000)
    max_peers = raw.get("max_peers", 500)
    overlay_k = raw.get("overlay_k", 50)

    _validate_port(port)
    _validate_positive("max_peers", max_peers)
    _validate_positive("overlay_k", overlay_k)

    return NetworkConfig(udp_port=port, max_peers=max_peers, overlay_k=overlay_k)


def _parse_storage(raw: dict[str, Any]) -> StorageConfig:
    limit = raw.get("message_log_limit_mb", 100)
    rotations = raw.get("max_log_rotations", 3)

    _validate_positive("message_log_limit_mb", limit)
    _validate_positive("max_log_rotations", rotations)

    return StorageConfig(message_log_limit_mb=limit, max_log_rotations=rotations)


def _parse_debug(raw: dict[str, Any]) -> DebugConfig:
    level = raw.get("log_level", "INFO")
    _validate_log_level(level)
    return DebugConfig(log_level=level.upper())


def _parse_profile(raw: dict[str, Any]) -> ProfileConfig:
    nickname = str(raw.get("nickname", "")).strip()
    _validate_nickname(nickname)
    return ProfileConfig(nickname=nickname)


def _parse_config(raw: dict[str, Any], base_dir: Path) -> AshiChatConfig:
    """Build a validated AshiChatConfig from parsed TOML dict."""
    return AshiChatConfig(
        network=_parse_network(raw.get("network", {})),
        storage=_parse_storage(raw.get("storage", {})),
        debug=_parse_debug(raw.get("debug", {})),
        profile=_parse_profile(raw.get("profile", {})),
        base_dir=base_dir,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_config(base_dir: Path | None = None) -> AshiChatConfig:
    """Load configuration from ``base_dir/config.toml``.

    If *base_dir* is ``None``, defaults to ``~/.ashichat/``.
    Creates directory structure and default config on first run.
    """
    if base_dir is None:
        base_dir = Path.home() / ".ashichat"

    ensure_directory_structure(base_dir)

    config_path = base_dir / "config.toml"
    raw: dict[str, Any] = {}
    if config_path.exists():
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)

    return _parse_config(raw, base_dir)


def _to_toml(config: AshiChatConfig) -> str:
    """Render a deterministic TOML representation of config."""
    nickname = config.profile.nickname.replace("\\", "\\\\").replace('"', '\\"')
    return (
        "# AshiChat configuration — https://github.com/AshiChat\n"
        "# Edit values below. Restart AshiChat for changes to take effect.\n\n"
        "[network]\n"
        f"udp_port = {config.network.udp_port}\n"
        f"max_peers = {config.network.max_peers}\n"
        f"overlay_k = {config.network.overlay_k}\n\n"
        "[storage]\n"
        f"message_log_limit_mb = {config.storage.message_log_limit_mb}\n"
        f"max_log_rotations = {config.storage.max_log_rotations}\n\n"
        "[debug]\n"
        f'log_level = "{config.debug.log_level}"\n\n'
        "[profile]\n"
        f'nickname = "{nickname}"\n'
    )


def save_config(config: AshiChatConfig) -> None:
    """Persist validated config to ``config.base_dir/config.toml``."""
    ensure_directory_structure(config.base_dir)
    (config.base_dir / "config.toml").write_text(_to_toml(config), encoding="utf-8")


def update_config(
    base_dir: Path,
    *,
    network: NetworkConfig | None = None,
    storage: StorageConfig | None = None,
    debug: DebugConfig | None = None,
    profile: ProfileConfig | None = None,
) -> AshiChatConfig:
    """Update and persist selected config sections; returns new config."""
    current = load_config(base_dir=base_dir)
    new_config = AshiChatConfig(
        network=network or current.network,
        storage=storage or current.storage,
        debug=debug or current.debug,
        profile=profile or current.profile,
        base_dir=base_dir,
    )
    save_config(new_config)
    return new_config
