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
from .pi import TurnHandle
from .secret import Secret, scrub
from .telegram import Telegram, Update

#: "you are waiting behind work", set when the message is enqueued.
HOURGLASS = "\N{HOURGLASS}"
#: "your message has been picked up", set when it starts (replacing ⏳).
EYES = "\N{EYES}"

#: Queued text is framed so the agent reads it as a correction, not a new topic.
QUEUED_PREFIX = "sent while you were working: "

#: How long `/stop` waits after SIGTERM before it has the group SIGKILLed.
STOP_GRACE_SECONDS = 5.0

RunMessage = Callable[[Update, str, "Turn"], Awaitable[None]]
StartNewSession = Callable[[Update], Awaitable[None]]


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


class Turn:
    """The one turn a chat's worker is running right now.

    The bridge attaches the handle as soon as the spawn returns, so an
    out-of-band `/stop` can terminate the turn's process group. The flag is
    what lets the bridge tell a stop it asked for apart from an abort the
    agent reported by itself.
    """

    def __init__(self) -> None:
        self.stop_requested = False
        self._handle: TurnHandle | None = None
        self._attached = asyncio.Event()

    def attach(self, handle: TurnHandle | None) -> None:
        """Record the handle, or `None` when the spawn itself failed."""
        self._handle = handle
        self._attached.set()

    async def handle(self) -> TurnHandle | None:
        """Wait for the spawn to settle; `/stop` can arrive first."""
        await self._attached.wait()
        return self._handle


@dataclass
class QueuedNewSession:
    """A `/new` control marker, applied wherever it lands in the FIFO."""

    update: Update
    done: asyncio.Future[None]


QueueItem = QueuedMessage | QueuedNewSession


class _ChatWorker:
    """One FIFO and one worker task for one chat."""

    def __init__(self, chat_id: int, scheduler: "Scheduler") -> None:
        self.chat_id = chat_id
        self._scheduler = scheduler
        self._queue: asyncio.Queue[QueueItem] = asyncio.Queue()
        self.task: asyncio.Task[None] | None = None
        self.turn_running = False
        self.turn: Turn | None = None

    @property
    def busy(self) -> bool:
        """A turn is running, or earlier messages are still waiting."""
        return self.turn_running or not self._queue.empty()

    def put(self, item: QueueItem) -> None:
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

    async def _handle(self, item: QueueItem) -> None:
        if isinstance(item, QueuedNewSession):
            try:
                await self._scheduler.start_new_session(item.update)
            except Exception as exc:  # one bad control must not wedge the chat
                self._scheduler.log_failure(item.update, exc)
            return
        if item.queued:
            await item.marked.wait()
            await self._scheduler.react(item.update, EYES)
        turn = Turn()
        self.turn = turn
        self.turn_running = True
        try:
            await self._scheduler.run_message(item.update, item.prompt, turn)
        except Exception as exc:  # one bad message must not wedge the chat
            self._scheduler.log_failure(item.update, exc)
        finally:
            self.turn_running = False
            self.turn = None


class Scheduler:
    """Marks, enqueues and serialises messages per chat."""

    def __init__(
        self,
        telegram: Telegram,
        logger: logging.Logger,
        run_message: RunMessage,
        start_new_session: StartNewSession,
        *,
        secret: Secret | None = None,
    ) -> None:
        self._telegram = telegram
        self._logger = logger
        self.run_message = run_message
        self.start_new_session = start_new_session
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

    async def stop(self, chat_id: int, grace: float = STOP_GRACE_SECONDS) -> bool:
        """Terminate the chat's running turn, keeping its queue.

        Returns False when the chat has no turn to stop. The queue is untouched
        on purpose: a correction typed during the run still runs next.
        """
        worker = self._workers.get(chat_id)
        turn = worker.turn if worker is not None else None
        if turn is None:
            return False
        turn.stop_requested = True
        handle = await turn.handle()
        if handle is not None:
            await handle.terminate(grace)
        return True

    async def submit_new(self, update: Update) -> None:
        """Enqueue `/new` as a control marker and wait until it is applied."""
        assert update.chat_id is not None
        worker = self._worker_for(update.chat_id)
        item = QueuedNewSession(
            update=update, done=asyncio.get_running_loop().create_future()
        )
        worker.put(item)
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

    def log_failure(self, update: Update, exc: Exception) -> None:
        log_event(
            self._logger,
            logging.ERROR,
            "update_failed",
            chat=update.chat_id,
            update=update.update_id,
            error=scrub(f"{type(exc).__name__}: {exc}", self._secret),
        )

    def _worker_for(self, chat_id: int) -> _ChatWorker:
        worker = self._workers.get(chat_id)
        if worker is None or worker.task is None or worker.task.done():
            worker = _ChatWorker(chat_id, self)
            self._workers[chat_id] = worker
            worker.task = asyncio.create_task(worker.run())
        return worker
