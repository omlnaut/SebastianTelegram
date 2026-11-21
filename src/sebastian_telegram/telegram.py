"""The Telegram port and a thin Bot API adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from .secret import Secret, scrub

_DEFAULT_TIMEOUT = 30.0


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
    """Owns HTTP, URL construction and token scrubbing. Nothing else."""

    def __init__(
        self,
        token: Secret,
        base_url: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient()

    async def get_updates(self, offset: int | None, timeout: int) -> list[Update]:
        payload: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": ["message"],
        }
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
        url = f"{self._base_url}/bot{self._token.reveal()}/{method}"
        try:
            response = await self._client.post(
                url, json=json, data=data, files=files, timeout=timeout or _DEFAULT_TIMEOUT
            )
        except httpx.HTTPError as exc:
            raise TelegramError(scrub(f"{method}: {type(exc).__name__}: {exc}", self._token)) from None

        try:
            body = response.json()
        except ValueError:
            raise TelegramError(
                scrub(f"{method}: non-JSON response {response.status_code}", self._token)
            ) from None

        if not isinstance(body, dict) or not body.get("ok"):
            description = body.get("description") if isinstance(body, dict) else None
            raise TelegramError(scrub(f"{method}: {description or 'request failed'}", self._token))

        return body.get("result")

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
