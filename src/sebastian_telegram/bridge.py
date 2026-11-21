"""The bridge core: allowlist, one turn per message, one answer back.

It owns no I/O: Telegram, pi and the clock are injected ports, so every
behaviour here is observable at that seam.
"""

from __future__ import annotations

import asyncio
import logging

from .clock import Clock
from .config import Config
from .log import log_event
from .pi import Pi, TurnResult
from .secret import scrub
from .state import StateStore
from .telegram import Telegram, TelegramError, Update

_FAILED_STOP_REASONS = frozenset({"error", "aborted", "none"})
_NOTICE_TAIL_LIMIT = 300


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
        # Load eagerly so a missing or corrupt file is logged at startup.
        self._state.load()
        self._frontier = self._state.offset()
        self._unhandled: set[int] = set()
        # One worker per chat, enforced by a lock; the queue ticket extends
        # this into a FIFO without ever allowing a second process per chat.
        self._chat_locks: dict[int, asyncio.Lock] = {}

    async def run(self) -> None:
        frontier = self._state.offset()
        if frontier is None:
            frontier = await self._discard_backlog()
            self._set_frontier(frontier)
        self._frontier = frontier
        self._unhandled.clear()
        runtime_offset = frontier

        while True:
            try:
                updates = await self._telegram.get_updates(
                    runtime_offset, self._config.poll_timeout
                )
            except TelegramError as exc:
                log_event(self._logger, logging.ERROR, "poll_failed", error=str(exc))
                await asyncio.sleep(1)
                continue

            # Advance the runtime offset past the whole batch before doing any
            # work, so Telegram cannot re-deliver what is already in memory.
            for update in updates:
                if runtime_offset is None or update.update_id + 1 > runtime_offset:
                    runtime_offset = update.update_id + 1
                self._unhandled.add(update.update_id)

            for update in updates:
                try:
                    await self.handle_update(update)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # one bad update must not stop the bridge
                    log_event(
                        self._logger,
                        logging.ERROR,
                        "update_failed",
                        chat=update.chat_id,
                        update=update.update_id,
                        error=scrub(f"{type(exc).__name__}: {exc}", self._config.token),
                    )
                    continue

                self._unhandled.discard(update.update_id)
                self._set_frontier(self._next_frontier(runtime_offset))

    def _next_frontier(self, runtime_offset: int | None) -> int | None:
        # The frontier is the first update not yet fully handled, across all
        # chats; once nothing is outstanding it catches up to the runtime one.
        return min(self._unhandled) if self._unhandled else runtime_offset

    def _set_frontier(self, frontier: int | None) -> None:
        if frontier is None or frontier == self._frontier:
            return
        self._frontier = frontier
        self._state.set_offset(frontier)
        self._state.persist()

    async def _discard_backlog(self) -> int | None:
        """First start: drain and drop whatever Telegram still holds."""
        offset: int | None = None
        while True:
            updates = await self._telegram.get_updates(offset, 0)
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
        elif not result.text.strip():
            answer = "(no output)"
        else:
            answer = result.text

        await self._telegram.send_message(chat_id, answer, reply_to=update.message_id)
        log_event(
            self._logger,
            logging.INFO,
            "turn_done",
            chat=chat_id,
            update=update.update_id,
            session=session_id,
            chars=len(answer),
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
