# SebastianTelegram

Telegram bridge for the `pi` agent: long-polls the Bot API, ignores any chat
that is not on the allowlist, and turns each allowlisted text message into one
`pi --mode json -p --session-id tg-<chat_id> -- <text>` turn in `~/vault`,
replying with the final assistant text as plain text.

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
