#!/usr/bin/env bash
# Acceptance checks for the shipped systemd unit (issue #10).
#
# Run on the host after the unit is installed and enabled:
#
#     sudo deploy/acceptance.sh
#
# Checks 1-4 are automated. Check 5 is interactive because only the human can
# send the inbound Telegram message; the script waits for the operator to
# confirm the answer arrived.
#
# The token is read from its file only to prove it is absent from the journal.
# It is never printed and never passed in an argument.
set -euo pipefail

UNIT=${UNIT:-sebastian-telegram.service}
ENV_FILE=${ENV_FILE:-/etc/sebastian-telegram/env}
MAX_RESTART_WAIT=${MAX_RESTART_WAIT:-10}

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
pass() { printf 'ok:   %s\n' "$*"; }

command -v systemctl >/dev/null 2>&1 || fail "systemctl not found; run this on the host"
systemctl is-active --quiet "$UNIT" || fail "$UNIT is not running"

# 1. The main process is the real entrypoint, not a shell wrapper.
main_pid=$(systemctl show -p MainPID --value "$UNIT")
[[ $main_pid =~ ^[1-9][0-9]*$ ]] || fail "$UNIT has no main process"
comm=$(ps -o comm= -p "$main_pid" | tr -d ' ')
case "$comm" in
  sh | bash | dash | zsh) fail "main process is a shell ($comm), not the entrypoint" ;;
esac
exe=$(readlink -f "/proc/$main_pid/exe")
pass "main process $main_pid is '$comm' ($exe), not a shell"

# 4. Real HOME, vault-capable working directory and non-root user.
[[ $(systemctl show -p User --value "$UNIT") == pi ]] \
  || fail "User is not pi: $(systemctl show -p User --value "$UNIT")"
[[ $(systemctl show -p WorkingDirectory --value "$UNIT") == /home/pi ]] \
  || fail "WorkingDirectory is not /home/pi: $(systemctl show -p WorkingDirectory --value "$UNIT")"
environment=$(systemctl show -p Environment --value "$UNIT")
grep -q 'HOME=/home/pi' <<<"$environment" \
  || fail "Environment does not set HOME=/home/pi: $environment"
pass "User=pi, WorkingDirectory=/home/pi, Environment contains HOME=/home/pi"

# 3. The bridge's own lines reach the journal under the unit, without a token.
sleep 1
journal=$(journalctl -u "$UNIT" --no-pager -n 500 -o cat || true)
grep -q 'event=' <<<"$journal" || fail "no bridge event= lines in the journal for $UNIT"
token_file=$(sed -n 's/^SEBASTIAN_TELEGRAM_TOKEN_FILE=//p' "$ENV_FILE" | head -1)
if [[ -n $token_file && -r $token_file ]]; then
  token=$(<"$token_file")
  if [[ -n $token ]] && grep -qF -- "$token" <<<"$journal"; then
    fail "the bot token appears in the journal"
  fi
fi
pass "journal holds bridge event= lines for $UNIT and no token"

# 2. kill -9 is followed by a live bridge within the wait budget.
before=$main_pid
kill -9 "$before" || fail "could not SIGKILL main process $before"
deadline=$((SECONDS + MAX_RESTART_WAIT))
after=0
while ((SECONDS < deadline)); do
  after=$(systemctl show -p MainPID --value "$UNIT")
  if [[ $after =~ ^[1-9][0-9]*$ && $after != "$before" ]] && systemctl is-active --quiet "$UNIT"; then
    break
  fi
  sleep 0.2
done
[[ $after =~ ^[1-9][0-9]*$ && $after != "$before" ]] \
  || fail "$UNIT did not come back within ${MAX_RESTART_WAIT}s"
pass "$UNIT came back with PID $after within ${MAX_RESTART_WAIT}s of kill -9"

# 5. A message sent immediately after the restart is answered. Only the human
#    can author that inbound message, so this one asks.
cat <<'EOF'

Manual check: send a message to the allowlisted chat now. The restarted bridge
should answer it. Follow the log in another terminal if you like:

    journalctl -u sebastian-telegram -f

EOF
read -r -p "Press Enter once the answer arrived (Ctrl-C if it did not): " _
pass "a message sent immediately after the restart was answered"
