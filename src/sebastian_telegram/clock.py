"""Clock port."""

from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float:
        """Wall-clock seconds, used for elapsed time and filenames."""

    def monotonic(self) -> float:
        """Monotonic seconds, used for durations that must not jump."""


class SystemClock:
    def now(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()
