"""Reading the shipped systemd unit and its environment file.

Test support only: these are plain parsers for the deployment artifacts, so the
tests can assert the supervision contract the artifacts declare.
"""

from __future__ import annotations

import configparser
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "systemd"
UNIT_PATH = DEPLOY / "sebastian-telegram.service"
ENV_PATH = DEPLOY / "sebastian-telegram.env"

# The unit, the console script and the installed environment file are one
# contract; the tests assert these literal paths.
ENTRYPOINT = "/home/pi/.local/bin/sebastian-telegram"
INSTALLED_ENV = "/etc/sebastian-telegram/env"
IDENTIFIER = "sebastian-telegram"

_ENV_KEY = re.compile(r"[A-Z_][A-Z0-9_]*")


def read_unit(path: Path = UNIT_PATH) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(
        interpolation=None, strict=False, allow_no_value=True
    )
    parser.optionxform = str  # systemd directive names are case-sensitive
    with path.open() as handle:
        parser.read_file(handle)
    return parser


def read_env(path: Path = ENV_PATH) -> dict[str, str]:
    """Parse the file as systemd's EnvironmentFile does: KEY=VALUE lines."""
    values: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        assert separator, f"not a KEY=VALUE line: {raw!r}"
        assert _ENV_KEY.fullmatch(key), f"not a valid variable name: {key!r}"
        values[key] = value
    return values
