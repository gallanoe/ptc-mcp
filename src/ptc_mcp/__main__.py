"""Entry point for running the PTC-MCP server."""

import asyncio
import logging
import sys

from .server import run_server


def main() -> None:
    """Configure logging to stderr and start the server."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    asyncio.run(run_server())


if __name__ == "__main__":
    main()
