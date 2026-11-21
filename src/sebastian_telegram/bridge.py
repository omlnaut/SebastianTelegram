"""The bridge core: allowlist, one turn per message, one answer back.

It owns no I/O: Telegram, pi and the clock are injected ports, so every
behaviour here is observable at that seam.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from .clock import Clock
from .config import Config
from .log import log_event
from .pi import Pi, TurnResult
from .scheduler import ChatStatus, Scheduler, Turn
from .secret import scrub
from .state import StateStore
from .telegram import Telegram, TelegramError, Update

_FAILED_STOP_REASONS = frozenset({"error", "aborted", "none"})
_NOTICE_TAIL_LIMIT = 300
#: How long the poll loop waits after the adapter has given up on a poll.
_POLL_RETRY_DELAY = 1.0

#: Command names the bridge handles itself rather than forwarding to `pi`.
# Plain-text Bot API messages stop at 4 096 characters; 3 500 leaves room for
# the reply and matches the settled decision.
MAX_MESSAGE_CHARS = 3_500

NOTHING_RUNNING = "nothing is running"


def parse_command(text: str) -> str | None:
    """The bare command name a text invokes, or None when it is not a command.

    Commands are exact and lowercase, and tolerate an `@botname` suffix so they
    work in group chats. The bridge compares the result with its own commands;
    every other slash command, and all plain text, is forwarded to the agent
    verbatim.
    """
    token = text.split(None, 1)[0]
    if not token.startswith("/"):
        return None
    return token[1:].split("@", 1)[0]


def answer_filename(chat_id: int, completed_at: float) -> str:
    """A `.md` name derived from the chat ID and the completion timestamp."""
    stamp = datetime.fromtimestamp(completed_at, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"tg-{chat_id}-{stamp}.md"


def truncation_warning(stop_reason: str) -> str:
    """An explicit warning that the answer stops at the model's output limit."""
    return (
        f"⚠️ output truncated: the assistant hit the {stop_reason} limit, "
        "so this answer is incomplete."
    )


def failure_notice(result: TurnResult) -> str:
    """A short, scrubbed notice: exit code plus the tail of stderr."""
    tail = " ".join((result.error_summary or "").split())
    if len(tail) > _NOTICE_TAIL_LIMIT:
        tail = tail[: _NOTICE_TAIL_LIMIT - 3] + "..."
    head = f"turn failed (exit {result.exit_code}"
    if result.stop_reason and result.stop_reason != "none":
        head += f", {result.stop_reason}"
    head += ")"
    return f"{head}: {tail}" if tail else head


def status_line(status: ChatStatus, session_id: str, clock: Clock) -> str:
    """One short plain-text line: what is running, and how much is waiting.

    Elapsed time comes from the injected clock's monotonic reading, so a test
    can move it forward without sleeping.
    """
    if status.running and status.started_at is not None:
        elapsed = max(0.0, clock.monotonic() - status.started_at)
        head = f"running for {int(elapsed)}s"
    else:
        head = "idle"
    return f"{head}, {status.queued} queued, session {session_id}"


def _failed(result: TurnResult) -> bool:
    return result.aborted or result.exit_code != 0 or result.stop_reason in _FAILED_STOP_REASONS


