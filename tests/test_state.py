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
