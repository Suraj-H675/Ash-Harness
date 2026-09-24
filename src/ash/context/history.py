"""Conversation budgeting and deterministic history compaction."""

from __future__ import annotations

import json
import hashlib
import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable


DEFAULT_CONTEXT_BUDGET_WEIGHTS: dict[str, float] = {
    "system": 0.20,
    "tools": 0.15,
    "history": 0.45,
    "repo_map": 0.10,
    "memory": 0.10,
}
IMAGE_TOKEN_ESTIMATE = 1024


class ContextFragmentKind(StrEnum):
    SYSTEM = "system"
    TOOL_SCHEMA = "tool_schema"
    HISTORY = "history"
    REPO_MAP = "repo_map"
    MEMORY = "memory"


class ContextTrust(StrEnum):
    BUILT_IN = "built_in"
    PROJECT = "project"
    SESSION = "session"
    MIXED = "mixed"


@dataclass(frozen=True)
class ContextFragment:
    """Provenance for one exact, bounded provider-input fragment."""

    kind: ContextFragmentKind
    source: str
    trust: ContextTrust
    tokens: int
    limit: int
    truncated: bool
    content_sha256: str
    metadata: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ContextBudgetSlice:
    name: str
    limit: int
    used: int = 0
    truncated: bool = False


@dataclass(frozen=True)
class ContextBudgetReport:
    """Per-turn token budget accounting for provider input construction."""

    maximum: int
    completion_reserve: int
    input_limit: int
    slices: dict[str, ContextBudgetSlice]
    fragments: tuple[ContextFragment, ...] = ()


@dataclass(frozen=True)
class BoundedText:
    text: str
    tokens: int
    truncated: bool


@dataclass(frozen=True)
class CompactionResult:
    messages: list[dict[str, Any]]
    summary: str
    compacted: bool
    estimated_tokens: int
    removed_messages: int = 0
    pruned_tool_outputs: int = 0


class ContextBudgetExceededError(RuntimeError):
    """Raised when protected current-turn context cannot fit the input budget."""

    def __init__(
        self,
        required_tokens: int,
        limit: int,
        *,
        protected_current_turn: bool,
    ) -> None:
        self.required_tokens = required_tokens
        self.limit = limit
        self.protected_current_turn = protected_current_turn
        scope = (
            "current conversation state"
            if protected_current_turn
            else "compacted conversation"
        )
        suffix = (
            "; shorten the current request or reduce current-turn tool output"
            if protected_current_turn
            else ""
        )
        super().__init__(
            f"{scope} exceeds the provider input budget: "
            f"requires approximately {required_tokens} tokens, limit is {limit}{suffix}"
        )


class ContextBudgetAllocator:
    """Allocate and enforce deterministic input budgets across context sections."""

    def __init__(
        self,
        *,
        max_context_tokens: int,
        completion_reserve: int,
        weights: dict[str, float] | None = None,
    ) -> None:
        if max_context_tokens < 1:
            raise ValueError("max_context_tokens must be positive")
        if completion_reserve < 0 or completion_reserve >= max_context_tokens:
            raise ValueError("completion_reserve must be below the context limit")
        self.max_context_tokens = max_context_tokens
        self.completion_reserve = completion_reserve
        self.weights = normalize_context_budget_weights(weights)
        if self.input_limit < len(self.weights):
            raise ValueError(
                "usable context must provide at least one token per budget bucket"
            )

    @property
    def input_limit(self) -> int:
        return max(1, self.max_context_tokens - self.completion_reserve)

    def allocate(self) -> dict[str, int]:
        remaining = self.input_limit
        budgets: dict[str, int] = {}
        items = list(self.weights.items())
        for index, (name, weight) in enumerate(items):
            if index == len(items) - 1:
                limit = remaining
            else:
                limit = max(1, int(self.input_limit * weight))
                limit = min(limit, max(1, remaining - (len(items) - index - 1)))
            budgets[name] = limit
            remaining -= limit
        return budgets

    def fit_text(
        self,
        text: str,
        *,
        limit: int,
        count_tokens: Callable[[str], int],
    ) -> BoundedText:
        tokens = max(0, int(count_tokens(text)))
        if tokens <= limit:
            return BoundedText(text=text, tokens=tokens, truncated=False)
        marker = (
            "\n\n[context section truncated by Ash budget manager; "
            f"original_tokens~{tokens}; budget={limit}]"
        )
        marker_tokens = max(0, int(count_tokens(marker)))
        if marker_tokens >= limit:
            compact_marker = f"[truncated; original_tokens~{tokens}]"
            compact_tokens = max(0, int(count_tokens(compact_marker)))
            if compact_tokens <= limit:
                return BoundedText(
                    text=compact_marker,
                    tokens=compact_tokens,
                    truncated=True,
                )
            return BoundedText(text="", tokens=0, truncated=True)

        low = 0
        high = len(text)
        best = ""
        while low <= high:
            mid = (low + high) // 2
            candidate = text[:mid].rstrip() + marker
            candidate_tokens = max(0, int(count_tokens(candidate)))
            if candidate_tokens <= limit:
                best = candidate
                low = mid + 1
            else:
                high = mid - 1
        final_tokens = max(0, int(count_tokens(best or marker.strip())))
        return BoundedText(
            text=best or marker.strip(),
            tokens=final_tokens,
            truncated=True,
        )

    def report(
        self,
        *,
        limits: dict[str, int],
        usage: dict[str, int],
        truncated: set[str] | None = None,
        fragments: tuple[ContextFragment, ...] = (),
    ) -> ContextBudgetReport:
        truncated = truncated or set()
        slices = {
            name: ContextBudgetSlice(
                name=name,
                limit=limits.get(name, 0),
                used=usage.get(name, 0),
                truncated=name in truncated,
            )
            for name in limits
        }
        return ContextBudgetReport(
            maximum=self.max_context_tokens,
            completion_reserve=self.completion_reserve,
            input_limit=self.input_limit,
            slices=slices,
            fragments=fragments,
        )


