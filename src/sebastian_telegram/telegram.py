"""The Telegram port and a thin, retrying Bot API adapter.

The adapter owns HTTP, retries, 429 handling and token scrubbing. Everything
that could carry the token — request URLs above all, but exception text too —
is scrubbed before it becomes a log line or an exception message.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from .log import log_event
from .secret import Secret, scrub, scrub_exception

_DEFAULT_TIMEOUT = 30.0

#: Server-side failures are explicit rejections, so retrying them is safe.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
#: Repeating these calls is harmless, so any transport failure may be retried.
_IDEMPOTENT_METHODS = frozenset({"getUpdates", "setMessageReaction"})
#: Failures that provably happened before the request reached Telegram.
_NOT_DELIVERED = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)

_MAX_ATTEMPTS = 5
_BACKOFF_BASE = 0.5
_BACKOFF_CAP = 30.0

Sleeper = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class Update:
    """The only update shape the bridge core sees."""

    update_id: int
    chat_id: int | None
    message_id: int | None
    from_bot: bool
    text: str | None


class TelegramError(Exception):
    """A Bot API call failed; the message is already scrubbed."""


class Telegram(Protocol):
    async def get_updates(self, offset: int | None, timeout: int) -> list[Update]: ...

    async def send_message(self, chat_id: int, text: str, reply_to: int | None = None) -> None: ...

    async def send_document(
        self,
        chat_id: int,
        filename: str,
        data: bytes,
        caption: str | None = None,
        reply_to: int | None = None,
    ) -> None: ...

    async def set_reaction(self, chat_id: int, message_id: int, emoji: str) -> None: ...


class BotApiClient:
    """Owns HTTP, retries, URL construction and token scrubbing. Nothing else."""

    def __init__(
        self,
        token: Secret,
        base_url: str,
        *,
        client: httpx.AsyncClient | None = None,
        logger: logging.Logger | None = None,
        sleep: Sleeper | None = None,
        max_attempts: int = _MAX_ATTEMPTS,
        backoff_base: float = _BACKOFF_BASE,
        backoff_cap: float = _BACKOFF_CAP,
    ) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient()
        self._logger = logger
        self._sleep: Sleeper = sleep or asyncio.sleep
        self._max_attempts = max(1, max_attempts)
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap

    async def get_updates(self, offset: int | None, timeout: int) -> list[Update]:
        payload: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            payload["offset"] = offset
        result = await self._call(
            "getUpdates", json=payload, timeout=timeout + _DEFAULT_TIMEOUT
        )
        return [self._to_update(raw) for raw in result or []]

    async def send_message(self, chat_id: int, text: str, reply_to: int | None = None) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_to is not None:
            payload["reply_to_message_id"] = reply_to
        # No parse_mode: answers are plain text, punctuation intact.
        await self._call("sendMessage", json=payload)

    async def send_document(
        self,
        chat_id: int,
        filename: str,
        data: bytes,
        caption: str | None = None,
        reply_to: int | None = None,
    ) -> None:
        form: dict[str, Any] = {"chat_id": str(chat_id)}
        if caption is not None:
            form["caption"] = caption
        if reply_to is not None:
            form["reply_to_message_id"] = str(reply_to)
        await self._call(
            "sendDocument",
            data=form,
            files={"document": (filename, data, "text/markdown")},
        )

    async def set_reaction(self, chat_id: int, message_id: int, emoji: str) -> None:
        await self._call(
            "setMessageReaction",
            json={
                "chat_id": chat_id,
                "message_id": message_id,
                "reaction": [{"type": "emoji", "emoji": emoji}],
            },
        )

    async def _call(
        self,
        method: str,
        *,
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        url = self._url(method)
        repeatable = method in _IDEMPOTENT_METHODS
        attempt = 0

        while True:
            attempt += 1
            try:
                response = await self._client.post(
                    url, json=json, data=data, files=files, timeout=timeout or _DEFAULT_TIMEOUT
                )
            except httpx.TransportError as exc:
                # A connection that was never opened cannot have delivered the
                # request; anything else may have, so only repeatable calls
                # are retried then.
                delivered = not isinstance(exc, _NOT_DELIVERED)
                if (delivered and not repeatable) or attempt >= self._max_attempts:
                    detail = f"{method}: {scrub_exception(exc, self._token)}"
                    raise TelegramError(detail) from None
                await self._retry(method, attempt, self._backoff_delay(attempt), error=exc)
                continue

            body = _json_or_none(response)
            status = response.status_code

            if status in _RETRYABLE_STATUS:
                if attempt >= self._max_attempts:
                    raise TelegramError(
                        self._scrub(f"{method}: {_describe(body, status)} after {attempt} attempts")
                    ) from None
                delay = _retry_after(response, body) if status == 429 else None
                if delay is None:
                    delay = self._backoff_delay(attempt)
                await self._retry(method, attempt, delay, status=status)
                continue

            if not isinstance(body, dict) or not body.get("ok"):
                raise TelegramError(self._scrub(f"{method}: {_describe(body, status)}"))

            return body.get("result")

    async def _retry(
        self,
        method: str,
        attempt: int,
        delay: float,
        *,
        status: int | None = None,
        error: BaseException | None = None,
    ) -> None:
        fields: dict[str, object] = {"method": method, "attempt": attempt, "delay": delay}
        if status is not None:
            fields["status"] = status
        if error is not None:
            fields["error"] = scrub_exception(error, self._token)
        if self._logger is not None:
            log_event(self._logger, logging.WARNING, "http_retry", **fields)
        await self._sleep(delay)

    def _backoff_delay(self, attempt: int) -> float:
        return min(self._backoff_base * (2 ** (attempt - 1)), self._backoff_cap)

    def _url(self, method: str) -> str:
        return f"{self._base_url}/bot{self._token.reveal()}/{method}"

    def _scrub(self, text: str) -> str:
        return scrub(text, self._token)

    @staticmethod
    def _to_update(raw: dict[str, Any]) -> Update:
        update_id = int(raw.get("update_id", 0))
        message = raw.get("message")
        if not isinstance(message, dict):
            return Update(update_id, None, None, False, None)

        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        text = message.get("text")
        return Update(
            update_id=update_id,
            chat_id=chat.get("id"),
            message_id=message.get("message_id"),
            from_bot=bool(sender.get("is_bot")),
            text=text if isinstance(text, str) else None,
        )


def _json_or_none(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _describe(body: Any, status: int) -> str:
    if isinstance(body, dict):
        description = body.get("description")
        if isinstance(description, str) and description:
            return description
    return f"HTTP {status}"


def _retry_after(response: httpx.Response, body: Any) -> float | None:
    """Telegram's `retry_after`, falling back to the Retry-After header."""
    if isinstance(body, dict):
        parameters = body.get("parameters")
        if isinstance(parameters, dict):
            value = parameters.get("retry_after")
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                return float(value)
    header = response.headers.get("retry-after")
    if header is not None:
        try:
            parsed = float(header)
        except ValueError:
            return None
        return parsed if parsed >= 0 else None
    return None
