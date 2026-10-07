"""Bounded, app-owned rendering and scrolling for the active conversation."""

from __future__ import annotations

from dataclasses import dataclass

from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.layout.controls import UIContent, UIControl
from prompt_toolkit.utils import get_cwidth
from rich.console import Console
from rich.markdown import Markdown
from rich.style import Style as RichStyle

from ash.ui.transcript import Transcript, TranscriptEntry


@dataclass(frozen=True)
class _DisplayRow:
    entry_id: str | None
    offset: int
    fragments: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class _EntryLayout:
    signature: tuple[str, str, str, bool]
    rows: tuple[tuple[int, tuple[tuple[str, str], ...]], ...]


class TranscriptView(UIControl):
    """A cached semantic transcript with follow-latest and reading-anchor modes."""

    def __init__(self, transcript: Transcript) -> None:
        self.transcript = transcript
        self.follow_latest = True
        self._anchor_id: str | None = None
        self._anchor_offset = 0
        self._entry_cache: dict[str, _EntryLayout] = {}
        self._wrapped_entry_cache: dict[
            str,
            tuple[tuple[str, str, str, bool], int, tuple[_DisplayRow, ...]],
        ] = {}
        self._entries: tuple[TranscriptEntry, ...] = ()
        self._rows: tuple[_DisplayRow, ...] = ()
        self._layout_width = 0
        self._layout_revision = -1
        self._height = 1
        self._on_change = None
        self._unsubscribe = transcript.subscribe(self._transcript_changed)
        self._transcript_changed(None)

    def set_invalidator(self, callback) -> None:
        self._on_change = callback

    @property
    def detached(self) -> bool:
        return not self.follow_latest

    def close(self) -> None:
        self._unsubscribe()

    def is_focusable(self) -> bool:
        return False

    def create_content(self, width: int, height: int) -> UIContent:
        self._height = max(1, height)
        self._ensure_layout(max(1, width))
        top_row = self.top_row(height)
        return UIContent(
            get_line=self._get_line,
            line_count=max(1, len(self._rows)),
            cursor_position=Point(x=0, y=top_row),
            show_cursor=False,
        )

    def scroll(self, rows: int) -> None:
        self._ensure_layout(self._layout_width or 80)
        if not self._rows:
            return
        if self.follow_latest:
            current = max(0, len(self._rows) - self._height)
        else:
            current = self._anchor_row()
        target = max(0, min(current + rows, max(0, len(self._rows) - self._height)))
        if target >= max(0, len(self._rows) - self._height - 1):
            self.scroll_to_latest()
            return
        self.follow_latest = False
        row = self._rows[target]
        self._anchor_id = row.entry_id
        self._anchor_offset = row.offset
        self._notify()

    def scroll_to_latest(self) -> None:
        changed = not self.follow_latest
        self.follow_latest = True
        self._anchor_id = None
        self._anchor_offset = 0
        if changed:
            self._notify()

    def top_row(self, height: int) -> int:
        self._height = max(1, height)
        self._ensure_layout(self._layout_width or 80)
        if self.follow_latest:
            return max(0, len(self._rows) - self._height)
        return min(self._anchor_row(), max(0, len(self._rows) - 1))

    def _anchor_row(self) -> int:
        if self._anchor_id is None:
            return 0
        for index, row in enumerate(self._rows):
            if row.entry_id == self._anchor_id and row.offset >= self._anchor_offset:
                return index
        for index, row in enumerate(self._rows):
            if row.entry_id == self._anchor_id:
                return index
        if self._rows:
            self._anchor_id = self._rows[0].entry_id
            self._anchor_offset = self._rows[0].offset
        return 0

    def _transcript_changed(self, event) -> None:
        if event is not None and event.action == "replaced":
            self.follow_latest = True
            self._anchor_id = None
            self._anchor_offset = 0
            self._entry_cache.clear()
            self._wrapped_entry_cache.clear()
        elif event is not None and event.action == "removed" and event.entry:
            self._entry_cache.pop(event.entry.entry_id, None)
            self._wrapped_entry_cache.pop(event.entry.entry_id, None)
        self._entries = self.transcript.snapshot()
        retained = {entry.entry_id for entry in self._entries}
        self._entry_cache = {
            entry_id: layout
            for entry_id, layout in self._entry_cache.items()
            if entry_id in retained
        }
        self._wrapped_entry_cache = {
            entry_id: cached
            for entry_id, cached in self._wrapped_entry_cache.items()
            if entry_id in retained
        }
        self._layout_revision = -1
        self._notify()

    def _notify(self) -> None:
        if self._on_change is not None:
            self._on_change()

    def _ensure_layout(self, width: int) -> None:
        if (
            self._layout_revision == self.transcript.revision
            and self._layout_width == width
        ):
            return
        self._layout_width = width
        rows: list[_DisplayRow] = []
        notice = self.transcript.omitted_entries
        if notice:
            rows.extend(
                _wrap_styled(
                    f"Earlier transcript entries omitted by the retained-history limit: {notice}.",
                    max(1, width),
                    "class:muted",
                    None,
                )
            )
            rows.append(_DisplayRow(None, 0, (("", ""),)))
        previous_kind: str | None = None
        for entry in self._entries:
            if previous_kind in {"user", "assistant"} and entry.kind in {
                "user",
                "assistant",
            }:
                rows.append(_DisplayRow(entry.entry_id, -1, (("", ""),)))
            wrapped_width = max(1, width)
            if entry.kind == "assistant" and entry.finalized:
                signature = (entry.kind, entry.content, entry.title, entry.finalized)
                cached = self._wrapped_entry_cache.get(entry.entry_id)
                if (
                    cached is not None
                    and cached[0] == signature
                    and cached[1] == wrapped_width
                ):
                    wrapped = cached[2]
                else:
                    wrapped = tuple(
                        _render_final_assistant(entry, wrapped_width)
                    )
                    self._wrapped_entry_cache[entry.entry_id] = (
                        signature,
                        wrapped_width,
                        wrapped,
                    )
            else:
                layout = self._entry_layout(entry)
                cached = self._wrapped_entry_cache.get(entry.entry_id)
                if (
                    cached is not None
                    and cached[0] == layout.signature
                    and cached[1] == wrapped_width
                ):
                    wrapped = cached[2]
                else:
                    wrapped = tuple(
                        _wrap_styled_rows(layout.rows, wrapped_width, entry.entry_id)
                    )
                    self._wrapped_entry_cache[entry.entry_id] = (
                        layout.signature,
                        wrapped_width,
                        wrapped,
                    )
            rows.extend(wrapped)
            previous_kind = entry.kind
        if not rows:
            rows.extend(
                _wrap_styled(
                    "Ready — type a message or use /help",
                    max(1, width),
                    "class:empty-title",
                    None,
                )
            )
        self._rows = tuple(rows)
        self._layout_revision = self.transcript.revision

    def _entry_layout(self, entry: TranscriptEntry) -> _EntryLayout:
        signature = (entry.kind, entry.content, entry.title, entry.finalized)
        cached = self._entry_cache.get(entry.entry_id)
        if cached is not None and cached.signature == signature:
            return cached
        if entry.kind == "user":
            lines = _offset_lines(entry.content, "class:user-band")
        elif entry.kind == "assistant":
            lines = ((-1, (("class:assistant-prefix", "ASH"),)),) + _offset_lines(
                "· " + entry.content,
                "class:assistant-body",
            )
        else:
            label = entry.title or entry.kind.upper()
            label_style = (
                "class:error-prefix"
                if entry.kind == "error"
                else "class:approval-prefix"
                if entry.kind == "approval"
                else "class:tool-prefix"
                if entry.kind == "tool"
                else "class:status-prefix"
            )
            content_lines = entry.content.split("\n") or [""]
            line_offsets: list[int] = []
            offset = 0
            for line in content_lines:
                line_offsets.append(offset)
                offset += len(line) + 1
            lines = tuple(
                (
                    line_offsets[index],
                    ((label_style, f"{label}  ") if index == 0 else ("", ""),)
                    + (("class:transcript-body", line),),
                )
                for index, line in enumerate(content_lines)
            )
        layout = _EntryLayout(signature, lines)
        self._entry_cache[entry.entry_id] = layout
        return layout

    def _get_line(self, index: int) -> StyleAndTextTuples:
        if not self._rows:
            return [("", " ")]
        return list(self._rows[min(max(index, 0), len(self._rows) - 1)].fragments)



