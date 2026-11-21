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
from .secret import scrub
from .state import StateStore
from .telegram import Telegram, TelegramError, Update

_FAILED_STOP_REASONS = frozenset({"error", "aborted", "none"})
_NOTICE_TAIL_LIMIT = 300
#: How long the poll loop waits after the adapter has given up on a poll.
_POLL_RETRY_DELAY = 1.0

# Plain-text Bot API messages stop at 4 096 characters; 3 500 leaves room for
# the reply and matches the settled decision.
MAX_MESSAGE_CHARS = 3_500


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
        # One worker per chat, enforced by a lock; the queue ticket extends
        # this into a FIFO without ever allowing a second process per chat.
        self._chat_locks: dict[int, asyncio.Lock] = {}

    async def run(self) -> None:
        offset = self._state.offset()
        if offset is None:
            offset = await self._discard_backlog()
        while True:
            updates = await self._poll(offset, self._config.poll_timeout)
            for update in updates:
                offset = update.update_id + 1
                try:
                    await self.handle_update(update)
                except Exception as exc:  # one bad update must not stop the bridge
                    log_event(
                        self._logger,
                        logging.ERROR,
                        "update_failed",
                        chat=update.chat_id,
                        update=update.update_id,
                        error=scrub(f"{type(exc).__name__}: {exc}", self._config.token),
                    )

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
        assert update.chat_id is not None
        lock = self._chat_locks.setdefault(update.chat_id, asyncio.Lock())
        async with lock:
            await self._run_turn(update)

    def _is_for_us(self, update: Update) -> bool:
        if update.chat_id is None or update.from_bot:
            return False
        if update.chat_id not in self._config.allowlist:
            return False
        return bool(update.text and update.text.strip())

    async def _run_turn(self, update: Update) -> None:
        assert update.chat_id is not None and update.text is not None
        chat_id = update.chat_id
        session_id = self._state.session_for(chat_id)
        log_event(
            self._logger,
            logging.INFO,
            "turn_start",
            chat=chat_id,
            update=update.update_id,
            session=session_id,
        )

        try:
            handle = await self._pi.start(session_id, update.text)
            result = await handle.result()
        except OSError as exc:
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
        state=StateStore(config.state_path),
        logger=logger,
    )
