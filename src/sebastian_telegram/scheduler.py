"""Per-chat FIFO with one worker per chat, and the best-effort reaction rule.

This is where the "never two `pi` processes on one session" rule is structural:
every chat has exactly one worker task, and that worker runs one item at a time.
Chats have separate workers, so a long run in one chat never holds up another.

The scheduler owns no I/O beyond the injected `Telegram` port and holds the
queue in memory only; losing queued work on restart is accepted for v1.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from .log import log_event
from .secret import Secret, scrub
from .telegram import Telegram, Update

#: "you are waiting behind work", set when the message is enqueued.
HOURGLASS = "\N{HOURGLASS}"
#: "your message has been picked up", set when it starts (replacing ⏳).
EYES = "\N{EYES}"

#: Queued text is framed so the agent reads it as a correction, not a new topic.
QUEUED_PREFIX = "sent while you were working: "

RunMessage = Callable[[Update, str], Awaitable[None]]


def frame_prompt(text: str, *, queued: bool) -> str:
    """A direct message goes verbatim; a queued one is labelled."""
    if not queued:
        return text
    return f"({QUEUED_PREFIX}{text})"


@dataclass
class QueuedMessage:
    update: Update
    prompt: str
    queued: bool
    done: asyncio.Future[None]
    #: Set once the enqueue-time reaction has settled, so 👀 cannot overtake ⏳.
    marked: asyncio.Event = field(default_factory=asyncio.Event)


class _ChatWorker:
    """One FIFO and one worker task for one chat."""

    def __init__(self, chat_id: int, scheduler: "Scheduler") -> None:
        self.chat_id = chat_id
        self._scheduler = scheduler
        self._queue: asyncio.Queue[QueuedMessage] = asyncio.Queue()
        self.task: asyncio.Task[None] | None = None
        self.turn_running = False

    @property
    def busy(self) -> bool:
        """A turn is running, or earlier messages are still waiting."""
        return self.turn_running or not self._queue.empty()

    def put(self, item: QueuedMessage) -> None:
        self._queue.put_nowait(item)

    async def run(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                await self._handle(item)
            finally:
                self._queue.task_done()
                if not item.done.done():
                    item.done.set_result(None)
                # Exit when drained rather than idling; the next message starts
                # a fresh worker. No await separates the check from the return,
                # and `submit` enqueues before it looks for a worker, so an
                # item cannot be stranded between the two.
                if self._queue.empty():
                    self.task = None
                    return

    async def _handle(self, item: QueuedMessage) -> None:
        if item.queued:
            await item.marked.wait()
            await self._scheduler.react(item.update, EYES)
        self.turn_running = True
        try:
            await self._scheduler.run_message(item.update, item.prompt)
        except Exception as exc:  # one bad message must not wedge the chat
            self._scheduler.log_failure(item, exc)
        finally:
            self.turn_running = False


class Scheduler:
    """Marks, enqueues and serialises messages per chat."""

    def __init__(
        self,
        telegram: Telegram,
        logger: logging.Logger,
        run_message: RunMessage,
        *,
        secret: Secret | None = None,
    ) -> None:
        self._telegram = telegram
        self._logger = logger
        self.run_message = run_message
        self._secret = secret
        self._workers: dict[int, _ChatWorker] = {}

    async def submit(self, update: Update) -> None:
        """Enqueue the message and wait until its turn has been handled."""
        assert update.chat_id is not None and update.text is not None
        worker = self._worker_for(update.chat_id)

        # Decide and enqueue without awaiting in between: arrival order is
        # queue order, and "busy" is whatever the chat looked like on arrival.
        queued = worker.busy
        item = QueuedMessage(
            update=update,
            prompt=frame_prompt(update.text, queued=queued),
            queued=queued,
            done=asyncio.get_running_loop().create_future(),
        )
        worker.put(item)
        try:
            await self.react(update, HOURGLASS if queued else EYES)
        finally:
            item.marked.set()

        await item.done

    async def react(self, update: Update, emoji: str) -> None:
        """Best effort: a failed reaction is logged, never fatal."""
        if update.chat_id is None or update.message_id is None:
            return
        try:
            await self._telegram.set_reaction(update.chat_id, update.message_id, emoji)
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "reaction_failed",
                chat=update.chat_id,
                message=update.message_id,
                update=update.update_id,
                error=scrub(f"{type(exc).__name__}: {exc}", self._secret),
            )

    def log_failure(self, item: QueuedMessage, exc: Exception) -> None:
        log_event(
            self._logger,
            logging.ERROR,
            "update_failed",
            chat=item.update.chat_id,
            update=item.update.update_id,
            error=scrub(f"{type(exc).__name__}: {exc}", self._secret),
        )

    def _worker_for(self, chat_id: int) -> _ChatWorker:
        worker = self._workers.get(chat_id)
        if worker is None or worker.task is None or worker.task.done():
            worker = _ChatWorker(chat_id, self)
            self._workers[chat_id] = worker
            worker.task = asyncio.create_task(worker.run())
        return worker
