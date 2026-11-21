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

from .clock import Clock
from .log import log_event
from .pi import TurnHandle
from .secret import Secret, scrub_exception
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
    #: Resolves to the exception that escaped handling, or `None` when the
    #: update was fully handled. The submitter waits on it so a failure keeps
    #: the update unhandled instead of looking like a completed turn.
    done: asyncio.Future[Exception | None]
    #: Set once the enqueue-time reaction has settled, so 👀 cannot overtake ⏳.
    marked: asyncio.Event = field(default_factory=asyncio.Event)


class Turn:
    """The one turn a chat's worker is running right now.

    The bridge attaches the handle as soon as the spawn returns, so an
    out-of-band `/stop` can terminate the turn's process group. The flag is
    what lets the bridge tell a stop it asked for apart from an abort the
    agent reported by itself.

    `completed` records that the turn's result is in hand. A `/stop` that
    arrives afterwards has nothing left to terminate, so it must not suppress
    a finished answer or claim it stopped the turn.
    """

    def __init__(self, started_at: float | None = None) -> None:
        self.stop_requested = False
        #: The result is in hand: only delivery is left to do.
        self.completed = False
        #: Monotonic reading from the injected clock when the turn started.
        self.started_at = started_at
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
    done: asyncio.Future[Exception | None]


QueueItem = QueuedMessage | QueuedNewSession


@dataclass(frozen=True)
class ChatStatus:
    """What `/status` needs to know, read without waiting for a turn.

    `started_at` is a monotonic reading so elapsed time cannot jump with the
    wall clock; `queued` counts messages waiting behind the running turn, not
    control markers.
    """

    running: bool
    started_at: float | None
    queued: int


class _ChatWorker:
    """One FIFO and one worker task for one chat."""

    def __init__(self, chat_id: int, scheduler: "Scheduler") -> None:
        self.chat_id = chat_id
        self._scheduler = scheduler
        self._queue: asyncio.Queue[QueueItem] = asyncio.Queue()
        self.task: asyncio.Task[None] | None = None
        self.turn: Turn | None = None
        #: Messages waiting in the FIFO, excluding `/new` control markers.
        self.waiting_messages = 0

    @property
    def busy(self) -> bool:
        """A turn is running, or earlier messages are still waiting."""
        return self.turn is not None or not self._queue.empty()

    def put(self, item: QueueItem) -> None:
        if isinstance(item, QueuedMessage):
            self.waiting_messages += 1
        self._queue.put_nowait(item)

    async def run(self) -> None:
        while True:
            item = await self._queue.get()
            if isinstance(item, QueuedMessage):
                self.waiting_messages -= 1
            failure: Exception | None = None
            try:
                await self._handle(item)
            except Exception as exc:  # one bad item must not wedge the chat
                # Hand the failure back to the submitter, which leaves the
                # update unhandled; the worker goes on draining the queue.
                failure = exc
            finally:
                self._queue.task_done()
                if not item.done.done():
                    item.done.set_result(failure)
                # Exit when drained rather than idling; the next message starts
                # a fresh worker. No await separates the check from the return,
                # and `submit` enqueues before it looks for a worker, so an
                # item cannot be stranded between the two.
                if self._queue.empty():
                    self.task = None
                    return

    async def _handle(self, item: QueueItem) -> None:
        if isinstance(item, QueuedNewSession):
            await self._scheduler.start_new_session(item.update)
            return
        if item.queued:
            await item.marked.wait()
            await self._scheduler.react(item.update, EYES)
        turn = self._scheduler.begin_turn()
        self.turn = turn
        try:
            await self._scheduler.run_message(item.update, item.prompt, turn)
        finally:
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
        clock: Clock,
        secret: Secret | None = None,
    ) -> None:
        self._telegram = telegram
        self._logger = logger
        self._clock = clock
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

        failure = await item.done
        if failure is not None:
            raise failure

    async def stop(self, chat_id: int, grace: float = STOP_GRACE_SECONDS) -> bool:
        """Terminate the chat's running turn, keeping its queue.

        Returns False when the chat has no turn to stop, including a turn whose
        result is already in hand and is only being delivered. The queue is
        untouched on purpose: a correction typed during the run still runs next.
        """
        worker = self._workers.get(chat_id)
        turn = worker.turn if worker is not None else None
        if turn is None or turn.completed:
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
        failure = await item.done
        if failure is not None:
            raise failure

    def begin_turn(self) -> Turn:
        """The turn a chat's worker is about to run, stamped with the clock."""
        return Turn(started_at=self._clock.monotonic())

    def status(self, chat_id: int) -> ChatStatus:
        """Read the chat's live state; never waits for a turn to finish."""
        worker = self._workers.get(chat_id)
        if worker is None:
            return ChatStatus(running=False, started_at=None, queued=0)
        return ChatStatus(
            running=worker.turn is not None,
            started_at=worker.turn.started_at if worker.turn is not None else None,
            queued=worker.waiting_messages,
        )

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
                error=scrub_exception(exc, self._secret),
            )

    def _worker_for(self, chat_id: int) -> _ChatWorker:
        worker = self._workers.get(chat_id)
        if worker is None or worker.task is None or worker.task.done():
            worker = _ChatWorker(chat_id, self)
            self._workers[chat_id] = worker
            worker.task = asyncio.create_task(worker.run())
        return worker