def context_fragment(
    *,
    kind: ContextFragmentKind,
    source: str,
    trust: ContextTrust,
    content: str,
    tokens: int,
    limit: int,
    truncated: bool,
    metadata: dict[str, str] | None = None,
) -> ContextFragment:
    """Describe bounded model-visible content without retaining another copy."""

    return ContextFragment(
        kind=kind,
        source=source,
        trust=trust,
        tokens=tokens,
        limit=limit,
        truncated=truncated,
        content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        metadata=tuple(sorted((metadata or {}).items())),
    )


def normalize_context_budget_weights(
    weights: dict[str, float] | None,
) -> dict[str, float]:
    source = dict(DEFAULT_CONTEXT_BUDGET_WEIGHTS)
    if weights:
        unknown = set(weights) - set(source)
        if unknown:
            names = ", ".join(sorted(unknown))
            raise ValueError(f"unknown context budget bucket(s): {names}")
        source.update(weights)
    if any(not math.isfinite(value) or value < 0 for value in source.values()):
        raise ValueError("context budget weights must be finite and non-negative")
    total = sum(source.values())
    if not math.isfinite(total) or total <= 0:
        raise ValueError("context budget weight sum must be finite and positive")
    return {name: value / total for name, value in source.items()}


class HistoryCompactor:
    """Fit canonical chat messages into a bounded model input budget.

    The full transcript remains in the session database. Only the provider
    request is compacted. The summary is deliberately extractive so compaction
    does not require another paid model call or invent facts.
    """

    def __init__(
        self,
        *,
        max_context_tokens: int,
        completion_reserve: int,
        threshold: float = 0.80,
        recent_messages: int = 12,
        summary_char_limit: int = 12_000,
        max_tool_output_chars: int = 8_000,
        input_token_limit: int | None = None,
    ) -> None:
        if max_context_tokens < 1:
            raise ValueError("max_context_tokens must be positive")
        if completion_reserve < 0 or completion_reserve >= max_context_tokens:
            raise ValueError("completion_reserve must be below the context limit")
        if not 0.1 <= threshold <= 1.0:
            raise ValueError("threshold must be between 0.1 and 1.0")
        self.max_context_tokens = max_context_tokens
        self.completion_reserve = completion_reserve
        self.threshold = threshold
        self.recent_messages = max(2, recent_messages)
        self.summary_char_limit = summary_char_limit
        self.max_tool_output_chars = max(256, max_tool_output_chars)
        if input_token_limit is not None and input_token_limit < 1:
            raise ValueError("input_token_limit must be positive")
        self._input_token_limit = input_token_limit

    @property
    def input_limit(self) -> int:
        if self._input_token_limit is not None:
            return self._input_token_limit
        usable = self.max_context_tokens - self.completion_reserve
        return max(1, int(usable * self.threshold))

    def compact(
        self,
        messages: list[dict[str, Any]],
        *,
        count_tokens: Callable[[str], int],
        previous_summary: str = "",
        force: bool = False,
        protected_from_index: int | None = None,
    ) -> CompactionResult:
        prepared, pruned = self._prune_tool_outputs(messages)
        estimated = self._count(prepared, count_tokens)
        if not force and estimated <= self.input_limit:
            return CompactionResult(
                prepared,
                previous_summary,
                bool(pruned),
                estimated,
                pruned_tool_outputs=pruned,
            )
        messages = prepared
        if not messages:
            return CompactionResult(
                messages,
                previous_summary,
                bool(pruned),
                estimated,
                pruned_tool_outputs=pruned,
            )
        if protected_from_index is not None and not (
            0 <= protected_from_index < len(messages)
        ):
            raise ValueError("protected_from_index is outside the message list")

        system = messages[0] if messages[0].get("role") == "system" else None
        body_start = 1 if system is not None else 0
        cutoff = max(body_start, len(messages) - self.recent_messages)
        if protected_from_index is not None:
            cutoff = min(cutoff, protected_from_index)
        while cutoff > body_start and messages[cutoff].get("role") == "tool":
            cutoff -= 1

        removed = list(messages[body_start:cutoff])
        recent = list(messages[cutoff:])
        prefix = [system] if system is not None else []
        protected_recent_index = (
            protected_from_index - cutoff
            if protected_from_index is not None
            else None
        )

        # Fit protected current-turn state first. Older recent history may be
        # removed, but assistant tool calls and their contiguous tool results
        # move as one protocol unit. The last user message and everything after
        # it belong to the active turn and are never discarded here.
        while self._count(prefix + recent, count_tokens) > self.input_limit:
            span = self._oldest_removable_span(
                recent,
                protected_from_index=protected_recent_index,
            )
            if span is None:
                protected_tokens = self._count(prefix + recent, count_tokens)
                raise ContextBudgetExceededError(
                    protected_tokens,
                    self.input_limit,
                    protected_current_turn=True,
                )
            start, end = span
            removed.extend(recent[start:end])
            del recent[start:end]
            if protected_recent_index is not None and start < protected_recent_index:
                protected_recent_index -= end - start

        summary = (
            self._summarize(removed, previous_summary)
            if removed
            else previous_summary
        )
        summary_message = (
            self._fit_summary_message(
                summary,
                prefix=prefix,
                recent=recent,
                count_tokens=count_tokens,
            )
            if removed and summary
            else None
        )
        compacted = prefix + ([summary_message] if summary_message is not None else []) + recent
        final_estimate = self._count(compacted, count_tokens)
        if final_estimate > self.input_limit:
            raise ContextBudgetExceededError(
                final_estimate,
                self.input_limit,
                protected_current_turn=False,
            )
        return CompactionResult(
            messages=compacted,
            summary=summary,
            compacted=bool(removed or pruned),
            estimated_tokens=final_estimate,
            removed_messages=len(removed),
            pruned_tool_outputs=pruned,
        )

    def _fit_summary_message(
        self,
        summary: str,
        *,
        prefix: list[dict[str, Any]],
        recent: list[dict[str, Any]],
        count_tokens: Callable[[str], int],
    ) -> dict[str, Any] | None:
        """Fit a provider-visible summary without shrinking persisted state."""

        header = "## Compacted conversation summary\n"

        def candidate(char_limit: int) -> dict[str, Any]:
            return {
                "role": "system",
                "content": header + _preserve_ends(summary, char_limit),
            }

        full = {"role": "system", "content": header + summary}
        if self._count(prefix + [full] + recent, count_tokens) <= self.input_limit:
            return full

        low = 0
        high = len(summary)
        best: dict[str, Any] | None = None
        while low <= high:
            mid = (low + high) // 2
            attempt = candidate(mid)
            if self._count(prefix + [attempt] + recent, count_tokens) <= self.input_limit:
                best = attempt
                low = mid + 1
            else:
                high = mid - 1
        return best

    @staticmethod
    def _oldest_removable_span(
        messages: list[dict[str, Any]],
        *,
        protected_from_index: int | None,
    ) -> tuple[int, int] | None:
        """Return the oldest safely removable pre-current-turn protocol unit."""

        if not messages:
            return None
        if protected_from_index is not None and protected_from_index <= 0:
            return None

        candidate = messages[0]
        role = candidate.get("role")
        if role == "tool":
            return None
        tool_calls = candidate.get("tool_calls") if role == "assistant" else None
        if not tool_calls:
            return (0, 1)
        if not isinstance(tool_calls, list):
            return None

        call_ids = {
            str(call.get("call_id") or call.get("id") or "")
            for call in tool_calls
            if isinstance(call, dict)
        }
        call_ids.discard("")
        if not call_ids:
            return None

        observed: set[str] = set()
        end = 1
        while end < len(messages) and messages[end].get("role") == "tool":
            tool_id = str(
                messages[end].get("tool_call_id")
                or messages[end].get("call_id")
                or ""
            )
            if not tool_id or tool_id not in call_ids:
                return None
            observed.add(tool_id)
            end += 1
        if observed != call_ids or (
            protected_from_index is not None and end > protected_from_index
        ):
            return None
        return (0, end)

    def _prune_tool_outputs(
        self, messages: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], int]:
        """Replace stale large tool payloads while preserving call identity."""
        boundary = max(0, len(messages) - 2)
        output: list[dict[str, Any]] = []
        pruned = 0
        for index, message in enumerate(messages):
            content = message.get("content")
            if (
                index < boundary
                and message.get("role") == "tool"
                and isinstance(content, str)
                and len(content) > self.max_tool_output_chars
            ):
                replacement = dict(message)
                call_id = (
                    message.get("tool_call_id") or message.get("call_id") or "unknown"
                )
                replacement["content"] = (
                    f"[stale tool output pruned: call_id={call_id}; "
                    f"original_chars={len(content)}]"
                )
                output.append(replacement)
                pruned += 1
            else:
                output.append(message)
        return output, pruned

    def _summarize(
        self,
        messages: list[dict[str, Any]],
        previous_summary: str,
    ) -> str:
        priority_lines: list[str] = []
        if previous_summary:
            priority_lines.append("Earlier summary:")
            priority_lines.append(
                _preserve_ends(previous_summary, self.summary_char_limit // 3)
            )
        priority_lines.extend(_preserved_state_lines(messages))
        event_lines = ["Compacted events:"]
        for message in messages:
            role = str(message.get("role", "unknown"))
            content = _summary_content(message.get("content", "")).strip()
            content = " ".join(content.split())
            if len(content) > 500:
                content = content[:497] + "..."
            calls = message.get("tool_calls") or []
            if calls:
                rendered_calls = ", ".join(
                    f"{call.get('name', '?')}({json.dumps(call.get('arguments', {}), sort_keys=True)[:300]})"
                    for call in calls
                )
                content = f"{content} tools=[{rendered_calls}]".strip()
            if content:
                event_lines.append(f"- {role}: {content}")
        priority = "\n".join(priority_lines)
        events = "\n".join(event_lines)
        if len(priority) >= self.summary_char_limit:
            return _preserve_ends(priority, self.summary_char_limit)
        remaining = self.summary_char_limit - len(priority) - 1
        bounded_events = _preserve_ends(events, max(0, remaining))
        return "\n".join(part for part in (priority, bounded_events) if part)

    @staticmethod
    def _count(
        messages: list[dict[str, Any]],
        count_tokens: Callable[[str], int],
    ) -> int:
        sanitized, image_count = _without_image_data(messages)
        payload = json.dumps(sanitized, ensure_ascii=False, default=str)
        return max(0, int(count_tokens(payload))) + image_count * IMAGE_TOKEN_ESTIMATE


def _without_image_data(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    sanitized: list[dict[str, Any]] = []
    image_count = 0
    for message in messages:
        copied = dict(message)
        content = copied.get("content")
        if isinstance(content, list):
            blocks: list[Any] = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "image":
                    image_count += 1
                    blocks.append(
                        {
                            "type": "image",
                            "media_type": block.get("media_type", ""),
                            "data": "[binary image omitted from token estimate]",
                        }
                    )
                else:
                    blocks.append(block)
            copied["content"] = blocks
        sanitized.append(copied)
    return sanitized, image_count


def _summary_content(content: Any) -> str:
    if not isinstance(content, list):
        return str(content)
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append(str(block.get("text", "")))
        elif block.get("type") == "image":
            parts.append(f"[image: {block.get('media_type', 'unknown')}]")
    return " ".join(parts)


def _preserved_state_lines(messages: list[dict[str, Any]]) -> list[str]:
    user_requests: list[str] = []
    assistant_outcomes: list[str] = []
    tool_actions: list[str] = []
    file_paths: set[str] = set()
    for message in messages:
        role = str(message.get("role", ""))
        content = " ".join(_summary_content(message.get("content", "")).split())
        if role == "user" and content:
            user_requests.append(content[:500])
        elif role == "assistant" and content:
            assistant_outcomes.append(content[:500])
        for call in message.get("tool_calls") or []:
            name = str(call.get("name", "?"))
            arguments = call.get("arguments", {})
            rendered = json.dumps(arguments, sort_keys=True, default=str)[:500]
            tool_actions.append(f"{name}({rendered})")
            if isinstance(arguments, dict):
                for key, value in arguments.items():
                    if key in {
                        "file_path",
                        "path",
                        "cwd",
                        "directory_path",
                    } and isinstance(value, str):
                        file_paths.add(value)
    lines = ["Preserved task state:"]
    lines.extend(f"- User request: {item}" for item in user_requests[-5:])
    lines.extend(f"- Referenced path: {item}" for item in sorted(file_paths)[:20])
    lines.extend(f"- Tool action: {item}" for item in tool_actions[-20:])
    lines.extend(f"- Assistant outcome: {item}" for item in assistant_outcomes[-5:])
    if len(lines) == 1:
        lines.append("- No extractable state in compacted messages.")
    return lines


def _preserve_ends(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    marker = "\n[...summary middle omitted...]\n"
    if len(marker) >= limit:
        return text[:limit]
    available = limit - len(marker)
    head = available // 2
    tail = available - head
    return text[:head] + marker + text[-tail:]
