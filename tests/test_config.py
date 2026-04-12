"""Tests for ashichat.config."""

from __future__ import annotations

from pathlib import Path

import pytest

from ashichat.config import (
    AshiChatConfig,
    DebugConfig,
    NetworkConfig,
    StorageConfig,
    _parse_config,
    ensure_directory_structure,
    load_config,
)


# ── Default loading ─────────────────────────────────────────────────────

class TestDefaultConfig:
    """Config loads correctly when no file exists."""

    def test_defaults_when_no_file(self, tmp_path: Path) -> None:
        base = tmp_path / ".ashichat"
        cfg = load_config(base_dir=base)

        assert cfg.network.udp_port == 9000
        assert cfg.network.max_peers == 500
        assert cfg.network.overlay_k == 50
        assert cfg.storage.message_log_limit_mb == 100
        assert cfg.storage.max_log_rotations == 3
        assert cfg.debug.log_level == "INFO"
        assert cfg.base_dir == base

    def test_directory_structure_created(self, tmp_path: Path) -> None:
        base = tmp_path / ".ashichat"
        load_config(base_dir=base)

        assert (base / "identity").is_dir()
        assert (base / "messages").is_dir()
        assert (base / "data").is_dir()
        assert (base / "config.toml").is_file()


# ── TOML parsing ────────────────────────────────────────────────────────

class TestTOMLParsing:
    """Config file values are parsed correctly."""

    def test_custom_values(self, tmp_path: Path) -> None:
        base = tmp_path / ".ashichat"
        ensure_directory_structure(base)
        (base / "config.toml").write_text(
            '[network]\nudp_port = 12345\nmax_peers = 200\noverlay_k = 30\n'
            '[storage]\nmessage_log_limit_mb = 50\nmax_log_rotations = 2\n'
            '[debug]\nlog_level = "DEBUG"\n',
            encoding="utf-8",
        )

        cfg = load_config(base_dir=base)
        assert cfg.network.udp_port == 12345
        assert cfg.network.max_peers == 200
        assert cfg.network.overlay_k == 30
        assert cfg.storage.message_log_limit_mb == 50
        assert cfg.storage.max_log_rotations == 2
        assert cfg.debug.log_level == "DEBUG"

    def test_missing_sections_fall_back(self, tmp_path: Path) -> None:
        base = tmp_path / ".ashichat"
        ensure_directory_structure(base)
        # Write an empty TOML — all sections missing
        (base / "config.toml").write_text("# empty\n", encoding="utf-8")

        cfg = load_config(base_dir=base)
        assert cfg.network.udp_port == 9000
        assert cfg.debug.log_level == "INFO"

    def test_partial_section(self, tmp_path: Path) -> None:
        base = tmp_path / ".ashichat"
        ensure_directory_structure(base)
        (base / "config.toml").write_text(
            '[network]\nudp_port = 8080\n',
            encoding="utf-8",
        )
        cfg = load_config(base_dir=base)
        assert cfg.network.udp_port == 8080
        assert cfg.network.max_peers == 500  # default preserved


# ── Validation ──────────────────────────────────────────────────────────

class TestValidation:
    """Invalid values raise ValueError."""

    def test_invalid_port_zero(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="udp_port"):
            _parse_config({"network": {"udp_port": 0}}, tmp_path)

    def test_invalid_port_too_high(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="udp_port"):
            _parse_config({"network": {"udp_port": 70_000}}, tmp_path)

    def test_negative_max_peers(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="max_peers"):
            _parse_config({"network": {"max_peers": -1}}, tmp_path)

    def test_invalid_log_level(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="log_level"):
            _parse_config({"debug": {"log_level": "VERBOSE"}}, tmp_path)


# ── Immutability ────────────────────────────────────────────────────────

class TestImmutability:
    """Frozen dataclasses prevent mutation."""

    def test_frozen_network(self) -> None:
        cfg = NetworkConfig()
        with pytest.raises(AttributeError):
            cfg.udp_port = 1234  # type: ignore[misc]

    def test_frozen_top_level(self, default_config: AshiChatConfig) -> None:
        with pytest.raises(AttributeError):
            default_config.debug = DebugConfig(log_level="ERROR")  # type: ignore[misc]
