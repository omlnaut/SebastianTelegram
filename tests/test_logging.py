import json
import logging
import stat

from sebastian_telegram.log import RedactTokenFilter, configure_logging
from sebastian_telegram.secret import Secret


def test_token_is_redacted_from_log_lines():
    buffer = __import__("io").StringIO()
    logger = configure_logging(Secret("123:abc"), stream=buffer)

    logger.info("event=http_error url=%s", "https://api.telegram.org/bot123:abc/sendMessage")

    output = buffer.getvalue()
    assert "123:abc" not in output
    assert "<redacted>" in output


def test_log_lines_are_key_value_on_one_line():
    buffer = __import__("io").StringIO()
    logger = configure_logging(Secret("123:abc"), stream=buffer)

    logger.info("event=turn_start chat=7 update=9 session=tg-7")

    line = buffer.getvalue().strip()
    assert "level=info" in line
    assert "event=turn_start" in line
    assert "chat=7" in line
    assert "\n" not in line


def test_filter_redacts_records_logged_through_any_handler():
    record = logging.LogRecord(
        "x", logging.INFO, __file__, 1, "token=%s", ("123:abc",), None
    )

    assert RedactTokenFilter(Secret("123:abc")).filter(record) is True
    assert "123:abc" not in record.getMessage()
    assert "<redacted>" in record.getMessage()


def test_secret_never_renders_its_value():
    secret = Secret("123:abc")

    assert "123:abc" not in repr(secret)
    assert "123:abc" not in str(secret)
    assert "123:abc" not in f"{secret}"
    assert "123:abc" not in "%s" % secret


def test_state_file_mode_is_private(tmp_path):
    from sebastian_telegram.state import State, StateStore

    path = tmp_path / "state.json"
    store = StateStore(path)
    store.save(State(offset=5, sessions={7: "tg-7"}))

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text()) == {"offset": 5, "sessions": {"7": "tg-7"}}
