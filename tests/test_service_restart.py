"""End-to-end proof of the restart contract, without handing it to systemd.

The unit declares `Restart=always` with `RestartSec=5`. This test reads those
values from the shipped unit, runs the real entrypoint against a local fake Bot
API and the fixture-replaying `pi` executable, SIGKILLs it, waits the declared
`RestartSec`, and asserts the bridge is live and answers the next message. It
never touches the real Telegram API and never starts a real `pi` run.

This is deliberately kept as the composition-path proof the spec asks for: it
starts `python -m sebastian_telegram`, so `cli()` -> `serve()` -> `build_service`
wire the real adapters from the environment exactly as the systemd unit does.
The failure half of that seam lives in `tests/test_startup.py`, which calls
`serve()`/`cli()` in-process for each misconfiguration.
"""

from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from .unitfile import ROOT, read_unit

FIXTURES = Path(__file__).parent / "fixtures"
FAKE_PI = Path(__file__).parent / "bin" / "fake_pi.py"
TOKEN = "123:abc"
ANSWER = "The answer is _this_ and `that`."
ACCEPTANCE_SECONDS = 10.0


class FakeBotApi:
    """A local stand-in for the Bot API: long-poll and record sends."""

    def __init__(self) -> None:
        self.answers: list[dict] = []
        self.update_calls = 0
        self._pending: list[dict] = []
        self._lock = threading.Lock()
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def add_message(self, chat_id: int, text: str, message_id: int) -> None:
        with self._lock:
            update_id = len(self._pending) + message_id
            self._pending.append(
                {
                    "update_id": update_id,
                    "message": {
                        "message_id": message_id,
                        "chat": {"id": chat_id},
                        "from": {"is_bot": False},
                        "text": text,
                    },
                }
            )

    def _handler(self):
        api = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server's spelling
                method = self.path.rsplit("/", 1)[-1]
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length) or b"{}"
                if method == "getUpdates":
                    offset = json.loads(raw).get("offset")
                    with api._lock:
                        api.update_calls += 1
                        pending = [
                            update
                            for update in api._pending
                            if offset is None or update["update_id"] >= offset
                        ]
                        api._pending = [u for u in api._pending if u not in pending]
                    # Keep the poll loop from spinning while idle.
                    if not pending:
                        time.sleep(0.02)
                    result = pending
                elif method == "sendMessage":
                    with api._lock:
                        api.answers.append(json.loads(raw))
                    result = {"message_id": 1}
                else:
                    result = True

                body = json.dumps({"ok": True, "result": result}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        return Handler


def wait_for(predicate, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


@pytest.fixture
def bot():
    api = FakeBotApi()
    api.start()
    try:
        yield api
    finally:
        api.stop()


def service_env(tmp_path: Path, base_url: str) -> dict[str, str]:
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN + "\n")
    token_file.chmod(0o600)
    (tmp_path / "vault").mkdir(exist_ok=True)
    env = dict(os.environ)
    env.update(
        {
            "PYTHONPATH": str(ROOT / "src"),
            "SEBASTIAN_TELEGRAM_TOKEN_FILE": str(token_file),
            "SEBASTIAN_TELEGRAM_ALLOWLIST": "7",
            "SEBASTIAN_TELEGRAM_STATE_DIR": str(tmp_path / "state"),
            "SEBASTIAN_TELEGRAM_VAULT_DIR": str(tmp_path / "vault"),
            "SEBASTIAN_TELEGRAM_PI_BIN": str(FAKE_PI),
            "SEBASTIAN_TELEGRAM_API_BASE_URL": base_url,
            "SEBASTIAN_TELEGRAM_POLL_TIMEOUT": "0",
            "FAKE_PI_FIXTURE": str(FIXTURES / "final_text.jsonl"),
        }
    )
    return env


def start_bridge(env: dict[str, str], log_path: Path):
    log = log_path.open("ab", buffering=0)
    process = subprocess.Popen(
        [sys.executable, "-m", "sebastian_telegram"],
        cwd=ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        start_new_session=True,
    )
    return process, log


def stop(process: subprocess.Popen, log) -> None:
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)
    log.close()


def test_a_sigkilled_bridge_is_back_within_ten_seconds_and_answers_the_next_message(
    tmp_path, bot
):
    env = service_env(tmp_path, bot.base_url)
    log_path = tmp_path / "bridge.log"
    restart_sec = float(read_unit()["Service"]["RestartSec"])
    assert restart_sec <= ACCEPTANCE_SECONDS

    # On first start the bridge drains and discards the backlog once. A
    # message sent to a live bridge is answered; one already waiting in the
    # backlog before startup is not. Wait for the first poll after that drain.
    first, first_log = start_bridge(env, log_path)
    try:
        wait_for(lambda: bot.update_calls >= 2, ACCEPTANCE_SECONDS, "the first poll")
        bot.add_message(chat_id=7, text="hello", message_id=41)
        wait_for(lambda: bot.answers, ACCEPTANCE_SECONDS, "the first answer")
        assert bot.answers[0]["text"] == ANSWER
        assert bot.answers[0]["reply_to_message_id"] == 41

        killed_at = time.monotonic()
        first.kill()
        first.wait(timeout=5)
    finally:
        if first.poll() is None:
            first.kill()
            first.wait(timeout=10)
        first_log.close()

    # What `Restart=always` + `RestartSec=5` does: wait, then start it again.
    time.sleep(restart_sec)
    baseline = bot.update_calls
    second, second_log = start_bridge(env, log_path)
    try:
        # The first start drains the backlog once, then serves. Two poll calls
        # mean the restarted bridge is past that drain and listening.
        wait_for(
            lambda: bot.update_calls >= baseline + 2,
            ACCEPTANCE_SECONDS,
            "the restarted bridge to poll",
        )
        bot.add_message(chat_id=7, text="after the crash", message_id=42)
        wait_for(
            lambda: len(bot.answers) >= 2,
            ACCEPTANCE_SECONDS,
            "an answer after the restart",
        )
    finally:
        stop(second, second_log)

    assert bot.answers[1]["text"] == ANSWER
    assert bot.answers[1]["reply_to_message_id"] == 42
    assert time.monotonic() - killed_at <= ACCEPTANCE_SECONDS

    log = log_path.read_text()
    assert TOKEN not in log
    assert "event=turn_start" in log
    assert "chat=7" in log
    assert "session=tg-7" in log
