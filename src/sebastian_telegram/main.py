"""Entrypoint: wire config, adapters and the bridge, then long-poll."""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Mapping

from .bridge import Bridge
from .clock import Clock, SystemClock
from .config import Config, ConfigError, load_config
from .log import configure_logging
from .pi import Pi, SubprocessPi
from .state import StateStore
from .telegram import BotApiClient, Telegram


def build_service(
    config: Config,
    logger: logging.Logger,
    *,
    telegram: Telegram | None = None,
    pi: Pi | None = None,
    clock: Clock | None = None,
) -> Bridge:
    """The one composition path: real adapters by default, fakes in tests.

    `serve()` and the tests both build the bridge here, so the tests exercise
    the same wiring the systemd unit starts.
    """
    return Bridge(
        config=config,
        telegram=(
            telegram
            if telegram is not None
            else BotApiClient(config.token, config.api_base_url, logger=logger)
        ),
        pi=(
            pi
            if pi is not None
            else SubprocessPi(config.pi_bin, config.vault_dir, secret=config.token)
        ),
        clock=clock if clock is not None else SystemClock(),
        state=StateStore(config.state_path, logger=logger),
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
