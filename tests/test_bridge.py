import asyncio
import contextlib
import logging
from io import StringIO
from pathlib import Path

import pytest

from sebastian_telegram.bridge import build_bridge
from sebastian_telegram.config import load_config
from sebastian_telegram.log import configure_logging
from sebastian_telegram.pi import TurnResult
from sebastian_telegram.secret import Secret
from sebastian_telegram.state import State, StateStore

from .fakes import FakeClock, FakePi, FakeTelegram, update


def make_config(tmp_path: Path, allowlist: str = "7"):
    token_file = tmp_path / "token"
    token_file.write_text("123:abc\n")
    token_file.chmod(0o600)
    return load_config(
        {
            "SEBASTIAN_TELEGRAM_TOKEN_FILE": str(token_file),
            "SEBASTIAN_TELEGRAM_ALLOWLIST": allowlist,
            "SEBASTIAN_TELEGRAM_STATE_DIR": str(tmp_path / "state"),
            "SEBASTIAN_TELEGRAM_VAULT_DIR": str(tmp_path / "vault"),
        }
    )


def make_bridge(
    tmp_path: Path,
    allowlist: str = "7",
    results: list[TurnResult] | None = None,
    *,
    seed_offset: int | None = None,
    seed_sessions: dict[int, str] | None = None,
    gate: asyncio.Event | None = None,
    logger: logging.Logger | None = None,
):
    config = make_config(tmp_path, allowlist)
    if seed_offset is not None or seed_sessions is not None:
        StateStore(config.state_path).save(
            State(offset=seed_offset, sessions=dict(seed_sessions or {}))
        )
    telegram = FakeTelegram()
    pi = FakePi(results, gate=gate)
    clock = FakeClock()
    logger = logger or logging.getLogger("test.bridge")
    logger.addHandler(logging.NullHandler())
    bridge = build_bridge(config, telegram=telegram, pi=pi, clock=clock, logger=logger)
    return bridge, telegram, pi


def persisted_state(tmp_path: Path) -> State:
    return StateStore(tmp_path / "state" / "state.json").load()


async def wait_until(condition, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            raise AssertionError("condition was not met before the timeout")
        await asyncio.sleep(0.01)


async def run_until(bridge, condition, timeout: float = 2.0) -> None:
    task = asyncio.create_task(bridge.run())
    try:
        await wait_until(condition, timeout)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


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


async def test_restarting_between_messages_resumes_the_same_session(tmp_path):
    first, _, first_pi = make_bridge(tmp_path, results=[TurnResult.success("one")])
    await first.handle_update(update(chat_id=7, text="one"))

    assert persisted_state(tmp_path).sessions == {7: "tg-7"}

    second, _, second_pi = make_bridge(tmp_path, results=[TurnResult.success("two")])
    await second.handle_update(update(chat_id=7, text="two"))

    assert [turn.session_id for turn in first_pi.started + second_pi.started] == [
        "tg-7",
        "tg-7",
    ]


async def test_a_persisted_session_rotation_is_used_after_a_restart(tmp_path):
    bridge, _, pi = make_bridge(
        tmp_path,
        results=[TurnResult.success("after new")],
        seed_offset=5,
        seed_sessions={7: "tg-7-2"},
    )

    await bridge.handle_update(update(chat_id=7, text="hello"))

    assert pi.started[0].session_id == "tg-7-2"
    assert persisted_state(tmp_path).sessions == {7: "tg-7-2"}


async def test_handled_updates_advance_the_persisted_frontier_across_a_restart(tmp_path):
    bridge, telegram, pi = make_bridge(
        tmp_path,
        seed_offset=10,
        results=[TurnResult.success("first"), TurnResult.success("second")],
    )
    telegram.queue_updates(
        [
            update(chat_id=7, message_id=1, update_id=10, text="first"),
            update(chat_id=7, message_id=2, update_id=11, text="second"),
        ]
    )

    await run_until(bridge, lambda: len(telegram.sent) == 2)

    state = persisted_state(tmp_path)
    assert state.offset == 12
    assert state.sessions == {7: "tg-7"}

    restarted, restarted_telegram, _ = make_bridge(
        tmp_path, results=[TurnResult.success("third")]
    )
    restarted_telegram.queue_updates(
        [update(chat_id=7, message_id=3, update_id=12, text="third")]
    )

    await run_until(restarted, lambda: len(restarted_telegram.sent) == 1)

    assert restarted_telegram.offsets[0] == 12
    assert [sent.text for sent in restarted_telegram.sent] == ["third"]


async def test_a_message_left_unhandled_when_killed_is_redelivered(tmp_path):
    gate = asyncio.Event()
    bridge, telegram, pi = make_bridge(tmp_path, seed_offset=10, gate=gate)
    telegram.queue_updates(
        [update(chat_id=7, message_id=5, update_id=10, text="do not lose me")]
    )

    task = asyncio.create_task(bridge.run())
    await wait_until(lambda: len(pi.started) == 1)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert telegram.sent == []
    assert persisted_state(tmp_path).offset == 10

    restarted, restarted_telegram, restarted_pi = make_bridge(
        tmp_path, results=[TurnResult.success("done")]
    )
    restarted_telegram.queue_updates(
        [update(chat_id=7, message_id=5, update_id=10, text="do not lose me")]
    )

    await run_until(restarted, lambda: len(restarted_telegram.sent) == 1)

    assert restarted_telegram.offsets[0] == 10
    assert restarted_pi.started[0].prompt == "do not lose me"


async def test_first_start_discards_the_pending_backlog(tmp_path):
    bridge, telegram, pi = make_bridge(tmp_path)
    telegram.queue_updates(
        [
            update(chat_id=7, message_id=1, update_id=1, text="ancient"),
            update(chat_id=7, message_id=2, update_id=2, text="old"),
        ]
    )

    await run_until(bridge, lambda: persisted_state(tmp_path).offset == 3)

    assert telegram.sent == []
    assert pi.started == []

    restarted, restarted_telegram, restarted_pi = make_bridge(
        tmp_path, results=[TurnResult.success("fresh")]
    )
    restarted_telegram.queue_updates(
        [update(chat_id=7, message_id=3, update_id=3, text="today")]
    )

    await run_until(restarted, lambda: len(restarted_telegram.sent) == 1)

    assert restarted_telegram.offsets[0] == 3
    assert restarted_pi.started[0].prompt == "today"


def test_a_corrupt_state_file_is_logged_when_the_service_is_built(tmp_path):
    config = make_config(tmp_path)
    config.state_path.write_text("{not json")

    buffer = StringIO()
    logger = configure_logging(Secret("123:abc"), stream=buffer)
    build_bridge(
        config,
        telegram=FakeTelegram(),
        pi=FakePi(),
        clock=FakeClock(),
        logger=logger,
    )

    output = buffer.getvalue()
    assert "event=state_reinitialised" in output
    assert "reason=corrupt" in output


async def test_a_deleted_state_file_starts_cleanly_and_loses_the_session_mapping(tmp_path):
    make_bridge(
        tmp_path,
        seed_offset=5,
        seed_sessions={7: "tg-7-2"},
    )
    (tmp_path / "state" / "state.json").unlink()

    buffer = StringIO()
    logger = configure_logging(Secret("123:abc"), stream=buffer)
    bridge, _, pi = make_bridge(
        tmp_path, results=[TurnResult.success("hello")], logger=logger
    )

    await bridge.handle_update(update(chat_id=7, text="hello"))

    assert pi.started[0].session_id == "tg-7"
    output = buffer.getvalue()
    assert "event=state_reinitialised" in output
    assert "reason=missing" in output