def _render_final_assistant(
    entry: TranscriptEntry,
    width: int,
) -> list[_DisplayRow]:
    """Render finalized Markdown to styled prompt-toolkit rows."""

    width = max(1, width)
    rows: list[_DisplayRow] = [
        _DisplayRow(entry.entry_id, -1, (("class:assistant-prefix", "ASH"),))
    ]
    body_width = max(1, width - 2)
    console = Console(
        force_terminal=False,
        color_system="truecolor",
        width=body_width,
        record=False,
        highlight=False,
    )
    rendered_lines = console.render_lines(
        Markdown(entry.content, hyperlinks=False),
        pad=False,
        new_lines=False,
    )
    if not rendered_lines:
        rendered_lines = [[]]

    offset = 0
    for index, line in enumerate(rendered_lines):
        prefix = "· " if index == 0 else "  "
        fragments: list[tuple[str, str]] = [
            ("class:assistant-body", _take_cells(prefix, width))
        ]
        line_text = ""
        for segment in line:
            if segment.control:
                continue
            text = segment.text
            if not text:
                continue
            line_text += text
            fragments.append((_rich_style_to_ptk(segment.style), text))
        rows.append(
            _DisplayRow(
                entry.entry_id,
                offset,
                tuple(fragments),
            )
        )
        offset += len(line_text) + 1
    return rows


