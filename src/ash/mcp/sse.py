"""Bounded byte-stream decoding for Server-Sent Events."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SSEEvent:
    """One blank-line-delimited SSE event or metadata update."""

    event: str
    data: str | None
    event_id: str | None


class SSEDecoder:
    """Decode SSE framing incrementally while bounding each event in bytes."""

    def __init__(self, max_event_bytes: int) -> None:
        if max_event_bytes <= 0:
            raise ValueError("SSE event limit must be positive")
        self._max_event_bytes = max_event_bytes
        self._line = bytearray()
        self._data: list[str] = []
        self._data_seen = False
        self._event = ""
        self._event_id = ""
        self._pending_event_id: str | None = None
        self._retry_ms: int | None = None
        self._event_bytes = 0
        self._skip_lf = False
        self._cr_ended_event = False
        self._prefix = bytearray()
        self._started = False

    @property
    def retry_ms(self) -> int | None:
        """Most recent valid retry field, including one before a blank line."""

        return self._retry_ms

    @property
    def last_event_id(self) -> str:
        return self._event_id

    def feed(self, chunk: bytes) -> Iterator[SSEEvent]:
        """Consume a transport chunk and yield every complete SSE event."""

        if not self._started:
            needed = 3 - len(self._prefix)
            self._prefix.extend(chunk[:needed])
            chunk = chunk[needed:]
            if len(self._prefix) < 3 and b"\xef\xbb\xbf".startswith(self._prefix):
                return
            prefix = bytes(self._prefix)
            self._prefix.clear()
            self._started = True
            if prefix.startswith(b"\xef\xbb\xbf"):
                prefix = prefix[3:]
            yield from self._feed_bytes(prefix)
        yield from self._feed_bytes(chunk)

    def finish(self) -> None:
        """Discard an unterminated line or event at end of stream."""

        self._line.clear()
        self._data.clear()
        self._data_seen = False
        self._event = ""
        self._pending_event_id = None
        self._event_bytes = 0
        self._skip_lf = False
        self._cr_ended_event = False

    def _feed_bytes(self, chunk: bytes) -> Iterator[SSEEvent]:
        start = 0
        while start < len(chunk):
            if self._skip_lf:
                self._skip_lf = False
                if chunk[start] == 0x0A:
                    if not self._cr_ended_event:
                        self._count_bytes(1)
                    start += 1
                    if start == len(chunk):
                        break
                self._cr_ended_event = False
            cr = chunk.find(b"\r", start)
            lf = chunk.find(b"\n", start)
            if cr < 0:
                delimiter = lf
            elif lf < 0:
                delimiter = cr
            else:
                delimiter = min(cr, lf)
            if delimiter < 0:
                segment = chunk[start:]
                self._cr_ended_event = False
                self._count_bytes(len(segment))
                self._line.extend(segment)
                break
            if delimiter > start:
                segment = chunk[start:delimiter]
                self._cr_ended_event = False
                self._count_bytes(len(segment))
                self._line.extend(segment)
            self._cr_ended_event = False
            if chunk[delimiter] == 0x0D:
                self._count_bytes(1)
                self._skip_lf = True
            else:
                self._count_bytes(1)
            yield from self._end_line()
            start = delimiter + 1

    def _count_bytes(self, byte_count: int) -> None:
        self._event_bytes += byte_count
        if self._event_bytes > self._max_event_bytes:
            raise ValueError(f"SSE event exceeded {self._max_event_bytes} bytes")

    def _end_line(self) -> Iterator[SSEEvent]:
        raw_line = bytes(self._line)
        self._line.clear()
        try:
            line = raw_line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("SSE stream contained invalid UTF-8") from exc
        if not line:
            self._cr_ended_event = True
            event_id = self._pending_event_id
            if event_id is not None:
                self._event_id = event_id
            if self._data_seen:
                yield SSEEvent(
                    event=self._event,
                    data="\n".join(self._data),
                    event_id=event_id,
                )
            elif event_id is not None:
                yield SSEEvent(event=self._event, data=None, event_id=event_id)
            self._data.clear()
            self._data_seen = False
            self._event = ""
            self._pending_event_id = None
            self._event_bytes = 0
            return
        if ":" in line:
            field, value = line.split(":", 1)
            if value.startswith(" "):
                value = value[1:]
        else:
            field, value = line, ""
        if field == "data":
            self._data.append(value)
            self._data_seen = True
        elif field == "event":
            self._event = value
        elif field == "id" and "\x00" not in value:
            self._pending_event_id = value
        elif field == "retry" and value and all("0" <= char <= "9" for char in value):
            try:
                self._retry_ms = int(value)
            except ValueError:
                # Python limits extremely long decimal conversions. The
                # transport only uses this value as a wait duration, so retain
                # an effectively unbounded retry without building an enormous
                # integer from an attacker-controlled line.
                self._retry_ms = 10**4000
