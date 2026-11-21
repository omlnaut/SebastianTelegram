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

## Running as a service

The repo ships its systemd unit and the environment file it reads, under
`deploy/`. See [`deploy/README.md`](deploy/README.md) for the deployment
contract, the paths the unit expects provisioning to create, and the on-host
acceptance checks.
