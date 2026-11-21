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
    offset: int | None = None,
    seed_offset: int | None = None,
    seed_sessions: dict[int, str] | None = None,
    gate: asyncio.Event | None = None,
    logger: logging.Logger | None = None,
    clock: FakeClock | None = None,
):
    config = make_config(tmp_path, allowlist)
    # A stored offset means this is not the first start, so the poll loop
    # serves updates instead of discarding the backlog.
    saved_offset = offset if offset is not None else seed_offset
    if saved_offset is not None or seed_sessions is not None:
        StateStore(config.state_path).save(
            State(offset=saved_offset, sessions=dict(seed_sessions or {}))
        )
    telegram = FakeTelegram()
    pi = FakePi(results, gate=gate)
    clock = clock or FakeClock()
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


async def test_a_dropped_poll_resumes_without_restarting_the_service(tmp_path, monkeypatch):
    import asyncio
    from contextlib import suppress

    from sebastian_telegram import bridge as bridge_module
    from sebastian_telegram.state import State, StateStore
    from sebastian_telegram.telegram import TelegramError

    monkeypatch.setattr(bridge_module, "_POLL_RETRY_DELAY", 0.01)
    # A resumed service, not a first start: it has an offset to poll from.
    StateStore(tmp_path / "state" / "state.json").save(State(offset=0, sessions={7: "tg-7"}))
    bridge, telegram, pi = make_bridge(
        tmp_path, results=[TurnResult.success("still here")]
    )
    telegram.fail_polls.append(TelegramError("getUpdates: RemoteProtocolError: reset"))
    telegram.queue_updates([update(chat_id=7, message_id=42, text="ping")])

    task = asyncio.create_task(bridge.run())
    try:
        await _until(lambda: telegram.sent)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    assert [sent.text for sent in telegram.sent] == ["still here"]
    assert telegram.sent[0].reply_to == 42
    assert len(pi.started) == 1


async def _until(predicate, timeout: float = 1.0) -> None:
    import asyncio

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("timed out waiting for the bridge")
        await asyncio.sleep(0.001)


async def test_a_failed_delivery_is_not_retried_by_the_core(tmp_path):
    from sebastian_telegram.telegram import TelegramError

    bridge, telegram, pi = make_bridge(tmp_path, results=[TurnResult.success("one")])
    telegram.fail_sends.append(TelegramError("sendMessage: 502 Bad Gateway"))

    # The chat's worker reports the failure and moves on; it never re-sends.
    await bridge.handle_update(update(chat_id=7, text="hi"))

    # The turn ran once; the answer was attempted once and not re-sent, so no
    # message can ever be answered twice by a retry.
    assert len(pi.started) == 1
    assert telegram.send_attempts == 1
    assert telegram.sent == []
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


# --- /new: the only reset, applied at its place in the queue (#9) ---


async def test_new_while_idle_starts_the_next_message_on_a_new_session(tmp_path):
    bridge, telegram, pi = make_bridge(
        tmp_path,
        results=[TurnResult.success("old answer"), TurnResult.success("new answer")],
    )

    await bridge.handle_update(update(chat_id=7, message_id=1, text="before"))
    await bridge.handle_update(update(chat_id=7, message_id=2, text="/new"))
    await bridge.handle_update(update(chat_id=7, message_id=3, text="after"))

    assert [turn.session_id for turn in pi.started] == ["tg-7", "tg-7-1"]
    assert [turn.prompt for turn in pi.started] == ["before", "after"]
    assert [sent.text for sent in telegram.sent] == ["old answer", "new answer"]
    assert persisted_state(tmp_path).sessions == {7: "tg-7-1"}


async def test_new_while_busy_applies_at_its_place_in_the_queue(tmp_path):
    bridge, telegram, pi = make_bridge(
        tmp_path,
        results=[
            TurnResult.success("one"),
            TurnResult.success("two"),
            TurnResult.success("three"),
        ],
    )
    pi.hold(1)

    first = asyncio.create_task(
        bridge.handle_update(update(chat_id=7, message_id=1, text="one"))
    )
    await pi.wait_for_starts(1)

    # Queued ahead of /new: must finish on the old session.
    ahead = asyncio.create_task(
        bridge.handle_update(update(chat_id=7, message_id=2, text="two"))
    )
    await asyncio.sleep(0)
    new = asyncio.create_task(
        bridge.handle_update(update(chat_id=7, message_id=3, text="/new"))
    )
    await asyncio.sleep(0)
    # Queued after /new: must start on the new session.
    after = asyncio.create_task(
        bridge.handle_update(update(chat_id=7, message_id=4, text="three"))
    )
    await asyncio.sleep(0)

    assert [turn.session_id for turn in pi.started] == ["tg-7"]
    pi.release(0)
    await asyncio.gather(first, ahead, new, after)

    assert [turn.session_id for turn in pi.started] == ["tg-7", "tg-7", "tg-7-1"]
    assert [turn.prompt for turn in pi.started] == [
        "one",
        "(sent while you were working: two)",
        "(sent while you were working: three)",
    ]
    assert [sent.text for sent in telegram.sent] == ["one", "two", "three"]


