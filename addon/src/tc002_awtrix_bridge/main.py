from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from pathlib import Path

from .config import load_config
from .service import BridgeService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AWTRIX NG bridge for Ulanzi TC002")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("/data/config.yaml"),
        help="YAML configuration path (default: /data/config.yaml)",
    )
    parser.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    return parser


async def _run(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    service = BridgeService(config)
    await service.start()
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stopped.set)
        except NotImplementedError:
            pass
    try:
        await stopped.wait()
    finally:
        await service.stop()


def main() -> None:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
