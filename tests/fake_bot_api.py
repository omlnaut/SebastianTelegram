"""A local HTTP server that stands in for api.telegram.org.

The adapter takes its base URL from config, so pointing it at this server
proves retries, backoff and 429 handling without touching Telegram and without
stubbing httpx. Replies are scripted per Bot API method; when the script runs
out the server answers successfully.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections import deque
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Reply:
    status: int = 200
    body: dict | None = None
    headers: dict[str, str] = field(default_factory=dict)


#: Accept the connection, then close it without writing a byte: the client
#: sees a dropped connection mid-poll.
RESET = object()


@dataclass(frozen=True)
class Request:
    method: str
    body: dict | None
    raw: bytes


class FakeBotApi:
    """Minimal HTTP/1.1 server: one request per connection, then close."""

    def __init__(self) -> None:
        self.requests: list[Request] = []
        self._script: dict[str, deque[Reply | object]] = {}
        self._server: asyncio.AbstractServer | None = None
        self._port = 0


    # -- scripting ---------------------------------------------------------

    def script(self, method: str, *replies: Reply | object) -> "FakeBotApi":
        self._script.setdefault(method, deque()).extend(replies)
        return self

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    def calls(self, method: str) -> list[Request]:
        return [request for request in self.requests if request.method == method]

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> "FakeBotApi":
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self._port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()
        self._server = None

    async def __aenter__(self) -> "FakeBotApi":
        return await self.start()

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()

    # -- protocol ----------------------------------------------------------

    def _next_reply(self, method: str) -> Reply | object:
        queued = self._script.get(method)
        if queued:
            return queued.popleft()
        result: object = [] if method == "getUpdates" else {}
        return Reply(200, {"ok": True, "result": result})

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readline()
            if not head:
                return
            method = head.decode("latin-1").split(" ")[1].rsplit("/", 1)[-1]
            headers: dict[str, str] = {}
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                name, _, value = line.decode("latin-1").partition(":")
                headers[name.strip().lower()] = value.strip()

            length = int(headers.get("content-length") or 0)
            raw = await reader.readexactly(length) if length else b""
            self.requests.append(Request(method=method, body=_as_json(raw), raw=raw))

            reply = self._next_reply(method)
            if reply is RESET:
                return  # closing without a response is the reset we want
            assert isinstance(reply, Reply)
            payload = json.dumps(reply.body if reply.body is not None else {}).encode()
            reason = "OK" if reply.status == 200 else "Error"
            lines = [
                f"HTTP/1.1 {reply.status} {reason}",
                "Content-Type: application/json",
                f"Content-Length: {len(payload)}",
                "Connection: close",
            ]
            lines.extend(f"{name}: {value}" for name, value in reply.headers.items())
            writer.write("\r\n".join(lines).encode("latin-1") + b"\r\n\r\n" + payload)
            await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


def _as_json(raw: bytes) -> dict | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None