async def test_new_does_not_stop_or_truncate_the_running_turn(tmp_path):
    bridge, telegram, pi = make_bridge(
        tmp_path, results=[TurnResult.success("the whole answer")]
    )
    pi.hold(1)

    turn = asyncio.create_task(
        bridge.handle_update(update(chat_id=7, message_id=1, text="a long turn"))
    )
    await pi.wait_for_starts(1)

    new = asyncio.create_task(
        bridge.handle_update(update(chat_id=7, message_id=2, text="/new"))
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # The turn is still the only one running, and nothing was cancelled.
    assert pi.active == 1
    assert pi.handles[0].terminated is False
    assert pi.handles[0].finished.is_set() is False

    pi.release(0)
    await asyncio.gather(turn, new)

    assert pi.handles[0].terminated is False
    assert [sent.text for sent in telegram.sent] == ["the whole answer"]


async def test_generations_increase_and_are_never_reused_across_a_restart(tmp_path):
    first, _, first_pi = make_bridge(
        tmp_path,
        results=[
            TurnResult.success("one"),
            TurnResult.success("two"),
            TurnResult.success("three"),
        ],
    )

    await first.handle_update(update(chat_id=7, text="one"))
    await first.handle_update(update(chat_id=7, text="/new"))
    await first.handle_update(update(chat_id=7, text="two"))
    await first.handle_update(update(chat_id=7, text="/new"))
    await first.handle_update(update(chat_id=7, text="three"))

    assert [turn.session_id for turn in first_pi.started] == ["tg-7", "tg-7-1", "tg-7-2"]

    second, _, second_pi = make_bridge(
        tmp_path,
        results=[TurnResult.success("four"), TurnResult.success("five")],
    )

    # The mapping survives the restart, so the next message continues it.
    await second.handle_update(update(chat_id=7, text="four"))
    await second.handle_update(update(chat_id=7, text="/new"))
    await second.handle_update(update(chat_id=7, text="five"))

    assert [turn.session_id for turn in second_pi.started] == ["tg-7-2", "tg-7-3"]
    assert persisted_state(tmp_path).sessions == {7: "tg-7-3"}


async def test_new_with_a_botname_suffix_is_the_same_command(tmp_path):
    bridge, _, pi = make_bridge(
        tmp_path, results=[TurnResult.success("one"), TurnResult.success("two")]
    )

    await bridge.handle_update(update(chat_id=7, message_id=1, text="one"))
    await bridge.handle_update(
        update(chat_id=7, message_id=2, text="/new@sebastian_telegram_bot")
    )
    await bridge.handle_update(update(chat_id=7, message_id=3, text="two"))

    assert [turn.session_id for turn in pi.started] == ["tg-7", "tg-7-1"]
    assert [turn.prompt for turn in pi.started] == ["one", "two"]


async def test_new_in_a_group_chat_keeps_the_sign_in_the_session_id(tmp_path):
    bridge, _, pi = make_bridge(
        tmp_path,
        allowlist="-100123",
        results=[TurnResult.success("one"), TurnResult.success("two")],
    )

    await bridge.handle_update(update(chat_id=-100123, message_id=1, text="one"))
    await bridge.handle_update(update(chat_id=-100123, message_id=2, text="/new"))
    await bridge.handle_update(update(chat_id=-100123, message_id=3, text="two"))

    assert [turn.session_id for turn in pi.started] == ["tg--100123", "tg--100123-1"]
    assert persisted_state(tmp_path).sessions == {-100123: "tg--100123-1"}


async def test_a_failed_turn_and_a_day_of_idling_do_not_reset_the_session(tmp_path):
    failure = TurnResult(
        text="",
        stop_reason="error",
        aborted=False,
        exit_code=1,
        error_summary="boom",
    )
    clock = FakeClock()
    bridge, telegram, pi = make_bridge(
        tmp_path,
        results=[failure, TurnResult.success("still the same session")],
        clock=clock,
    )

    await bridge.handle_update(update(chat_id=7, message_id=1, text="break"))
    clock.wall += 86_400
    clock.mono += 86_400
    await bridge.handle_update(update(chat_id=7, message_id=2, text="a day later"))

    assert [turn.session_id for turn in pi.started] == ["tg-7", "tg-7"]
    assert telegram.sent[1].text == "still the same session"
