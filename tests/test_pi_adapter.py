import json
import os
import sys
from pathlib import Path

import pytest

from sebastian_telegram.bridge import build_bridge
from sebastian_telegram.config import load_config
from sebastian_telegram.pi import SubprocessPi
from sebastian_telegram.secret import Secret

from .fakes import FakeClock, FakeTelegram, update

BIN = Path(__file__).parent / "bin" / "fake_pi.py"
FIXTURES = Path(__file__).parent / "fixtures"


def fake_env(fixture=None, stderr=None, exit_code=0, record=None, sleep=None):
    env = {}
    if fixture:
        env["FAKE_PI_FIXTURE"] = str(FIXTURES / fixture)
    if stderr is not None:
        env["FAKE_PI_STDERR"] = stderr
    if record:
        env["FAKE_PI_RECORD"] = str(record)
    if sleep is not None:
        env["FAKE_PI_SLEEP"] = str(sleep)
    env["FAKE_PI_EXIT"] = str(exit_code)
    return env


def make_pi(tmp_path, **env_kwargs):
    return SubprocessPi(
        pi_bin=str(BIN),
        cwd=tmp_path,
        secret=Secret("123:abc"),
        env=fake_env(**env_kwargs),
    )


async def test_final_text_is_the_last_assistant_message_without_thinking_or_tools(tmp_path):
    pi = make_pi(tmp_path, fixture="final_text.jsonl")

    handle = await pi.start("tg-7", "hello")
    result = await handle.result()

    assert result.text == "The answer is _this_ and `that`."
    assert result.stop_reason == "stop"
    assert result.aborted is False
    assert result.exit_code == 0


async def test_prompt_beginning_with_a_dash_arrives_as_text_not_an_option(tmp_path):
    record = tmp_path / "record.json"
    pi = make_pi(tmp_path, record=record)

    handle = await pi.start("tg-7", "-rm -rf / --help")
    await handle.result()

    argv = json.loads(record.read_text())["argv"]
    assert argv[argv.index("--") + 1 :] == ["-rm -rf / --help"]
    assert "--mode" in argv and "json" in argv
    assert "-p" in argv
    assert argv[argv.index("--session-id") + 1] == "tg-7"


async def test_child_runs_in_its_own_process_group(tmp_path):
    record = tmp_path / "record.json"
    pi = make_pi(tmp_path, record=record, sleep=0.3)

    handle = await pi.start("tg-7", "hello")
    await handle.result()

    recorded = json.loads(record.read_text())
    assert recorded["pgrp"] == recorded["pid"]
    assert recorded["pgrp"] != os.getpgrp()


async def test_an_error_stop_reason_is_reported_even_when_pi_exits_zero(tmp_path):
    pi = make_pi(tmp_path, fixture="error_stop.jsonl", exit_code=0)

    result = await (await pi.start("tg-7", "hello")).result()

    assert result.stop_reason == "error"
    assert result.aborted is False
    assert result.exit_code == 0


async def test_an_aborted_run_is_reported(tmp_path):
    pi = make_pi(tmp_path, fixture="aborted.jsonl")

    result = await (await pi.start("tg-7", "hello")).result()

    assert result.aborted is True
    assert result.stop_reason == "aborted"


async def test_a_malformed_line_does_not_stop_parsing(tmp_path):
    pi = make_pi(tmp_path, fixture="malformed.jsonl")

    result = await (await pi.start("tg-7", "hello")).result()

    assert result.text == "survived"


async def test_stderr_tail_is_kept_and_scrubbed(tmp_path):
    pi = make_pi(tmp_path, stderr="boom: token 123:abc rejected", exit_code=1)

    result = await (await pi.start("tg-7", "hello")).result()

    assert result.exit_code == 1
    assert "boom" in result.error_summary
    assert "123:abc" not in result.error_summary


async def test_terminate_kills_the_whole_process_group(tmp_path):
    pi = make_pi(tmp_path, sleep=30)

    handle = await pi.start("tg-7", "hello")
    await handle.terminate(grace=0.2)
    result = await handle.result()

    assert result.aborted is True
    assert result.exit_code < 0


async def test_token_never_reaches_pi_argv_or_environment(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("123:abc\n")
    token_file.chmod(0o600)
    record = tmp_path / "record.json"
    (tmp_path / "vault").mkdir()
    config = load_config(
        {
            "SEBASTIAN_TELEGRAM_TOKEN_FILE": str(token_file),
            "SEBASTIAN_TELEGRAM_ALLOWLIST": "7",
            "SEBASTIAN_TELEGRAM_STATE_DIR": str(tmp_path / "state"),
            "SEBASTIAN_TELEGRAM_VAULT_DIR": str(tmp_path / "vault"),
            "SEBASTIAN_TELEGRAM_PI_BIN": str(BIN),
        }
    )
    pi = SubprocessPi(
        config.pi_bin,
        config.vault_dir,
        secret=config.token,
        env=fake_env(fixture="final_text.jsonl", record=record),
    )
    telegram = FakeTelegram()
    bridge = build_bridge(
        config, telegram=telegram, pi=pi, clock=FakeClock(), logger=__import__("logging").getLogger("t")
    )

    await bridge.handle_update(update(chat_id=7, text="hello"))

    recorded = json.loads(record.read_text())
    assert "123:abc" not in " ".join(recorded["argv"])
    assert all("123:abc" not in f"{key}={value}" for key, value in recorded["env"].items())
    assert telegram.sent[0].text == "The answer is _this_ and `that`."


async def test_missing_pi_binary_surfaces_as_a_failed_turn_not_a_hang(tmp_path):
    pi = SubprocessPi(pi_bin=str(tmp_path / "no-such-pi"), cwd=tmp_path, env={})

    with pytest.raises(OSError):
        await pi.start("tg-7", "hello")


async def test_a_length_stop_reason_reaches_the_bridge_without_thinking_or_tools(tmp_path):
    pi = make_pi(tmp_path, fixture="length.jsonl")

    result = await (await pi.start("tg-7", "write a lot")).result()

    assert result.stop_reason == "length"
    assert result.aborted is False
    assert result.text == "partial answer"
    assert "private reasoning" not in result.text
    assert "toolCall" not in result.text


async def test_the_text_comes_from_the_last_message_end_never_from_deltas(tmp_path):
    pi = make_pi(tmp_path, fixture="delta_stream.jsonl")

    result = await (await pi.start("tg-7", "hello")).result()

    assert result.text == "authoritative final text"
    assert "DELTA-CONTENT-MUST-NOT-WIN" not in result.text
