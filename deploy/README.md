# Deployment artifacts

This directory ships the bridge's supervision, not the host provisioning. The
runtime-provisioning ticket installs `pi`, authenticates providers, clones the
vault and enables the unit; this ticket ships the unit, the environment file it
reads, and the proof that the service comes back on its own.

## What ships

| File | Installed as | Purpose |
|---|---|---|
| `systemd/sebastian-telegram.service` | `/etc/systemd/system/sebastian-telegram.service` | system unit, `User=pi` |
| `systemd/sebastian-telegram.env` | `/etc/sebastian-telegram/env` | token path, allowlist, paths |
| `acceptance.sh` | — | the on-host acceptance checks from #10 |

The unit is a **system** unit, not a user unit: the bridge has to survive
reboots and logins/logouts, and a user unit would only exist while a session
for `pi` does. It runs as the non-root user `pi` with `HOME=/home/pi`, so
`~/vault` resolves to `/home/pi/vault`.

`ExecStart` names the console script `/home/pi/.local/bin/sebastian-telegram`
directly, with no shell and no wrapper, so the process `systemctl status` shows
is the entrypoint itself. Provisioning must place it there (for example with a
`pip install --user` of this package as `pi`, which is what produces that
console script; `pyproject.toml` declares it as
`sebastian-telegram = "sebastian_telegram.main:cli"`).

The environment file carries no secret. The bot token lives in its own file,
mode `0600`, named by `SEBASTIAN_TELEGRAM_TOKEN_FILE`; startup refuses a token
file with group or other permission bits. The allowlist ships as `0`, a valid
chat ID that matches no chat, so a freshly installed bridge answers nobody
until the operator sets their own chat ID. The polling and base-URL overrides
are commented out in the shipped file because their defaults are correct for
the host.

`Restart=always` with `RestartSec=5` and `StartLimitIntervalSec=0` means a
crashed or killed bridge is started again five seconds later, forever.
`SyslogIdentifier=sebastian-telegram` gives every line in the journal a stable
identifier, while the bridge's own lines are `key=value` and never include
message text or the token.

## Installing (belongs to the runtime-provisioning ticket)

```bash
install -Dm644 deploy/systemd/sebastian-telegram.service /etc/systemd/system/sebastian-telegram.service
install -Dm644 deploy/systemd/sebastian-telegram.env     /etc/sebastian-telegram/env
# then edit /etc/sebastian-telegram/env: set SEBASTIAN_TELEGRAM_ALLOWLIST
systemctl daemon-reload
systemctl enable --now sebastian-telegram.service
```

## Acceptance

On the host, with the unit running and journal access:

```bash
sudo deploy/acceptance.sh
```

The script checks that the main process is the entrypoint rather than a shell,
that `kill -9` is followed by a new live process within ten seconds, that the
journal holds the bridge's own `event=` lines under the unit's identifier and
no token, and that `systemctl show` reports `User=pi`, `HOME=/home/pi` and
`WorkingDirectory=/home/pi`. The last check is interactive: the operator sends
a Telegram message immediately after the restart and confirms the answer,
because only the human can author the inbound message.

The same restart guarantee is proven without systemd in the test suite:
`tests/test_service_restart.py` reads `RestartSec` from the shipped unit, runs
the real entrypoint against a local fake Bot API and the fixture-replaying `pi`
executable, SIGKILLs it, waits that interval and asserts the next message is
answered and that the log lines carry the chat, session and no token.
