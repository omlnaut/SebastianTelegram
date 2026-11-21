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
        self.reactions.append((chat_id, message_id, emoji))


class FakeTurnHandle:
    def __init__(self, session_id: str, prompt: str, result: TurnResult) -> None:
        self.session_id = session_id
        self.prompt = prompt
        self._result = result
        self.pid = 0
        self.process_group = 0
        self.terminated = False

    async def result(self) -> TurnResult:
        return self._result

    async def terminate(self, grace: float) -> None:
        self.terminated = True


class FakePi:
    def __init__(self, results: list[TurnResult] | None = None) -> None:
        self.results = list(results or [])
        self.started: list[StartedTurn] = []
        self.handles: list[FakeTurnHandle] = []
        self.fail_next: list[Exception] = []

    async def start(self, session_id: str, prompt: str) -> FakeTurnHandle:
        if self.fail_next:
            raise self.fail_next.pop(0)
        self.started.append(StartedTurn(session_id=session_id, prompt=prompt))
        result = self.results.pop(0) if self.results else TurnResult.success("ok")
        handle = FakeTurnHandle(session_id, prompt, result)
        self.handles.append(handle)
        return handle


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
