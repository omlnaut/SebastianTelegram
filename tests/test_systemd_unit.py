"""The supervision contract: the shipped unit and the file it reads.

These assert the deployment artifact itself, which is the externally visible
behaviour this ticket ships: the unit's identity, restart policy, journal
identifier and the environment file the operator edits.
"""

from __future__ import annotations

import getpass
import grp
import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from sebastian_telegram.config import load_config

from .unitfile import (
    ENTRYPOINT,
    ENV_PATH,
    IDENTIFIER,
    INSTALLED_ENV,
    ROOT,
    UNIT_PATH,
    read_env,
    read_unit,
)

ACCEPTANCE = ROOT / "deploy" / "acceptance.sh"

CONFIG_KEYS = {
    "SEBASTIAN_TELEGRAM_TOKEN_FILE",
    "SEBASTIAN_TELEGRAM_ALLOWLIST",
    "SEBASTIAN_TELEGRAM_STATE_DIR",
    "SEBASTIAN_TELEGRAM_VAULT_DIR",
    "SEBASTIAN_TELEGRAM_PI_BIN",
    "SEBASTIAN_TELEGRAM_POLL_TIMEOUT",
    "SEBASTIAN_TELEGRAM_API_BASE_URL",
}

# A Bot API token is "<bot id digits>:<35-ish base64url characters>". The
# environment file must never contain one; the token lives in its own 0600 file.
TOKEN_SHAPE = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b")


def test_the_unit_runs_the_real_entrypoint_without_a_shell_wrapper():
    service = read_unit()["Service"]

    argv = shlex.split(service["ExecStart"])

    assert argv == [ENTRYPOINT]
    assert service["Type"] == "simple"
    assert argv[0] not in {"sh", "bash", "dash", "sh -c", "/bin/sh", "/bin/bash"}


def test_the_entrypoint_matches_the_console_script_pyproject_declares():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    scripts = pyproject["project"]["scripts"]
    script_name = Path(ENTRYPOINT).name

    assert script_name in scripts
    assert scripts[script_name] == "sebastian_telegram.main:cli"


def test_the_unit_restarts_always_without_a_start_limit():
    unit = read_unit()

    assert unit["Service"]["Restart"] == "always"
    assert unit["Service"]["RestartSec"] == "5"
    assert unit["Unit"]["StartLimitIntervalSec"] == "0"


def test_the_unit_runs_as_the_non_root_pi_user_in_the_pi_home():
    service = read_unit()["Service"]

    assert service["User"] == "pi"
    assert service["Group"] == "pi"
    assert service["WorkingDirectory"] == "/home/pi"
    assert service["Environment"] == "HOME=/home/pi"


def test_the_journal_identifier_is_stable_and_explicit():
    service = read_unit()["Service"]

    assert service["SyslogIdentifier"] == IDENTIFIER
    assert service["StandardOutput"] == "journal"
    assert service["StandardError"] == "journal"


def test_the_unit_reads_the_shipped_environment_file():
    service = read_unit()["Service"]

    assert service["EnvironmentFile"] == INSTALLED_ENV
    assert ENV_PATH.exists()


def test_the_environment_file_sets_token_path_allowlist_and_paths():
    env = read_env()

    assert env["SEBASTIAN_TELEGRAM_TOKEN_FILE"] == "/home/pi/.config/sebastian-telegram/token"
    assert env["SEBASTIAN_TELEGRAM_STATE_DIR"] == "/home/pi/.local/state/sebastian-telegram"
    assert env["SEBASTIAN_TELEGRAM_VAULT_DIR"] == "/home/pi/vault"
    assert env["SEBASTIAN_TELEGRAM_PI_BIN"] == "/home/pi/.local/bin/pi"
    assert all(entry.strip().lstrip("-").isdigit() for entry in env["SEBASTIAN_TELEGRAM_ALLOWLIST"].split(","))


def test_the_environment_file_only_sets_variables_the_config_reads():
    assert set(read_env()) <= CONFIG_KEYS


def test_the_environment_file_has_no_token_in_it():
    env = read_env()

    assert "SEBASTIAN_TELEGRAM_TOKEN" not in env
    assert "SEBASTIAN_TELEGRAM_TOKEN_FILE" in env
    assert TOKEN_SHAPE.search(ENV_PATH.read_text()) is None


def test_the_shipped_environment_file_boots_a_valid_configuration(tmp_path):
    env = dict(read_env())
    token_file = tmp_path / "token"
    token_file.write_text("123:abc\n")
    token_file.chmod(0o600)
    env.update(
        {
            "SEBASTIAN_TELEGRAM_TOKEN_FILE": str(token_file),
            "SEBASTIAN_TELEGRAM_STATE_DIR": str(tmp_path / "state"),
            "SEBASTIAN_TELEGRAM_VAULT_DIR": str(tmp_path / "vault"),
            "HOME": "/home/pi",
        }
    )

    config = load_config(env)

    # 0 is the shipped placeholder: a valid chat ID that matches no real chat,
    # so a freshly installed bridge answers nobody until the operator sets it.
    assert config.allowlist == frozenset({0})
    assert config.state_path == tmp_path / "state" / "state.json"
    assert config.pi_bin == "/home/pi/.local/bin/pi"


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="systemd-analyze not installed")
def test_systemd_accepts_the_unit_file(tmp_path):
    """Syntax-check through systemd itself, with host-specific values swapped.

    ExecStart, EnvironmentFile, User and Group point at host paths that only
    provisioning creates, so the syntax check uses values that exist here. The
    literal values are asserted by the tests above.
    """
    env_copy = tmp_path / "env"
    env_copy.write_text(ENV_PATH.read_text())
    text = UNIT_PATH.read_text()
    text = text.replace(f"ExecStart={ENTRYPOINT}", f"ExecStart={sys.executable}")
    text = text.replace(f"EnvironmentFile={INSTALLED_ENV}", f"EnvironmentFile={env_copy}")
    text = re.sub(r"^User=.*$", f"User={getpass.getuser()}", text, flags=re.MULTILINE)
    text = re.sub(
        r"^Group=.*$",
        f"Group={grp.getgrgid(os.getgid()).gr_name}",
        text,
        flags=re.MULTILINE,
    )
    copy = tmp_path / "sebastian-telegram.service"
    copy.write_text(text)

    result = subprocess.run(
        ["systemd-analyze", "verify", str(copy)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_the_acceptance_script_is_runnable_and_valid_bash():
    assert os.access(ACCEPTANCE, os.X_OK)

    result = subprocess.run(
        ["bash", "-n", str(ACCEPTANCE)], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr
