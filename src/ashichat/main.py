"""AshiChat — main entry point.

Usage:
    python main.py          # direct invocation
    ashichat                # via console script (after pip install)
"""

from __future__ import annotations

import asyncio

from ashichat.config import load_config
from ashichat.logging_setup import get_logger, setup_logging
from ashichat.node import Node
from ashichat.ui.tui import AshiChatApp

log = get_logger(__name__)


async def main() -> None:
    config = load_config()
    setup_logging(config)
    log.info("AshiChat v0.1.0 starting — base_dir=%s", config.base_dir)

    node = Node(config)
    await node.start()

    app = AshiChatApp(node)
    try:
        await app.run_async()
    finally:
        await node.stop()


def run() -> None:
    """Console-script entry point (``ashichat`` command)."""
    asyncio.run(main())


if __name__ == "__main__":
    run()