class Bridge:
    def __init__(
        self,
        config: Config,
        telegram: Telegram,
        pi: Pi,
        clock: Clock,
        state: StateStore,
        logger: logging.Logger,
    ) -> None:
        self._config = config
        self._telegram = telegram
        self._pi = pi
        self._clock = clock
        self._state = state
        self._logger = logger
        # Load eagerly so a missing or corrupt file is logged at startup.
        self._state.load()
        self._frontier = self._state.offset()
        self._unhandled: set[int] = set()
        self._runtime_offset: int | None = self._frontier
        # One worker per chat, holding a FIFO: a second message for a busy chat
        # waits its turn instead of starting a second process on the session.
        self._scheduler = Scheduler(
            telegram,
            logger,
            self._run_turn,
            self._start_new_session,
            clock=clock,
            secret=config.token,
        )
        self._pending: set[asyncio.Task[None]] = set()

    async def run(self) -> None:
        frontier = self._state.offset()
        if frontier is None:
            frontier = await self._discard_backlog()
            self._set_frontier(frontier)
        self._frontier = frontier
        self._unhandled.clear()
        self._runtime_offset = frontier
        try:
            while True:
                updates = await self._poll(self._runtime_offset, self._config.poll_timeout)

                # Advance the runtime offset past the whole batch before doing any
                # work, so Telegram cannot re-deliver what is already in memory.
                for update in updates:
                    if (
                        self._runtime_offset is None
                        or update.update_id + 1 > self._runtime_offset
                    ):
                        self._runtime_offset = update.update_id + 1
                    self._unhandled.add(update.update_id)

                # Dispatch without waiting: a long turn in one chat must not hold
                # up polling, and so must not delay another chat.
                for update in updates:
                    self._dispatch(update)
        finally:
            for task in set(self._pending):
                task.cancel()
            await asyncio.gather(*self._pending, return_exceptions=True)

    def _dispatch(self, update: Update) -> None:
        task = asyncio.create_task(self._handle_update_safely(update))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _handle_update_safely(self, update: Update) -> None:
        try:
            await self.handle_update(update)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # one bad update must not stop the bridge
            # Leave the update unhandled so the persisted frontier stays behind
            # it and a restart re-delivers it.
            log_event(
                self._logger,
                logging.ERROR,
                "update_failed",
                chat=update.chat_id,
                update=update.update_id,
                error=scrub(f"{type(exc).__name__}: {exc}", self._config.token),
            )
            return

        self._unhandled.discard(update.update_id)
        self._set_frontier(self._next_frontier())

    def _next_frontier(self) -> int | None:
        # The frontier is the first update not yet fully handled, across all
        # chats; once nothing is outstanding it catches up to the runtime one.
        return min(self._unhandled) if self._unhandled else self._runtime_offset

    def _set_frontier(self, frontier: int | None) -> None:
        if frontier is None or frontier == self._frontier:
            return
        self._frontier = frontier
        self._state.set_offset(frontier)
        self._state.persist()

    async def _poll(self, offset: int | None, timeout: int) -> list[Update]:
        """One long-poll; a dropped connection is retried, never fatal."""
        while True:
            try:
                return await self._telegram.get_updates(offset, timeout)
            except TelegramError as exc:
                log_event(self._logger, logging.ERROR, "poll_failed", error=str(exc))
                await asyncio.sleep(_POLL_RETRY_DELAY)

    async def _discard_backlog(self) -> int | None:
        """First start: drain and drop whatever Telegram still holds."""
        offset: int | None = None
        while True:
            updates = await self._poll(offset, 0)
            if not updates:
                return offset
            offset = max(update.update_id for update in updates) + 1

    async def handle_update(self, update: Update) -> None:
        if not self._is_for_us(update):
            return
        command = parse_command(update.text or "")
        # `/stop` is handled out of band: it must never queue behind the work
        # it is meant to control.
        if command == "stop":
            await self._handle_stop(update)
            return
        # `/status` is out of band for the same reason: it reports on the run
        # it must not queue behind.
        if command == "status":
            await self._handle_status(update)
            return
        if command == "new":
            # A control marker, not out-of-band: it takes effect at its FIFO
            # position, and never interrupts the turn that is running.
            await self._scheduler.submit_new(update)
            return
        # Enqueues, marks and waits for this message's own turn to finish; the
        # chat's worker is what actually runs it.
        await self._scheduler.submit(update)

    async def _handle_stop(self, update: Update) -> None:
        assert update.chat_id is not None
        stopped = await self._scheduler.stop(update.chat_id)
        log_event(
            self._logger,
            logging.INFO,
            "stop_requested",
            chat=update.chat_id,
            update=update.update_id,
            stopped=stopped,
        )
        if not stopped:
            await self._telegram.send_message(
                update.chat_id, NOTHING_RUNNING, reply_to=update.message_id
            )

    async def _handle_status(self, update: Update) -> None:
        assert update.chat_id is not None
        status = self._scheduler.status(update.chat_id)
        session_id = self._state.session_for(update.chat_id)
        log_event(
            self._logger,
            logging.INFO,
            "status_requested",
            chat=update.chat_id,
            update=update.update_id,
            session=session_id,
            running=status.running,
            queued=status.queued,
        )
        await self._telegram.send_message(
            update.chat_id,
            status_line(status, session_id, self._clock),
            reply_to=update.message_id,
        )

    def _is_for_us(self, update: Update) -> bool:
        if update.chat_id is None or update.from_bot:
            return False
        if update.chat_id not in self._config.allowlist:
            return False
        return bool(update.text and update.text.strip())

    async def _start_new_session(self, update: Update) -> None:
        """Mint and persist the chat's next generation; never reuse one."""
        assert update.chat_id is not None
        session_id = self._state.start_new_session(update.chat_id)
        self._state.persist()
        log_event(
            self._logger,
            logging.INFO,
            "session_new",
            chat=update.chat_id,
            update=update.update_id,
            session=session_id,
        )

    async def _run_turn(self, update: Update, prompt: str, turn: Turn) -> None:
        assert update.chat_id is not None
        chat_id = update.chat_id
        session_id = self._state.session_for(chat_id)
        self._state.remember_session(chat_id, session_id)
        self._state.persist()
        log_event(
            self._logger,
            logging.INFO,
            "turn_start",
            chat=chat_id,
            update=update.update_id,
            session=session_id,
        )

        try:
            handle = await self._pi.start(session_id, prompt)
        except OSError as exc:
            # Nothing to kill, but a `/stop` that raced the spawn must not wait
            # on a handle that will never exist.
            turn.attach(None)
            error = scrub(f"{type(exc).__name__}: {exc}", self._config.token)
            log_event(
                self._logger,
                logging.ERROR,
                "turn_spawn_failed",
                chat=chat_id,
                update=update.update_id,
                session=session_id,
                error=error,
            )
            await self._telegram.send_message(
                chat_id, f"turn failed to start: {error}", reply_to=update.message_id
            )
            return

        turn.attach(handle)
        result = await handle.result()

        if turn.stop_requested:
            # An intentional abort is not a failure and its partial output is
            # not an answer: stay silent, keep the queue.
            log_event(
                self._logger,
                logging.INFO,
                "turn_stopped",
                chat=chat_id,
                update=update.update_id,
                session=session_id,
            )
            return

        if _failed(result):
            log_event(
                self._logger,
                logging.ERROR,
                "turn_failed",
                chat=chat_id,
                update=update.update_id,
                session=session_id,
                exit=result.exit_code,
                stop=result.stop_reason,
                stderr=result.error_summary,
            )
            answer = scrub(failure_notice(result), self._config.token)
            truncated = False
        elif result.stop_reason == "length":
            # A length stop still produced usable text, but it is incomplete
            # and must be labelled as such rather than passed off as final.
            partial = result.text.rstrip()
            warning = truncation_warning(result.stop_reason)
            answer = f"{partial}\n\n{warning}" if partial else warning
            truncated = True
        elif not result.text.strip():
            answer = "(no output)"
            truncated = False
        else:
            answer = result.text
            truncated = False

        await self._deliver(chat_id, update.message_id, answer)
        log_event(
            self._logger,
            logging.INFO,
            "turn_done",
            chat=chat_id,
            update=update.update_id,
            session=session_id,
            chars=len(answer),
            truncated=truncated,
        )

    async def _deliver(self, chat_id: int, reply_to: int | None, text: str) -> None:
        """Plain text up to the threshold, an exact `.md` document beyond it."""
        if len(text) <= MAX_MESSAGE_CHARS:
            await self._telegram.send_message(chat_id, text, reply_to=reply_to)
            return
        await self._telegram.send_document(
            chat_id,
            answer_filename(chat_id, self._clock.now()),
            text.encode("utf-8"),
            reply_to=reply_to,
        )


def build_bridge(
    config: Config,
    *,
    telegram: Telegram,
    pi: Pi,
    clock: Clock,
    logger: logging.Logger,
) -> Bridge:
    """The one composition path the service and the tests both use."""
    return Bridge(
        config=config,
        telegram=telegram,
        pi=pi,
        clock=clock,
        state=StateStore(config.state_path, logger=logger),
        logger=logger,
    )
