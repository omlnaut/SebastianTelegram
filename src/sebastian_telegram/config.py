"""Configuration from the environment, validated before the first message."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .secret import Secret


class ConfigError(Exception):
    """Startup misconfiguration; the service refuses to run."""


@dataclass(frozen=True)
class Config:
    token: Secret
    allowlist: frozenset[int]
    state_path: Path
    vault_dir: Path
    pi_bin: str
    poll_timeout: int
    api_base_url: str


def _home(env: Mapping[str, str]) -> Path:
    return Path(env.get("HOME") or "~").expanduser()


def _default_config_dir(env: Mapping[str, str]) -> Path:
    xdg = env.get("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else _home(env) / ".config") / "sebastian-telegram"


def _default_state_dir(env: Mapping[str, str]) -> Path:
    xdg = env.get("XDG_STATE_HOME")
    return (Path(xdg) if xdg else _home(env) / ".local" / "state") / "sebastian-telegram"


def _read_token(path: Path) -> Secret:
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        raise ConfigError(f"token file not found: {path}") from None
    except OSError as exc:
        raise ConfigError(f"token file is unreadable: {path}: {exc.strerror}") from None

    if mode & 0o077:
        raise ConfigError(
            f"token file has group or other permission bits set (mode {mode & 0o777:o}): {path}; "
            "run: chmod 600 " + str(path)
        )

    try:
        token = path.read_text().strip()
    except OSError as exc:
        raise ConfigError(f"token file is unreadable: {path}: {exc.strerror}") from None

    if not token:
        raise ConfigError(f"token file is empty: {path}")

    return Secret(token)


def _parse_allowlist(raw: str | None) -> frozenset[int]:
    if raw is None or not raw.strip():
        raise ConfigError(
            "allowlist is empty: set SEBASTIAN_TELEGRAM_ALLOWLIST to a comma-separated list of chat IDs"
        )

    chat_ids: set[int] = set()
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            raise ConfigError(f"allowlist contains an empty entry: {raw!r}")
        try:
            chat_ids.add(int(entry))
        except ValueError:
            raise ConfigError(f"allowlist entry is not an integer chat ID: {entry!r}") from None

    if not chat_ids:
        raise ConfigError("allowlist is empty")
    return frozenset(chat_ids)


def _ensure_writable_state_dir(state_dir: Path) -> None:
    try:
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise ConfigError(f"state directory is not writable: {state_dir}: {exc.strerror}") from None

    try:
        fd, probe = tempfile.mkstemp(dir=state_dir, prefix=".probe-")
        os.close(fd)
        os.unlink(probe)
    except OSError as exc:
        raise ConfigError(f"state directory is not writable: {state_dir}: {exc.strerror}") from None


def _parse_timeout(raw: str | None) -> int:
    if raw is None or not raw.strip():
        return 30
    try:
        timeout = int(raw)
    except ValueError:
        raise ConfigError(f"poll timeout is not an integer: {raw!r}") from None
    if timeout < 0:
        raise ConfigError(f"poll timeout must not be negative: {timeout}")
    return timeout


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Build a validated config from the environment or raise ConfigError."""
    env = os.environ if env is None else env

    token_file = Path(
        env.get("SEBASTIAN_TELEGRAM_TOKEN_FILE") or _default_config_dir(env) / "token"
    ).expanduser()
    token = _read_token(token_file)

    allowlist = _parse_allowlist(env.get("SEBASTIAN_TELEGRAM_ALLOWLIST"))

    state_dir = Path(
        env.get("SEBASTIAN_TELEGRAM_STATE_DIR") or _default_state_dir(env)
    ).expanduser()
    _ensure_writable_state_dir(state_dir)

    vault_dir = Path(env.get("SEBASTIAN_TELEGRAM_VAULT_DIR") or _home(env) / "vault").expanduser()

    return Config(
        token=token,
        allowlist=allowlist,
        state_path=state_dir / "state.json",
        vault_dir=vault_dir,
        pi_bin=env.get("SEBASTIAN_TELEGRAM_PI_BIN") or "pi",
        poll_timeout=_parse_timeout(env.get("SEBASTIAN_TELEGRAM_POLL_TIMEOUT")),
        api_base_url=env.get("SEBASTIAN_TELEGRAM_API_BASE_URL") or "https://api.telegram.org",
    )
