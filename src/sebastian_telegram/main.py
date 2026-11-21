"""Entrypoint: wire config, adapters and the bridge, then long-poll."""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Mapping

from .bridge import Bridge, build_bridge
from .clock import SystemClock
from .config import Config, ConfigError, load_config
from .log import configure_logging
from .pi import SubprocessPi
from .telegram import BotApiClient


def build_service(config: Config, logger: logging.Logger) -> Bridge:
    """Real adapters, same composition path as the tests."""
    return build_bridge(
        config,
        telegram=BotApiClient(config.token, config.api_base_url, logger=logger),
        pi=SubprocessPi(config.pi_bin, config.vault_dir, secret=config.token),
        clock=SystemClock(),
        logger=logger,
    )


async def serve(env: Mapping[str, str] | None = None) -> None:
    config = load_config(env)
    logger = configure_logging(config.token)
    bridge = build_service(config, logger)
    await bridge.run()


def cli() -> None:
    try:
        asyncio.run(serve())
    except ConfigError as exc:
        print(f"sebastian-telegram: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    cli()