def _rich_style_to_ptk(style: RichStyle | None) -> str:
    """Translate the Rich Markdown/Syntax subset Ash emits to prompt-toolkit."""

    if style is None:
        return "class:assistant-body"
    parts = ["class:assistant-body"]
    color = style.color
    if color is not None:
        triplet = color.get_truecolor()
        parts.append(f"#{triplet.red:02x}{triplet.green:02x}{triplet.blue:02x}")
    background = style.bgcolor
    if background is not None:
        triplet = background.get_truecolor()
        parts.append(
            f"bg:#{triplet.red:02x}{triplet.green:02x}{triplet.blue:02x}"
        )
    for enabled, name in (
        (style.bold, "bold"),
        (style.italic, "italic"),
        (style.underline, "underline"),
        (style.strike, "strike"),
        (style.reverse, "reverse"),
        (style.dim, "dim"),
    ):
        if enabled:
            parts.append(name)
    return " ".join(parts)

def _wrap_styled(
    text: str,
    width: int,
    style: str,
    entry_id: str | None,
) -> list[_DisplayRow]:
    result: list[_DisplayRow] = []
    offset = 0
    for logical_line in text.split("\n"):
        for chunk, start in _wrap_line_spans(logical_line, width):
            result.append(_DisplayRow(entry_id, offset + start, ((style, chunk),)))
        offset += len(logical_line) + 1
    return result or [_DisplayRow(entry_id, 0, ((style, ""),))]


def _wrap_styled_rows(
    lines: tuple[tuple[int, tuple[tuple[str, str], ...]], ...],
    width: int,
    entry_id: str,
) -> list[_DisplayRow]:
    result: list[_DisplayRow] = []
    for offset, fragments in lines:
        text = "".join(fragment for _style, fragment in fragments)
        user_band = bool(fragments and fragments[0][0] == "class:user-band")
        user_prefix_width = min(2, max(0, width - 1)) if user_band else 0
        split = _wrap_line_spans(
            text,
            max(1, width - user_prefix_width) if user_band else width,
        )
        for index, (chunk, start) in enumerate(split):
            chunk_offset = offset + start
            styles: tuple[tuple[str, str], ...]
            if user_band:
                prefix = "> " if index == 0 else "  "
                prefix = _take_cells(prefix, user_prefix_width)
                rendered = prefix + chunk
                padding = max(0, width - get_cwidth(rendered))
                styles = (("class:user-band", rendered + " " * padding),)
            else:
                styles = _slice_fragments(fragments, start, start + len(chunk))
            result.append(_DisplayRow(entry_id, chunk_offset, tuple(styles)))
    return result or [_DisplayRow(entry_id, 0, (("", ""),))]


def _offset_lines(
    text: str, style: str
) -> tuple[tuple[int, tuple[tuple[str, str], ...]], ...]:
    lines: list[tuple[int, tuple[tuple[str, str], ...]]] = []
    offset = 0
    for line in text.split("\n") or [""]:
        lines.append((offset, ((style, line),)))
        offset += len(line) + 1
    return tuple(lines)


def _slice_fragments(
    fragments: tuple[tuple[str, str], ...], start: int, end: int
) -> tuple[tuple[str, str], ...]:
    result: list[tuple[str, str]] = []
    offset = 0
    for style, text in fragments:
        fragment_end = offset + len(text)
        left = max(start, offset)
        right = min(end, fragment_end)
        if left < right:
            result.append((style, text[left - offset : right - offset]))
        offset = fragment_end
        if offset >= end:
            break
    return tuple(result) or (("", ""),)


def _wrap_line_spans(value: str, width: int) -> list[tuple[str, int]]:
    if not value:
        return [("", 0)]
    width = max(1, width)
    assistant_ascii = (
        value.startswith("· ")
        and value[2:].isascii()
        and value[2:].isprintable()
    )
    if (value.isascii() and value.isprintable()) or assistant_ascii:
        ascii_chunks: list[tuple[str, int]] = []
        start = 0
        while start < len(value):
            end = min(start + width, len(value))
            if end == len(value):
                ascii_chunks.append((value[start:], start))
                break
            split_at = value.rfind(" ", start, end + 1)
            if split_at > start:
                end = split_at
            chunk = value[start:end].rstrip(" ")
            if not chunk:
                end = max(start + 1, end)
                chunk = value[start:end]
            ascii_chunks.append((chunk, start))
            start = end
            while start < len(value) and value[start] == " ":
                start += 1
        return ascii_chunks

    chunks: list[tuple[str, int]] = []
    start = 0
    while start < len(value):
        used = 0
        end = start
        while end < len(value):
            char_width = get_cwidth(value[end])
            if used + char_width > width:
                break
            used += char_width
            end += 1
        if end == len(value):
            chunks.append((value[start:], start))
            break
        if end == start:
            end += 1
        split_at = value.rfind(" ", start, end + 1)
        if split_at > start:
            end = split_at
        chunk = value[start:end].rstrip(" ")
        if not chunk:
            end = max(start + 1, end)
            chunk = value[start:end]
        chunks.append((chunk, start))
        start = end
        while start < len(value) and value[start] == " ":
            start += 1
    return chunks


def _take_cells(value: str, width: int) -> str:
    if width <= 0:
        return ""
    result: list[str] = []
    used = 0
    for character in value:
        character_width = get_cwidth(character)
        if used + character_width > width:
            break
        result.append(character)
        used += character_width
    return "".join(result)
