---
status: open
blocks:
  - "[[Decision - Telegram bridge shape]]"
  - "[[Task - Provision the agent runtime on the Pi]]"
type: task
claimed: ""
---

# Task - Build the Telegram bridge

## Question

Implement the shape settled in [[Decision - Telegram bridge shape]]. Scope is exactly the service, not its supervision and not its rules:

- [ ] Long polling against the Bot API with the token read from a `600` file (never argv, never a log line).
- [ ] Chat-ID allowlist; unknown chats are dropped silently.
- [ ] One queued worker per chat — **never two `pi` processes on one session ID** (see the fork evidence in the decision ticket).
- [ ] Spawn `pi` with `cwd=~/vault`, the settled session policy, and the settled progress mode.
- [ ] Return the final text; anything over the message limit goes as a `.md` attachment. Telegram Markdown is not CommonMark — escape or send plain.
- [ ] `/new`, `/stop` (SIGTERM to the process group), and `/status` (running? since when? queue depth?).
- [ ] Attach the bot's own journald tag so a failed run is traceable.

## Verification

- [ ] A message from an allowlisted chat starts a run and returns the answer; a message from any other chat does nothing.
- [ ] Two messages sent in quick succession run one after the other, and the session file has a single linear branch (no dead branch).
- [ ] `/stop` during a long run returns the chat to idle within a few seconds.
- [ ] A 6 000-character answer arrives as a file, not a truncated message.
- [ ] The token does not appear in `journalctl -u <bridge>`.
