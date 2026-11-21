# SebastianTelegram

Telegram bridge for the `pi` agent: long-polls the Bot API, ignores any chat
that is not on the allowlist, and turns each allowlisted text message into one
`pi --mode json -p --session-id tg-<chat_id> -- <text>` turn in `~/vault`,
replying with the final assistant text as plain text.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `SEBASTIAN_TELEGRAM_TOKEN_FILE` | `$XDG_CONFIG_HOME/sebastian-telegram/token` | Bot token, mode `600` |
| `SEBASTIAN_TELEGRAM_ALLOWLIST` | required | Comma-separated chat IDs |
| `SEBASTIAN_TELEGRAM_STATE_DIR` | `$XDG_STATE_HOME/sebastian-telegram` | State directory |
| `SEBASTIAN_TELEGRAM_VAULT_DIR` | `~/vault` | `pi` working directory |
| `SEBASTIAN_TELEGRAM_PI_BIN` | `pi` | `pi` executable |
| `SEBASTIAN_TELEGRAM_POLL_TIMEOUT` | `30` | Long-poll timeout in seconds |
| `SEBASTIAN_TELEGRAM_API_BASE_URL` | `https://api.telegram.org` | Bot API base URL |

Startup validates the token file, its permissions, the allowlist and the state
directory; misconfiguration exits with a clear message instead of failing on
the first message.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
```

Tests drive the bridge core through fake `Telegram`, `Pi` and `Clock` ports and
never touch the real Telegram API or start a real `pi` run.

## Transport behaviour

Every Bot API call goes through one retrying path:

- Connection errors, timeouts, `5xx` and other transient network failures are
  retried with exponential backoff (0.5 s, 1 s, 2 s, 4 s; five attempts).
- A `429` is obeyed: the adapter waits Telegram's `retry_after` seconds before
  retrying instead of hammering the API.
- A poll that dies mid-flight is retried inside the adapter, and the poll loop
  resumes from its saved offset; a blip never restarts the service.
- Retries cannot answer a message twice: calls that are not safe to repeat
  (`sendMessage`, `sendDocument`) are only retried when the server explicitly
  rejected them or the connection was never opened. A request whose outcome is
  unknown (for example a read timeout) fails rather than being sent again.
- Request URLs carry the token, so URLs and exception text are scrubbed before
  they reach a log line or an error message, and a logging filter redacts the
  token as a last line of defence.

The base URL is configurable, so `tests/test_transport_robustness.py` points
the adapter at a local fake Bot API server and asserts the retry, backoff and
`429` sequences without touching Telegram.
