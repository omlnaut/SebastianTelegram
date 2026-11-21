"""Transport robustness: retries, backoff, 429 and token scrubbing.

Everything here runs against a local fake Bot API server reached through the
configurable base URL, so no test touches Telegram.
"""

from __future__ import annotations

import io

import httpx
import pytest

from sebastian_telegram.log import configure_logging
from sebastian_telegram.secret import Secret
from sebastian_telegram.telegram import BotApiClient, TelegramError

from .fake_bot_api import RESET, FakeBotApi, Reply
from .fakes import RecordingSleeper

TOKEN = "123:abc-def_TELEGRAM_SECRET"
MESSAGE = {"chat_id": 7, "text": "hello", "reply_to_message_id": 99}
OK = Reply(200, {"ok": True, "result": {}})


def server_error(description: str = "Internal Server Error") -> Reply:
    return Reply(500, {"ok": False, "error_code": 500, "description": description})


def too_many_requests(retry_after: int) -> Reply:
    return Reply(
        429,
        {
            "ok": False,
            "error_code": 429,
            "description": f"Too Many Requests: retry after {retry_after}",
            "parameters": {"retry_after": retry_after},
        },
    )


def make_client(base_url: str, *, sleeper=None, **kwargs):
    """The adapter with a captured log stream and a sleeper that records delays."""
    buffer = io.StringIO()
    logger = configure_logging(Secret(TOKEN), stream=buffer)
    sleeper = sleeper if sleeper is not None else RecordingSleeper()
    client = BotApiClient(Secret(TOKEN), base_url, logger=logger, sleep=sleeper, **kwargs)
    return client, sleeper, buffer


@pytest.fixture
async def bot_api():
    server = await FakeBotApi().start()
    try:
        yield server
    finally:
        await server.stop()


async def test_a_500_twice_then_200_sends_exactly_one_message_and_logs_the_retries(bot_api):
    bot_api.script("sendMessage", server_error(), server_error(), OK)
    client, sleeps, logs = make_client(bot_api.base_url)

    await client.send_message(7, "hello", reply_to=99)

    calls = bot_api.calls("sendMessage")
    assert len(calls) == 3
    assert all(call.body == MESSAGE for call in calls), "the retry repeats the same request"
    assert logs.getvalue().count("event=http_retry") == 2
    assert len(sleeps.delays) == 2
    assert 0 < sleeps.delays[0] < sleeps.delays[1], "retries back off"


async def test_a_429_carrying_retry_after_three_waits_three_seconds_first(bot_api):
    bot_api.script("sendMessage", too_many_requests(3), OK)
    client, sleeps, logs = make_client(bot_api.base_url)

    await client.send_message(7, "hello", reply_to=99)

    assert len(bot_api.calls("sendMessage")) == 2
    assert sleeps.delays == [3.0], "retry_after wins over backoff"
    assert "status=429" in logs.getvalue()


async def test_a_connection_reset_mid_poll_resumes_polling(bot_api):
    bot_api.script(
        "getUpdates",
        RESET,
        Reply(200, {"ok": True, "result": [{"update_id": 1, "message": {"message_id": 2,
            "chat": {"id": 7}, "from": {"is_bot": False}, "text": "after the blip"}}]}),
    )
    client, sleeps, _ = make_client(bot_api.base_url)

    updates = await client.get_updates(offset=None, timeout=30)

    assert [update.text for update in updates] == ["after the blip"]
    assert len(bot_api.calls("getUpdates")) == 2
    assert len(sleeps.delays) == 1


async def test_an_ambiguous_failure_never_sends_the_same_message_twice():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        # The request reached the far side; the answer never came back.
        raise httpx.ReadTimeout("timed out waiting for a response", request=request)

    client = BotApiClient(
        Secret(TOKEN),
        "https://api.telegram.org",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        sleep=(sleeper := RecordingSleeper()),
    )

    with pytest.raises(TelegramError):
        await client.send_message(7, "hello", reply_to=99)

    assert len(seen) == 1, "a lost response is not safely repeatable"
    assert sleeper.delays == []


async def test_giving_up_on_a_server_error_still_names_no_token(bot_api):
    bot_api.script("sendMessage", *[server_error() for _ in range(5)])
    client, _, logs = make_client(bot_api.base_url, max_attempts=2)

    with pytest.raises(TelegramError) as caught:
        await client.send_message(7, "hello", reply_to=99)

    assert len(bot_api.calls("sendMessage")) == 2
    assert TOKEN not in str(caught.value)
    assert TOKEN not in logs.getvalue()


async def test_an_error_whose_message_embeds_the_request_url_is_logged_scrubbed():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        raise httpx.ConnectError(f"cannot connect to {request.url}")

    client = BotApiClient(
        Secret(TOKEN),
        "https://api.telegram.org",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        logger=configure_logging(Secret(TOKEN), stream=(buffer := io.StringIO())),
        sleep=(sleeper := RecordingSleeper()),
        max_attempts=2,
    )

    with pytest.raises(TelegramError) as caught:
        await client.get_updates(offset=None, timeout=30)

    logged = buffer.getvalue()
    assert TOKEN in str(seen[0].url), "the URL really does carry the token"
    assert TOKEN not in logged
    assert TOKEN not in str(caught.value)
    assert len(sleeper.delays) == 1
