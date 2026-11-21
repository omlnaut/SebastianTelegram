---
status: closed
blocks: []
type: decision
claimed: "2026-11-01 agent-host-chart"
---

# Decision - Telegram bridge shape

## Question

A small always-on service turns Telegram messages into `pi` work and returns the result. The human's starting sketch was *"a small Python service that listens for incoming Telegram messages and either starts a new pi session or redirects to an already running one"*. This ticket settles the shape before anything is built.

**Facts that constrain it** (tested 2026-11-01 on the WSL host, `pi` 1.4.0):

- `pi -p` runs one prompt and exits with only the final text; `--mode json` streams events for progress; `--mode rpc` keeps a process alive and accepts further commands; the TS SDK embeds the agent but is Node-only.
- `--session-id <id>` reuses a named session and creates it if missing. Sessions are scoped to `cwd`, so the bridge must run from `~/vault`.
- **"Redirect to an already running session" is not possible with a second process.** No lock exists: a second `pi -p --session-id X` started mid-run appended its turn off the in-flight assistant message while the first process wrote its tool result to a different branch — two leaves, one dead branch, both exits 0. Serializing per chat is therefore not optional.
- Long polling (`getUpdates`) needs no inbound port and no public hostname.

**Open questions to settle (grill, 2026-11-01):**

1. **Process model** — Python service spawning `pi --mode json -p` per turn (isolation, cwd-based resources, crash containment) vs a long-lived `pi --mode rpc` child (mid-run steering, one process to supervise) vs Node + SDK (no process boundary, no crash isolation).
2. **Session topology** — one session per chat forever with `/new`, vs one session per task, vs per-topic. The vault is the durable memory; the session is just the working context.
3. **A message arriving mid-run** — queue it as the next turn, steer the running turn (rpc only), or kill and restart with the new text.
4. **Progress UX** — one status message edited from the JSON event stream, final answer replacing it, long output as a `.md` attachment?
5. **Where the code lives** — new small repo, or a directory in `SebastianSkills` so one clone + one `setup.sh` provisions the Pi.

## Grilling

**Q1 — Process model: settled 2026-11-01.** Python bridge, **one `pi` subprocess per turn**: `pi --mode json --session-id tg-<chat> "<prompt>"` with `cwd=~/vault`. The session file is the state carrier, so a session survives the process, a reboot and a bot restart; the agent asks a question by ending its turn with a question, and the human's next message starts a new process on the same session. Accepted costs: ~1–2 s startup per message, and no touching a run in progress — a mid-run message is queued or `/stop`ped, never injected.
**Long-term option, deliberately deferred**: one live `pi --mode rpc` child per chat buys in-place steering, an `abort` that keeps the session coherent, and no startup cost — at the price of speaking pi's JSONL command protocol and babysitting a long-lived node process. Revisit only if the queued-corrections workflow chafes.

**Q2 — Session topology: settled 2026-11-01.** **One session per chat** (`--session-id tg-<chatid>`), same transcript every message, **`/new` is the only reset** — deliberately no idle-based auto-reset, because the vault (not the chat) carries state between days and a magic reset breaks "continue with that thing". Consequence: the transcript grows without bound and pi's compaction summarises old turns lossily; `/new` is the human's answer when it gets unwieldy. If topic-scoped memory is wanted later, it is a change in the bridge's `chat_id → session_id` rule, not a rewrite.

**Q3 — mid-run message: settled 2026-11-01.** Queued FIFO per chat, never a second process. The queued message gets a ⏳ reaction, an immediately picked-up one 👀 — with no status line (Q4), the reaction is the only queue signal. `/stop` SIGTERMs the running process group and **keeps** the queue, so the correction runs next. The queued text is delivered as `(sent while you were working: …)`.

**Q4 — progress: settled the other way (2026-11-01).** No edited status message: the bot stays silent until the answer is ready, then posts it; answers over ~3 500 characters go as a `.md` document because of Telegram's 4 096 limit. Note recorded for the build: progress edits cost **no** model tokens — the bridge already receives the JSON event stream and an edit is one HTTP call to Telegram; the only real cost of a status line is chat noise, which the human does not want.

**Q5 — code home: settled 2026-11-01.** New repo `omlnaut/clawd-bridge` (Python, systemd unit included); `setup.sh` gains a clone-and-install step so the Pi is still provisioned by one script.


## Resolution

Settled 2026-11-01 — the build is a **Python service, one `pi --mode json` subprocess per turn**, `cwd=~/vault`, `--session-id tg-<chatid>`, one session per chat with `/new` as the only reset, mid-run messages queued FIFO (never a second process) with `/stop` keeping the queue, **no progress messages** (final answer only; >3 500 characters as a `.md` document), code in a new repo `omlnaut/clawd-bridge` with its systemd unit and a `setup.sh` install step. Detail and the rejected alternatives are under `## Grilling`; RPC is the documented upgrade path, not v1.

What this ticket deliberately does **not** settle: what the bridge's prompt prefix says to the agent, and whether `/status` reports the queue or the session id — implementation detail for [[Task - Build the Telegram bridge]].
