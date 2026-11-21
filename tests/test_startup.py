"""Startup validation through the composition path the service actually runs.

The spec asks for misconfiguration to be caught the way the service builds
itself, so these call `serve()` — exactly what `cli()` runs and what the
systemd unit starts — rather than `load_config()` in isolation. The valid-path
end of the same seam is `tests/test_service_restart.py`, which runs the real
entrypoint against a fake Bot API.
"""

from __future__ import annotations

import stat

import pytest

from sebastian_telegram.config import ConfigError
from sebastian_telegram.main import cli, serve


def env_for(tmp_path, *, token_mode: int = 0o600, **overrides):
    token_file = tmp_path / "token"
    token_file.write_text("123:abc\n")
    token_file.chmod(token_mode)
    env = {
        "SEBASTIAN_TELEGRAM_TOKEN_FILE": str(token_file),
        "SEBASTIAN_TELEGRAM_ALLOWLIST": "42",
        "SEBASTIAN_TELEGRAM_STATE_DIR": str(tmp_path / "state"),
        "SEBASTIAN_TELEGRAM_VAULT_DIR": str(tmp_path / "vault"),
    }
    env.update(overrides)
    return env


async def test_a_missing_token_file_stops_the_service_before_it_polls(tmp_path):
    env = env_for(tmp_path, SEBASTIAN_TELEGRAM_TOKEN_FILE=str(tmp_path / "absent"))

    with pytest.raises(ConfigError, match="token file"):
        await serve(env)


async def test_a_loose_token_file_stops_the_service_before_it_polls(tmp_path):
    env = env_for(tmp_path, token_mode=0o644)

    with pytest.raises(ConfigError, match="permission"):
        await serve(env)


async def test_an_invalid_allowlist_stops_the_service_before_it_polls(tmp_path):
    env = env_for(tmp_path, SEBASTIAN_TELEGRAM_ALLOWLIST="42,nope")

    with pytest.raises(ConfigError, match="allowlist"):
        await serve(env)


async def test_an_unwritable_state_directory_stops_the_service_before_it_polls(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        with pytest.raises(ConfigError, match="state director"):
            await serve(env_for(tmp_path))
    finally:
        state_dir.chmod(stat.S_IRWXU)


def test_cli_exits_non_zero_with_a_clear_message_on_misconfiguration(tmp_path, monkeypatch, capsys):
    for name, value in env_for(tmp_path).items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("SEBASTIAN_TELEGRAM_TOKEN_FILE", str(tmp_path / "absent"))

    with pytest.raises(SystemExit) as exit_info:
        cli()

    assert exit_info.value.code == 1
    assert "token file" in capsys.readouterr().err
