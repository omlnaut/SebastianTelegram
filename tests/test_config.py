import os
import stat

import pytest

from sebastian_telegram.config import ConfigError, load_config


def write_token(path, token="123:abc", mode=0o600):
    path.write_text(token + "\n")
    path.chmod(mode)
    return path


def env_for(tmp_path, token_file, allowlist="42,-100", **overrides):
    env = {
        "SEBASTIAN_TELEGRAM_TOKEN_FILE": str(token_file),
        "SEBASTIAN_TELEGRAM_ALLOWLIST": allowlist,
        "SEBASTIAN_TELEGRAM_STATE_DIR": str(tmp_path / "state"),
        "SEBASTIAN_TELEGRAM_VAULT_DIR": str(tmp_path / "vault"),
    }
    env.update(overrides)
    return env


def test_loads_token_allowlist_and_paths(tmp_path):
    token_file = write_token(tmp_path / "token")

    config = load_config(env_for(tmp_path, token_file))

    assert config.token.reveal() == "123:abc"
    assert config.allowlist == frozenset({42, -100})
    assert config.state_path == tmp_path / "state" / "state.json"
    assert config.vault_dir == tmp_path / "vault"


def test_token_is_never_shown_in_repr(tmp_path):
    token_file = write_token(tmp_path / "token")

    config = load_config(env_for(tmp_path, token_file))

    assert "123:abc" not in repr(config)
    assert "123:abc" not in str(config.token)


def test_missing_token_file_fails_loudly(tmp_path):
    with pytest.raises(ConfigError, match="token file"):
        load_config(env_for(tmp_path, tmp_path / "absent"))


def test_token_file_with_group_or_other_bits_fails_loudly(tmp_path):
    token_file = write_token(tmp_path / "token", mode=0o644)

    with pytest.raises(ConfigError, match="permission"):
        load_config(env_for(tmp_path, token_file))


@pytest.mark.parametrize("mode", [0o600, 0o400, 0o700])
def test_token_file_without_group_or_other_bits_is_accepted(tmp_path, mode):
    token_file = write_token(tmp_path / "token", mode=mode)

    assert load_config(env_for(tmp_path, token_file)).token.reveal() == "123:abc"


@pytest.mark.parametrize("allowlist", ["", "  ", "42,nope", "42,,43"])
def test_invalid_allowlist_fails_loudly(tmp_path, allowlist):
    token_file = write_token(tmp_path / "token")

    with pytest.raises(ConfigError, match="allowlist"):
        load_config(env_for(tmp_path, token_file, allowlist=allowlist))


def test_unwritable_state_directory_fails_loudly(tmp_path):
    token_file = write_token(tmp_path / "token")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)

    try:
        with pytest.raises(ConfigError, match="state director"):
            load_config(env_for(tmp_path, token_file))
    finally:
        state_dir.chmod(stat.S_IRWXU)


def test_defaults_come_from_environment_paths(tmp_path):
    token_file = write_token(tmp_path / "token")
    env = {
        "SEBASTIAN_TELEGRAM_TOKEN_FILE": str(token_file),
        "SEBASTIAN_TELEGRAM_ALLOWLIST": "7",
        "XDG_STATE_HOME": str(tmp_path / "xdg-state"),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
        "HOME": str(tmp_path / "home"),
    }

    config = load_config(env)

    assert config.state_path == tmp_path / "xdg-state" / "sebastian-telegram" / "state.json"
    assert config.vault_dir == tmp_path / "home" / "vault"
    assert config.pi_bin == "pi"
    assert config.poll_timeout == 30
    assert config.api_base_url == "https://api.telegram.org"


def test_the_state_path_is_configurable_while_the_state_dir_stays_its_default_parent(tmp_path):
    token_file = write_token(tmp_path / "token")
    explicit = tmp_path / "elsewhere" / "offset.json"

    config = load_config(
        env_for(tmp_path, token_file, SEBASTIAN_TELEGRAM_STATE_PATH=str(explicit))
    )

    assert config.state_path == explicit
    assert config.state_path.parent.exists()

    defaulted = load_config(env_for(tmp_path, token_file))
    assert defaulted.state_path == tmp_path / "state" / "state.json"


def test_a_state_path_in_an_unwritable_directory_fails_loudly(tmp_path):
    token_file = write_token(tmp_path / "token")
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(stat.S_IRUSR | stat.S_IXUSR)

    try:
        with pytest.raises(ConfigError, match="not writable"):
            load_config(
                env_for(
                    tmp_path,
                    token_file,
                    SEBASTIAN_TELEGRAM_STATE_PATH=str(locked / "state.json"),
                )
            )
    finally:
        locked.chmod(stat.S_IRWXU)


def test_state_directory_is_created_private(tmp_path):
    token_file = write_token(tmp_path / "token")

    config = load_config(env_for(tmp_path, token_file))

    mode = os.stat(config.state_path.parent).st_mode
    assert mode & 0o077 == 0
