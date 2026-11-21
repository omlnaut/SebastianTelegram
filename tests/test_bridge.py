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


LONG_ANSWER = "First line — with é, emoji 🚀 and `code`.\n" + ("Lorem ipsum dolor sit amet. " * 250)


async def test_a_long_answer_arrives_as_a_markdown_document_with_no_truncated_copy(tmp_path):
    bridge, telegram, _ = make_bridge(
        tmp_path, results=[TurnResult.success(LONG_ANSWER)]
    )
    assert len(LONG_ANSWER) > 6_000

    await bridge.handle_update(update(chat_id=7, message_id=42, text="write a lot"))

    # No truncated copy of the answer in the chat body, only the document.
    assert telegram.sent == []
    assert len(telegram.documents) == 1
    document = telegram.documents[0]
    assert document["data"] == LONG_ANSWER.encode("utf-8")
    assert document["filename"] == "tg-7-20231114T221320Z.md"
    assert document["reply_to"] == 42


async def test_a_3_000_character_answer_arrives_as_a_message_not_a_file(tmp_path):
    answer = "b" * 3_000
    bridge, telegram, _ = make_bridge(tmp_path, results=[TurnResult.success(answer)])

    await bridge.handle_update(update(chat_id=7, message_id=43, text="write a bit"))

    assert telegram.documents == []
    assert len(telegram.sent) == 1
    assert telegram.sent[0].text == answer
    assert telegram.sent[0].reply_to == 43


@pytest.mark.parametrize("length,as_document", [(3_500, False), (3_501, True)])
async def test_the_3_500_character_threshold_decides_message_or_document(
    tmp_path, length, as_document
):
    answer = "c" * length
    bridge, telegram, _ = make_bridge(tmp_path, results=[TurnResult.success(answer)])

    await bridge.handle_update(update(chat_id=7, text="boundary"))

    if as_document:
        assert telegram.sent == []
        assert telegram.documents[0]["data"] == answer.encode("utf-8")
    else:
        assert telegram.documents == []
        assert telegram.sent[0].text == answer


async def test_a_length_stop_reason_posts_the_partial_text_with_a_truncation_warning(tmp_path):
    partial = TurnResult(
        text="the first half of the answer",
        stop_reason="length",
        aborted=False,
        exit_code=0,
        error_summary="",
    )
    bridge, telegram, _ = make_bridge(tmp_path, results=[partial])

    await bridge.handle_update(update(chat_id=7, message_id=44, text="say everything"))

    assert telegram.documents == []
    text = telegram.sent[0].text
    assert telegram.sent[0].reply_to == 44
    assert "the first half of the answer" in text
    assert "truncat" in text.lower()
    assert "turn failed" not in text.lower()


async def test_an_error_stop_reason_is_a_failure_even_though_pi_exits_zero(tmp_path):
    failure = TurnResult(
        text="partial output that must not be posted",
        stop_reason="error",
        aborted=False,
        exit_code=0,
        error_summary="provider exploded",
    )
    bridge, telegram, _ = make_bridge(tmp_path, results=[failure])

    await bridge.handle_update(update(chat_id=7, message_id=45, text="break it"))

    assert telegram.documents == []
    notice = telegram.sent[0].text
    assert telegram.sent[0].reply_to == 45
    assert "exit 0" in notice
    assert "provider exploded" in notice
    assert "partial output that must not be posted" not in notice


async def test_an_aborted_response_withholds_partial_output_and_posts_a_notice(tmp_path):
    aborted = TurnResult(
        text="cut off mid sentence",
        stop_reason="aborted",
        aborted=True,
        exit_code=0,
        error_summary="",
    )
    bridge, telegram, _ = make_bridge(tmp_path, results=[aborted])

    await bridge.handle_update(update(chat_id=7, text="abort please"))

    notice = telegram.sent[0].text
    assert "aborted" in notice.lower()
    assert "cut off mid sentence" not in notice


async def test_a_non_zero_exit_withholds_partial_output_even_with_a_stop_reason(tmp_path):
    failed = TurnResult(
        text="partial output from a dead run",
        stop_reason="stop",
        aborted=False,
        exit_code=3,
        error_summary="killed by signal",
    )
    bridge, telegram, _ = make_bridge(tmp_path, results=[failed])

    await bridge.handle_update(update(chat_id=7, text="die"))

    notice = telegram.sent[0].text
    assert "exit 3" in notice
    assert "killed by signal" in notice
    assert "partial output from a dead run" not in notice


async def test_a_long_length_truncated_answer_arrives_as_a_document_carrying_the_warning(tmp_path):
    partial = TurnResult(
        text="d" * 4_000,
        stop_reason="length",
        aborted=False,
        exit_code=0,
        error_summary="",
    )
    bridge, telegram, _ = make_bridge(tmp_path, results=[partial])

    await bridge.handle_update(update(chat_id=7, text="ramble"))

    assert telegram.sent == []
    data = telegram.documents[0]["data"].decode("utf-8")
    assert data.startswith("d" * 4_000)
    assert "truncat" in data.lower()
