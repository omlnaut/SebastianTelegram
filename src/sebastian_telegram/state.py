"""Durable-by-intent state: the update offset and chat -> session mapping."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .log import log_event


@dataclass
class State:
    offset: int | None = None
    sessions: dict[int, str] = field(default_factory=dict)


class StateStore:
    """A small JSON file written atomically with mode 0600."""

    def __init__(self, path: Path, logger: logging.Logger | None = None) -> None:
        self._path = Path(path)
        self._logger = logger
        self._state = State()
        self._loaded = False

    def load(self) -> State:
        self._loaded = True
        try:
            raw = json.loads(self._path.read_text())
            offset = raw.get("offset")
            sessions = {
                int(chat_id): str(session_id)
                for chat_id, session_id in (raw.get("sessions") or {}).items()
            }
            self._state = State(
                offset=offset if isinstance(offset, int) else None,
                sessions=sessions,
            )
        except FileNotFoundError:
            self._recover("missing")
        except OSError:
            self._recover("unreadable")
        except (ValueError, TypeError, AttributeError):
            self._recover("corrupt")
        return self._state

    def _recover(self, reason: str) -> None:
        self._state = State()
        if self._logger is not None:
            log_event(
                self._logger,
                logging.WARNING,
                "state_reinitialised",
                path=self._path,
                reason=reason,
            )

    def save(self, state: State) -> None:
        self._state = state
        self._loaded = True
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = {
            "offset": state.offset,
            "sessions": {str(chat_id): session_id for chat_id, session_id in state.sessions.items()},
        }
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def session_for(self, chat_id: int) -> str:
        if not self._loaded:
            self.load()
        return self._state.sessions.get(chat_id) or f"tg-{chat_id}"

    def remember_session(self, chat_id: int, session_id: str) -> None:
        if not self._loaded:
            self.load()
        self._state.sessions[chat_id] = session_id

    def offset(self) -> int | None:
        if not self._loaded:
            self.load()
        return self._state.offset

    def set_offset(self, offset: int | None) -> None:
        if not self._loaded:
            self.load()
        self._state.offset = offset

    def persist(self) -> None:
        self.save(self._state)
