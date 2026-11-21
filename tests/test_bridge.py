import asyncio
import contextlib
import json
import logging
from pathlib import Path

import pytest

from sebastian_telegram.bridge import build_bridge
from sebastian_telegram.config import load_config
from sebastian_telegram.pi import TurnResult

from .fakes import FakeClock, FakePi, FakeTelegram, update


def make_bridge(
    tmp_path: Path,
    allowlist: str = "7",
    results: list[TurnResult] | None = None,
    offset: int | None = None,
):
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
    if offset is not None:
        # A stored offset means this is not the first start, so the poll loop
        # serves updates instead of discarding the backlog.
        config.state_path.write_text(json.dumps({"offset": offset, "sessions": {}}))
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


# --- One worker per chat: FIFO, reactions, queued framing (#6) ---

HOURGLASS = "\N{HOURGLASS}"  # ⏳
EYES = "\N{EYES}"  # 👀


async def test_two_messages_in_quick_succession_run_one_after_the_other(tmp_path):
    bridge, telegram, pi = make_bridge(
        tmp_path,
        results=[TurnResult.success("first"), TurnResult.success("second")],
    )
    pi.hold(1)

    first = asyncio.create_task(
        bridge.handle_update(update(chat_id=7, message_id=1, text="one"))
    )
    await pi.wait_for_starts(1)

    second = asyncio.create_task(
        bridge.handle_update(update(chat_id=7, message_id=2, text="two"))
    )
    await asyncio.sleep(0)
    # The first turn is still in flight, so nothing else has started yet.
    assert [turn.prompt for turn in pi.started] == ["one"]

    pi.release(0)
    await asyncio.gather(first, second)

    # Same session, in arrival order, and never two processes on it at once.
    assert [turn.session_id for turn in pi.started] == ["tg-7", "tg-7"]
    assert [turn.prompt for turn in pi.started] == [
        "one",
        "(sent while you were working: two)",
    ]
    assert pi.max_active_by_session["tg-7"] == 1
    assert [sent.text for sent in telegram.sent] == ["first", "second"]


async def test_a_message_sent_mid_run_is_marked_hourglass_then_eyes(tmp_path):
    bridge, telegram, pi = make_bridge(
        tmp_path,
        results=[TurnResult.success("first"), TurnResult.success("second")],
    )
    pi.hold(1)

    first = asyncio.create_task(
        bridge.handle_update(update(chat_id=7, message_id=1, text="one"))
    )
    await pi.wait_for_starts(1)
    second = asyncio.create_task(
        bridge.handle_update(update(chat_id=7, message_id=2, text="actually two"))
    )
    await asyncio.sleep(0)
    pi.release(0)
    await asyncio.gather(first, second)

    assert telegram.reactions == [
        (7, 1, EYES),
        (7, 2, HOURGLASS),
        (7, 2, EYES),
    ]
    assert pi.started[1].prompt == "(sent while you were working: actually two)"


async def test_a_message_that_starts_immediately_gets_eyes_and_is_forwarded_verbatim(tmp_path):
    bridge, telegram, pi = make_bridge(tmp_path)

    await bridge.handle_update(update(chat_id=7, message_id=5, text="-not an option"))

    assert telegram.reactions == [(7, 5, EYES)]
    assert pi.started[0].prompt == "-not an option"


async def test_queued_messages_run_in_arrival_order(tmp_path):
    bridge, telegram, pi = make_bridge(
        tmp_path, results=[TurnResult.success("a"), TurnResult.success("b"), TurnResult.success("c")]
    )
    pi.hold(1)

    first = asyncio.create_task(
        bridge.handle_update(update(chat_id=7, message_id=1, text="m1"))
    )
    await pi.wait_for_starts(1)
    rest = [
        asyncio.create_task(
            bridge.handle_update(update(chat_id=7, message_id=n, text=f"m{n}"))
        )
        for n in (2, 3)
    ]
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    pi.release(0)
    await asyncio.gather(first, *rest)

    assert [turn.prompt for turn in pi.started] == [
        "m1",
        "(sent while you were working: m2)",
        "(sent while you were working: m3)",
    ]
    assert [sent.text for sent in telegram.sent] == ["a", "b", "c"]


async def test_the_queue_keeps_draining_after_a_failed_turn(tmp_path):
    failure = TurnResult(
        text="partial",
        stop_reason="error",
        aborted=False,
        exit_code=1,
        error_summary="boom",
    )
    bridge, telegram, pi = make_bridge(
        tmp_path, results=[failure, TurnResult.success("second")]
    )
    pi.hold(1)

    first = asyncio.create_task(
        bridge.handle_update(update(chat_id=7, message_id=1, text="break"))
    )
    await pi.wait_for_starts(1)
    second = asyncio.create_task(
        bridge.handle_update(update(chat_id=7, message_id=2, text="recover"))
    )
    await asyncio.sleep(0)
    pi.release(0)
    await asyncio.gather(first, second)

    assert "exit 1" in telegram.sent[0].text
    assert telegram.sent[1].text == "second"
    assert telegram.sent[1].reply_to == 2


async def test_a_long_run_in_one_chat_does_not_delay_another_chat(tmp_path):
    bridge, telegram, pi = make_bridge(
        tmp_path,
        allowlist="7,8",
        results=[TurnResult.success("long"), TurnResult.success("quick")],
    )
    pi.hold(1)

    slow = asyncio.create_task(
        bridge.handle_update(update(chat_id=7, message_id=1, text="slow"))
    )
    await pi.wait_for_starts(1)

    await asyncio.wait_for(
        bridge.handle_update(update(chat_id=8, message_id=2, text="quick")),
        timeout=1,
    )

    assert [sent.text for sent in telegram.sent] == ["quick"]
    assert not pi.handles[0].finished.is_set()

    pi.release(0)
    await slow
    assert [sent.text for sent in telegram.sent] == ["quick", "long"]


async def test_the_poll_loop_does_not_wait_for_a_running_turn(tmp_path):
    bridge, telegram, pi = make_bridge(
        tmp_path,
        allowlist="7,8",
        results=[TurnResult.success("long"), TurnResult.success("quick")],
        offset=1,
    )
    pi.hold(1)
    telegram.queue_updates(
        [
            update(chat_id=7, message_id=1, text="slow"),
            update(chat_id=8, message_id=2, text="quick"),
        ]
    )

    runner = asyncio.create_task(bridge.run())
    try:
        await pi.wait_for_starts(2)
        assert [turn.session_id for turn in pi.started] == ["tg-7", "tg-8"]

        pi.release(0)
        while len(telegram.sent) < 2:
            await asyncio.sleep(0)
    finally:
        runner.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await runner

    assert [sent.text for sent in telegram.sent] == ["quick", "long"]


async def test_a_failing_reaction_is_logged_and_the_turn_still_answers(tmp_path, caplog):
    bridge, telegram, pi = make_bridge(tmp_path, results=[TurnResult.success("answered")])
    telegram.reaction_error = RuntimeError("reaction rejected")

    with caplog.at_level(logging.WARNING):
        await bridge.handle_update(update(chat_id=7, message_id=1, text="hi"))

    assert [sent.text for sent in telegram.sent] == ["answered"]
    assert telegram.reactions == []
    logged = [record.getMessage() for record in caplog.records]
    assert any("event=reaction_failed" in line for line in logged)
    assert any("reaction rejected" in line for line in logged)
