"""A secret value that refuses to render itself."""

from __future__ import annotations

REDACTED = "<redacted>"


class Secret:
    """Wraps a token so repr, str and formatting cannot leak it."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return REDACTED

    def __str__(self) -> str:
        return REDACTED

    def __format__(self, spec: str) -> str:
        return REDACTED

    def __len__(self) -> int:
        return len(self._value)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and other._value == self._value

    def __hash__(self) -> int:
        return hash(self._value)


def scrub(text: str, secret: Secret | None) -> str:
    """Remove the secret's value from text, including inside Bot API URLs."""
    if secret is None:
        return text
    value = secret.reveal()
    if not value:
        return text
    return text.replace(value, REDACTED)
