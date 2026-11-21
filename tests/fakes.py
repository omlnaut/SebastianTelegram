"""Test doubles for the three ports the bridge core talks to."""

from __future__ import annotations

import asyncio
import itertools
from collections import deque
from dataclasses import dataclass, field

from sebastian_telegram.pi import TurnResult
from sebastian_telegram.telegram import Update


@dataclass(frozen=True)
class SentMessage:
    chat_id: int
    text: str
    reply_to: int | None


@dataclass(frozen=True)
class StartedTurn:
    session_id: str
    prompt: str


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[SentMessage] = []
        self.documents: list[dict] = []
        self.reactions: list[tuple[int, int, str]] = []
        self.reaction_error: Exception | None = None
        self.pending_updates: deque[list[Update]] = deque()
        self.offsets: list[int | None] = []

    def queue_updates(self, updates: list[Update]) -> None:
        self.pending_updates.append(updates)

    async def get_updates(self, offset: int | None, timeout: int) -> list[Update]:
        self.offsets.append(offset)
        if self.pending_updates:
            return self.pending_updates.popleft()
        await asyncio.sleep(0)
        return []

    async def send_message(self, chat_id: int, text: str, reply_to: int | None = None) -> None:
        self.sent.append(SentMessage(chat_id=chat_id, text=text, reply_to=reply_to))

    async def send_document(
        self,
        chat_id: int,
        filename: str,
        data: bytes,
        caption: str | None = None,
        reply_to: int | None = None,
    ) -> None:
        self.documents.append(
            {
                "chat_id": chat_id,
                "filename": filename,
                "data": data,
                "caption": caption,
                "reply_to": reply_to,
            }
        )

    async def set_reaction(self, chat_id: int, message_id: int, emoji: str) -> None:
        # A real Bot API call yields; doing so here keeps interleavings honest.
        await asyncio.sleep(0)
        if self.reaction_error is not None:
            raise self.reaction_error
        self.reactions.append((chat_id, message_id, emoji))


class FakeTurnHandle:
    def __init__(
        self,
        session_id: str,
        prompt: str,
        result: TurnResult,
        *,
        gate: asyncio.Event | None = None,
        on_finish=None,
    ) -> None:
        self.session_id = session_id
        self.prompt = prompt
        self._result = result
        self.pid = 0
        self.process_group = 0
        self.terminated = False
        self.gate = gate
        self.finished = asyncio.Event()
        self._on_finish = on_finish

    async def result(self) -> TurnResult:
        if self.gate is not None:
            await self.gate.wait()
        if not self.finished.is_set():
            self.finished.set()
            if self._on_finish is not None:
                self._on_finish()
        return self._result

    async def terminate(self, grace: float) -> None:
        self.terminated = True
        if self.gate is not None:
            self.gate.set()


class FakePi:
    """A Pi port that can hold turns open and report overlap.

    A turn counts as active from `start()` until its handle's `result()`
    resolves, which is how a test sees whether two `pi` processes would have
    been running at once.
    """

    def __init__(self, results: list[TurnResult] | None = None) -> None:
        self.results = list(results or [])
        self.started: list[StartedTurn] = []
        self.handles: list[FakeTurnHandle] = []
        self.fail_next: list[Exception] = []
        self.active = 0
        self.max_active = 0
        self.max_active_by_session: dict[str, int] = {}
        self._active_by_session: dict[str, int] = {}
        self._hold = 0
        self._gates: list[asyncio.Event] = []

    def hold(self, count: int = 1) -> None:
        """Make the next `count` turns wait for `release()`."""
        self._hold += count

    def release(self, index: int = 0) -> None:
        self._gates[index].set()

    def release_all(self) -> None:
        for gate in self._gates:
            gate.set()

    async def wait_for_starts(self, count: int) -> None:
        while len(self.started) < count:
            await asyncio.sleep(0)

    async def start(self, session_id: str, prompt: str) -> FakeTurnHandle:
        if self.fail_next:
            raise self.fail_next.pop(0)
        self.started.append(StartedTurn(session_id=session_id, prompt=prompt))
        gate = None
        if self._hold:
            self._hold -= 1
            gate = asyncio.Event()
            self._gates.append(gate)
        result = self.results.pop(0) if self.results else TurnResult.success("ok")
        handle = FakeTurnHandle(
            session_id,
            prompt,
            result,
            gate=gate,
            on_finish=lambda: self._finish(session_id),
        )
        self._enter(session_id)
        self.handles.append(handle)
        return handle

    def _enter(self, session_id: str) -> None:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        count = self._active_by_session.get(session_id, 0) + 1
        self._active_by_session[session_id] = count
        self.max_active_by_session[session_id] = max(
            self.max_active_by_session.get(session_id, 0), count
        )

    def _finish(self, session_id: str) -> None:
        self.active -= 1
        self._active_by_session[session_id] -= 1


class FakeClock:
    def __init__(self) -> None:
        self.wall = 1_700_000_000.0
        self.mono = 1000.0

    def now(self) -> float:
        return self.wall

    def monotonic(self) -> float:
        return self.mono


_counter = itertools.count(1)


def update(
    *,
    chat_id: int = 7,
    message_id: int | None = None,
    text: str | None = "hello",
    from_bot: bool = False,
    update_id: int | None = None,
) -> Update:
    return Update(
        update_id=update_id if update_id is not None else next(_counter),
        chat_id=chat_id,
        message_id=message_id if message_id is not None else next(_counter),
        from_bot=from_bot,
        text=text,
    )
