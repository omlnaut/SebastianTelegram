from sebastian_telegram.state import State, StateStore


def test_missing_state_file_is_a_fresh_state(tmp_path):
    store = StateStore(tmp_path / "state.json")

    state = store.load()

    assert state.offset is None
    assert state.sessions == {}


def test_state_round_trips_offset_and_sessions(tmp_path):
    store = StateStore(tmp_path / "state.json")
    store.save(State(offset=42, sessions={7: "tg-7", -100: "tg--100"}))

    loaded = StateStore(tmp_path / "state.json").load()

    assert loaded.offset == 42
    assert loaded.sessions == {7: "tg-7", -100: "tg--100"}


def test_corrupt_state_file_is_reinitialised_not_fatal(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json")

    state = StateStore(path).load()

    assert state.offset is None
    assert state.sessions == {}


def test_session_id_defaults_to_the_chat_id_and_keeps_the_sign(tmp_path):
    store = StateStore(tmp_path / "state.json")

    assert store.session_for(7) == "tg-7"
    assert store.session_for(-100123) == "tg--100123"


def test_a_persisted_session_wins_over_the_default(tmp_path):
    path = tmp_path / "state.json"
    StateStore(path).save(State(offset=1, sessions={7: "tg-7-2"}))

    assert StateStore(path).session_for(7) == "tg-7-2"


def test_missing_state_file_is_logged_and_starts_clean(tmp_path):
    from io import StringIO

    from sebastian_telegram.log import configure_logging
    from sebastian_telegram.secret import Secret

    buffer = StringIO()
    logger = configure_logging(Secret("123:abc"), stream=buffer)

    state = StateStore(tmp_path / "state.json", logger=logger).load()

    assert state.offset is None
    assert state.sessions == {}
    output = buffer.getvalue()
    assert "event=state_reinitialised" in output
    assert "reason=missing" in output


def test_a_truncated_state_file_starts_cleanly_with_a_log(tmp_path):
    from io import StringIO

    from sebastian_telegram.log import configure_logging
    from sebastian_telegram.secret import Secret

    path = tmp_path / "state.json"
    path.write_text('{"offset": 4, "sessions": {"7": "tg-7')

    buffer = StringIO()
    logger = configure_logging(Secret("123:abc"), stream=buffer)
    state = StateStore(path, logger=logger).load()

    assert state.offset is None
    assert state.sessions == {}
    output = buffer.getvalue()
    assert "event=state_reinitialised" in output
    assert "reason=corrupt" in output


def test_a_leftover_half_written_temp_file_does_not_stop_startup(tmp_path):
    path = tmp_path / "state.json"
    StateStore(path).save(State(offset=7, sessions={7: "tg-7"}))
    (tmp_path / ".state-crashed.tmp").write_text('{"offset": 9, "sess')

    loaded = StateStore(path).load()

    assert loaded.offset == 7
    assert loaded.sessions == {7: "tg-7"}
