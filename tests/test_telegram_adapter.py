import httpx
import pytest

from sebastian_telegram.secret import Secret
from sebastian_telegram.telegram import BotApiClient, TelegramError

from .fakes import RecordingSleeper

TOKEN = "123:abc"


def client(handler):
    return BotApiClient(
        Secret(TOKEN),
        "https://api.telegram.org",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        sleep=RecordingSleeper(),
    )


async def test_get_updates_maps_messages_to_the_minimal_update_model():
    def handler(request):
        assert request.url.path.endswith("/getUpdates")
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    {
                        "update_id": 10,
                        "message": {
                            "message_id": 5,
                            "chat": {"id": -100},
                            "from": {"is_bot": False},
                            "text": "hello",
                        },
                    },
                    {
                        "update_id": 11,
                        "message": {
                            "message_id": 6,
                            "chat": {"id": 7},
                            "from": {"is_bot": True},
                            "text": "beep",
                        },
                    },
                    {
                        "update_id": 12,
                        "message": {
                            "message_id": 7,
                            "chat": {"id": 7},
                            "from": {"is_bot": False},
                            "photo": [{"file_id": "x"}],
                        },
                    },
                    {"update_id": 13, "edited_message": {"message_id": 8}},
                ],
            },
        )

    updates = await client(handler).get_updates(offset=10, timeout=30)

    assert [(u.update_id, u.chat_id, u.message_id, u.from_bot, u.text) for u in updates] == [
        (10, -100, 5, False, "hello"),
        (11, 7, 6, True, "beep"),
        (12, 7, 7, False, None),
        (13, None, None, False, None),
    ]


async def test_send_message_is_plain_text_and_replies_to_the_trigger():
    seen = {}

    def handler(request):
        seen["request"] = request
        return httpx.Response(200, json={"ok": True, "result": {}})

    await client(handler).send_message(7, "a_b `c`", reply_to=99)

    import json

    body = json.loads(seen["request"].content)
    assert seen["request"].url.path.endswith("/sendMessage")
    assert body == {"chat_id": 7, "text": "a_b `c`", "reply_to_message_id": 99}
    assert "parse_mode" not in body


async def test_transport_errors_never_leak_the_token_even_via_a_request_url():
    def handler(request):
        raise httpx.ConnectError(f"cannot connect to {request.url}")

    with pytest.raises(TelegramError) as caught:
        await client(handler).get_updates(offset=None, timeout=30)

    assert TOKEN not in str(caught.value)
    assert "123" in str(caught.value) or "<redacted>" in str(caught.value)


async def test_api_errors_are_scrubbed_too():
    def handler(request):
        return httpx.Response(
            401, json={"ok": False, "description": f"Unauthorized bot{TOKEN}"}
        )

    with pytest.raises(TelegramError) as caught:
        await client(handler).send_message(7, "hi")

    assert TOKEN not in str(caught.value)
