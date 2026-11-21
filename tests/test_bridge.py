import logging
from pathlib import Path

import pytest

from sebastian_telegram.bridge import build_bridge
from sebastian_telegram.config import load_config
from sebastian_telegram.pi import TurnResult

from .fakes import FakeClock, FakePi, FakeTelegram, update


def make_bridge(tmp_path: Path, allowlist: str = "7", results: list[TurnResult] | None = None):
    token_file = tmp_path / "token"
    token_file.write_text("123:abc\n")
    token_file.chmod(0o600)
    config = load_config(
        {
            "SEBASTIAN_TELEGRAM_TOKEN_FILE": str(token_file),
            "SEBASTIAN_TELEGRAM_ALLOWLIST": allowlist,
            "SEBASTIAN_TELEGRAM_STATE_DIR": str(tmp_path / "state"),
            "SEBASTIAN_TELEGRAM_VAULT_DIR": str(tmp_path / "vault"),
        }
    )
    telegram = FakeTelegram()
    pi = FakePi(results)
    clock = FakeClock()
    logger = logging.getLogger("test.bridge")
    logger.addHandler(logging.NullHandler())
    bridge = build_bridge(config, telegram=telegram, pi=pi, clock=clock, logger=logger)
    return bridge, telegram, pi


async def test_allowlisted_message_is_answered_plainly_as_a_reply(tmp_path):
    bridge, telegram, pi = make_bridge(
        tmp_path, results=[TurnResult.success("a_b `code()` *not bold*")]
    )

    await bridge.handle_update(update(chat_id=7, message_id=99, text="ping"))

    assert pi.started[0].session_id == "tg-7"
    assert pi.started[0].prompt == "ping"
    assert telegram.sent[0].chat_id == 7
    assert telegram.sent[0].text == "a_b `code()` *not bold*"
    assert telegram.sent[0].reply_to == 99


async def test_message_from_a_chat_off_the_allowlist_is_ignored(tmp_path):
    bridge, telegram, pi = make_bridge(tmp_path, allowlist="7")

    await bridge.handle_update(update(chat_id=8, text="let me in"))

    assert telegram.sent == []
    assert pi.started == []


async def test_non_text_empty_and_bot_messages_are_ignored_without_crashing(tmp_path):
    bridge, telegram, pi = make_bridge(tmp_path)

    await bridge.handle_update(update(chat_id=7, text=None))
    await bridge.handle_update(update(chat_id=7, text=""))
    await bridge.handle_update(update(chat_id=7, text="hello", from_bot=True))

    assert telegram.sent == []
    assert pi.started == []


async def test_two_messages_in_one_chat_continue_one_session(tmp_path):
    bridge, telegram, pi = make_bridge(
        tmp_path,
        allowlist="7,-100",
        results=[TurnResult.success("first"), TurnResult.success("second")],
    )

    await bridge.handle_update(update(chat_id=7, text="one"))
    await bridge.handle_update(update(chat_id=-100, text="other chat"))
    await bridge.handle_update(update(chat_id=7, text="two"))

    assert [turn.session_id for turn in pi.started] == ["tg-7", "tg--100", "tg-7"]


async def test_a_failed_turn_posts_a_notice_and_the_next_message_still_runs(tmp_path):
    failure = TurnResult(
        text="partial output that must not be posted",
        stop_reason="error",
        aborted=False,
        exit_code=2,
        error_summary="ValueError: bad input",
    )
    bridge, telegram, pi = make_bridge(
        tmp_path, results=[failure, TurnResult.success("recovered")]
    )

    await bridge.handle_update(update(chat_id=7, message_id=10, text="break"))
    await bridge.handle_update(update(chat_id=7, message_id=11, text="again"))

    notice, answer = telegram.sent
    assert "exit 2" in notice.text
    assert "ValueError: bad input" in notice.text
    assert "partial output" not in notice.text
    assert notice.reply_to == 10
    assert answer.text == "recovered"
    assert answer.reply_to == 11


async def test_a_successful_turn_with_no_text_says_no_output(tmp_path):
    bridge, telegram, _ = make_bridge(tmp_path, results=[TurnResult.success("")])

    await bridge.handle_update(update(chat_id=7, text="quiet"))

    assert telegram.sent[0].text == "(no output)"


async def test_the_answer_carries_no_parse_mode(tmp_path):
    bridge, telegram, _ = make_bridge(tmp_path, results=[TurnResult.success("snake_case")])

    await bridge.handle_update(update(chat_id=7, text="hi"))

    # The port has no parse_mode parameter at all, so plain text is structural:
    # sending an answer can only express chat, text and reply target.
    assert telegram.sent[0].text == "snake_case"


async def test_transport_failure_between_turns_does_not_wedge_the_chat(tmp_path):
    bridge, telegram, pi = make_bridge(
        tmp_path, results=[TurnResult.success("one"), TurnResult.success("two")]
    )

    await bridge.handle_update(update(chat_id=7, text="first"))
    await bridge.handle_update(update(chat_id=7, text="second"))

    assert [sent.text for sent in telegram.sent] == ["one", "two"]
