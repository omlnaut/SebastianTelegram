# SebastianTelegram

Telegram bridge for the `pi` agent: long-polls the Bot API, ignores any chat
that is not on the allowlist, and turns each allowlisted text message into one
`pi --mode json -p --session-id tg-<chat_id> -- <text>` turn in `~/vault`,
replying with the final assistant text. Answers up to 3 500 characters arrive as
a plain-text reply; longer answers arrive as a `.md` document whose bytes are
exactly the UTF-8 of the final text, so nothing is split or truncated at
Telegram's 4 096-character message limit.

Each chat has one FIFO queue drained by a single worker, so a chat never has
two `pi` processes on its session and chats never block each other. A message
that arrives while a turn is running (or while earlier messages are waiting) is
marked ⏳ and, once it starts, 👀; it reaches the agent framed as
`(sent while you were working: <text>)` so it reads as a correction. A message
picked up immediately is marked 👀 and forwarded verbatim. Reaction calls are
best effort: a failure is logged and never blocks the turn. The queue is in
memory, so queued work is lost on restart.

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
## Running as a service

The repo ships its systemd unit and the environment file it reads, under
`deploy/`. See [`deploy/README.md`](deploy/README.md) for the deployment
contract, the paths the unit expects provisioning to create, and the on-host
acceptance checks.
