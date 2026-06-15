"""In-memory context object shared during a single turn."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TurnContext:
    """Mutable context object passed through a single turn's lifecycle.

    Tools, hooks, and middleware can read and write to this object
    to share state during a single user turn without database I/O.
    """

    session_id: str
    turn_id: str
    data: dict[str, Any] = field(default_factory=dict)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def has(self, key: str) -> bool:
        return key in self.data

    def clear(self) -> None:
        self.data.clear()
