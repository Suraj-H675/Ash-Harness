"""Semantic transcript state shared by Ash's interactive terminal surfaces."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, Literal
from uuid import uuid4


TranscriptKind = Literal[
    "user",
    "assistant",
    "reasoning",
    "tool",
    "approval",
    "status",
    "error",
]
TranscriptAction = Literal["added", "updated", "finalized", "reset"]


@dataclass(frozen=True)
class TranscriptEntry:
    entry_id: str
    kind: TranscriptKind
    content: str
    title: str = ""
    finalized: bool = True
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class TranscriptEvent:
    revision: int
    action: TranscriptAction
    entry: TranscriptEntry | None = None


TranscriptListener = Callable[[TranscriptEvent], None]


class Transcript:
    """Bounded committed history with mutable in-flight entries.

    Entries are immutable snapshots. Streaming updates replace an entry instead
    of mutating an object already held by a renderer, so redraw consumers can
    safely retain prior snapshots.
    """

    def __init__(self, *, max_entries: int = 1000, max_characters: int = 2_000_000):
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        if max_characters < 1:
            raise ValueError("max_characters must be positive")
        self.max_entries = max_entries
        self.max_characters = max_characters
        self._entries: list[TranscriptEntry] = []
        self._listeners: set[TranscriptListener] = set()
        self._revision = 0

    @property
    def revision(self) -> int:
        return self._revision

    def snapshot(self) -> tuple[TranscriptEntry, ...]:
        return tuple(self._entries)

    def append(
        self,
        kind: TranscriptKind,
        content: str,
        *,
        title: str = "",
        finalized: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        entry = TranscriptEntry(
            entry_id=str(uuid4()),
            kind=kind,
            content=content,
            title=title,
            finalized=finalized,
            metadata=dict(metadata) if metadata is not None else None,
        )
        self._entries.append(entry)
        self._emit("added", entry)
        self._prune()
        return entry.entry_id

    def begin(
        self,
        kind: TranscriptKind,
        *,
        title: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        return self.append(
            kind,
            "",
            title=title,
            finalized=False,
            metadata=metadata,
        )

    def append_delta(self, entry_id: str, text: str) -> TranscriptEntry:
        index = self._entry_index(entry_id)
        current = self._entries[index]
        if current.finalized:
            raise ValueError(f"transcript entry is already finalized: {entry_id}")
        updated = replace(current, content=current.content + text)
        self._entries[index] = updated
        self._emit("updated", updated)
        return updated

    def finalize(self, entry_id: str) -> TranscriptEntry:
        index = self._entry_index(entry_id)
        current = self._entries[index]
        if current.finalized:
            return current
        updated = replace(current, finalized=True)
        self._entries[index] = updated
        self._emit("finalized", updated)
        self._prune()
        return updated

    def clear(self) -> None:
        self._entries.clear()
        self._emit("reset", None)

    def subscribe(self, listener: TranscriptListener) -> Callable[[], None]:
        self._listeners.add(listener)

        def unsubscribe() -> None:
            self._listeners.discard(listener)

        return unsubscribe

    def _entry_index(self, entry_id: str) -> int:
        for index, entry in enumerate(self._entries):
            if entry.entry_id == entry_id:
                return index
        raise KeyError(f"transcript entry not found: {entry_id}")

    def _prune(self) -> None:
        changed = False
        while self._over_limit():
            removable = next(
                (index for index, entry in enumerate(self._entries) if entry.finalized),
                None,
            )
            if removable is None:
                break
            del self._entries[removable]
            changed = True
        if changed:
            self._emit("reset", None)

    def _over_limit(self) -> bool:
        return (
            len(self._entries) > self.max_entries
            or sum(len(entry.content) for entry in self._entries) > self.max_characters
        )

    def _emit(
        self,
        action: TranscriptAction,
        entry: TranscriptEntry | None,
    ) -> None:
        self._revision += 1
        event = TranscriptEvent(self._revision, action, entry)
        for listener in tuple(self._listeners):
            listener(event)
