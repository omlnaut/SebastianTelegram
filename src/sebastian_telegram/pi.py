"""The pi turn port and its subprocess adapter.

The bridge never shells out itself: it asks a `Pi` for a turn handle and awaits
the result. The real adapter spawns one `pi --mode json -p` child per turn in
`~/vault`, in its own process group, and parses the JSONL event stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
from dataclasses import dataclass
from typing import Protocol

from .secret import Secret, scrub

_STDERR_TAIL_LIMIT = 8_000
_STREAM_CHUNK = 4_096


@dataclass(frozen=True)
class TurnResult:
    text: str
    stop_reason: str
    aborted: bool
    exit_code: int
    error_summary: str

    @classmethod
    def success(cls, text: str) -> "TurnResult":
        return cls(
            text=text,
            stop_reason="stop",
            aborted=False,
            exit_code=0,
            error_summary="",
        )


class TurnHandle(Protocol):
    pid: int
    process_group: int

    async def result(self) -> TurnResult: ...

    async def terminate(self, grace: float) -> None: ...


class Pi(Protocol):
    async def start(self, session_id: str, prompt: str) -> TurnHandle: ...


class SubprocessPi:
    """Spawns `pi --mode json -p --session-id <id> -- <prompt>`."""

    def __init__(
        self,
        pi_bin: str,
        cwd: os.PathLike[str] | str,
        *,
        secret: Secret | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self._pi_bin = pi_bin
        self._cwd = os.fspath(cwd)
        self._secret = secret
        self._env = dict(env or {})

    def argv(self, session_id: str, prompt: str) -> list[str]:
        # The `--` terminator is what keeps a prompt beginning with `-` text
        # rather than a CLI option.
        return [
            self._pi_bin,
            "--mode",
            "json",
            "-p",
            "--session-id",
            session_id,
            "--",
            prompt,
        ]

    async def start(self, session_id: str, prompt: str) -> TurnHandle:
        env = dict(os.environ)
        env.update(self._env)
        # The token is deliberately absent: it never enters argv or the child
        # environment.
        process = await asyncio.create_subprocess_exec(
            *self.argv(session_id, prompt),
            cwd=self._cwd,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        return SubprocessTurn(process, self._secret)


class SubprocessTurn:
    """Reads both pipes continuously and settles into one `TurnResult`."""

    def __init__(self, process: asyncio.subprocess.Process, secret: Secret | None) -> None:
        self._process = process
        self._secret = secret
        self.pid = process.pid
        self.process_group = process.pid
        self._text = ""
        self._stop_reason = "none"
        self._aborted = False
        self._terminated = False
        self._stderr = ""
        self._done: asyncio.Future[TurnResult] = asyncio.get_running_loop().create_future()
        self._stdout_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())
        self._waiter = asyncio.create_task(self._wait())

    async def result(self) -> TurnResult:
        return await asyncio.shield(self._done)

    async def terminate(self, grace: float) -> None:
        self._terminated = True
        if self._process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.process_group, signal.SIGTERM)
        try:
            await asyncio.wait_for(self._process.wait(), timeout=grace)
            return
        except asyncio.TimeoutError:
            pass
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.process_group, signal.SIGKILL)
        await self._process.wait()

    async def _read_stdout(self) -> None:
        assert self._process.stdout is not None
        while True:
            line = await self._process.stdout.readline()
            if not line:
                return
            self._handle_line(line.decode("utf-8", errors="replace").rstrip("\r\n"))

    async def _read_stderr(self) -> None:
        assert self._process.stderr is not None
        while True:
            chunk = await self._process.stderr.read(_STREAM_CHUNK)
            if not chunk:
                return
            self._stderr = (self._stderr + chunk.decode("utf-8", errors="replace"))[
                -_STDERR_TAIL_LIMIT:
            ]

    def _handle_line(self, line: str) -> None:
        if not line.strip():
            return
        try:
            event = json.loads(line)
        except ValueError:
            return
        if not isinstance(event, dict):
            return

        kind = event.get("type")
        if kind == "message_end":
            message = event.get("message") or {}
            if message.get("role") == "assistant":
                self._text = "\n".join(
                    block.get("text", "")
                    for block in message.get("content") or []
                    if isinstance(block, dict) and block.get("type") == "text"
                )
                self._stop_reason = message.get("stopReason") or "none"
        elif kind == "agent_settled":
            self._aborted = bool(event.get("aborted"))

    async def _wait(self) -> None:
        exit_code = await self._process.wait()
        await self._stdout_task
        await self._stderr_task
        aborted = self._terminated or self._aborted or self._stop_reason == "aborted"
        stop_reason = "aborted" if self._terminated and self._stop_reason == "none" else self._stop_reason
        self._done.set_result(
            TurnResult(
                text=self._text,
                stop_reason=stop_reason,
                aborted=aborted,
                exit_code=exit_code,
                error_summary=scrub(self._stderr.strip(), self._secret),
            )
        )
