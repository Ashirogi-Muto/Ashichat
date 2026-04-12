"""Shared pytest fixtures for AshiChat tests."""

from __future__ import annotations

import pytest
from pathlib import Path

from ashichat.config import AshiChatConfig, NetworkConfig, StorageConfig, DebugConfig


@pytest.fixture
def tmp_ashichat_dir(tmp_path: Path) -> Path:
    """Create a temporary ~/.ashichat/-equivalent directory tree."""
    base = tmp_path / ".ashichat"
    base.mkdir()
    (base / "identity").mkdir()
    (base / "messages").mkdir()
    (base / "data").mkdir()
    return base


@pytest.fixture
def default_config(tmp_ashichat_dir: Path) -> AshiChatConfig:
    """Return an AshiChatConfig with defaults, pointed at a temp dir."""
    return AshiChatConfig(
        network=NetworkConfig(),
        storage=StorageConfig(),
        debug=DebugConfig(),
        base_dir=tmp_ashichat_dir,
    )
