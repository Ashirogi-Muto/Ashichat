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
class AshiChatConfig:
    """Top-level application configuration."""
    network: NetworkConfig = field(default_factory=NetworkConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    debug: DebugConfig = field(default_factory=DebugConfig)
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


def _parse_config(raw: dict[str, Any], base_dir: Path) -> AshiChatConfig:
    """Build a validated AshiChatConfig from parsed TOML dict."""
    return AshiChatConfig(
        network=_parse_network(raw.get("network", {})),
        storage=_parse_storage(raw.get("storage", {})),
        debug=_parse_debug(raw.get("debug", {})),
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
