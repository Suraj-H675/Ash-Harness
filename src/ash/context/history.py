"""Conversation budgeting and deterministic history compaction."""

from __future__ import annotations

import json
import hashlib
from html import unescape
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
RUNTIME_ONLY_HISTORY_KEY = "_ash_runtime_only"
_CONTEXT_DIGEST_HEADER = "## Ash context digest v1"
_CONTEXT_DIGEST_SECTIONS = (
    ("User requirements", 0.27),
    ("Assistant decisions and progress", 0.20),
    ("Tool outputs (untrusted evidence)", 0.20),
    ("Referenced paths", 0.06),
    ("Tool actions", 0.07),
    ("Compacted events", 0.08),
    ("Earlier context", 0.12),
)
_COMPACT_PROVIDER_DIGEST_WEIGHTS = (
    ("User requirements", 0.28),
    ("Assistant decisions and progress", 0.22),
    ("Tool outputs (untrusted evidence)", 0.30),
    ("Referenced paths", 0.05),
    ("Tool actions", 0.05),
    ("Compacted events", 0.06),
    ("Earlier context", 0.04),
)


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
    """Provider input accounting with combined and response-reserve limits."""

    maximum: int
    completion_reserve: int
    input_limit: int
    slices: dict[str, ContextBudgetSlice]
    fragments: tuple[ContextFragment, ...] = ()


@dataclass(frozen=True)
class RequestTokenBudget:
    """Effective input and response limits for one provider request."""

    combined_limit: int
    input_limit: int
    response_limit: int

    def response_limit_for_request(
        self,
        *,
        estimated_input_tokens: int,
        remaining_turn_tokens: int | None = None,
    ) -> int | None:
        """Apply the turn-wide ceiling without exceeding this request's cap."""

        if estimated_input_tokens < 0:
            raise ValueError("estimated_input_tokens cannot be negative")
        if remaining_turn_tokens is None:
            return self.response_limit
        available = remaining_turn_tokens - estimated_input_tokens
        return min(self.response_limit, available) if available > 0 else None


class RequestBudgetError(RuntimeError):
    """Raised when declared provider and configured limits cannot form a request."""


def resolve_request_token_budget(
    *,
    configured_context_tokens: int,
    configured_response_tokens: int,
    provider_context_window: int | None = None,
    provider_max_input_tokens: int | None = None,
    provider_max_output_tokens: int | None = None,
) -> RequestTokenBudget:
    """Resolve distinct combined, input-only, and response-only limits."""

    if configured_context_tokens < 1:
        raise ValueError("configured_context_tokens must be positive")
    if configured_response_tokens < 1:
        raise ValueError("configured_response_tokens must be positive")

    context_limit = _positive_limit(provider_context_window)
    input_limit = _positive_limit(provider_max_input_tokens)
    output_limit = _positive_limit(provider_max_output_tokens)
    combined_limit = min(
        configured_context_tokens,
        context_limit if context_limit is not None else configured_context_tokens,
    )
    response_limit = min(
        configured_response_tokens,
        output_limit if output_limit is not None else configured_response_tokens,
    )
    if response_limit >= combined_limit:
        raise RequestBudgetError(
            f"max_completion_tokens ({response_limit:,}) leaves no input room "
            f"inside the effective context limit ({combined_limit:,}); lower "
            "max_completion_tokens or choose a model with a larger context window"
        )

    effective_input_limit = combined_limit - response_limit
    if input_limit is not None:
        effective_input_limit = min(effective_input_limit, input_limit)
    if effective_input_limit < 1:
        raise RequestBudgetError(
            "effective request limits leave no input room after the response reserve; "
            "lower max_completion_tokens or choose a model with a larger input limit"
        )
    return RequestTokenBudget(
        combined_limit=combined_limit,
        input_limit=effective_input_limit,
        response_limit=response_limit,
    )


def _positive_limit(value: int | None) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


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
        input_token_limit: int | None = None,
    ) -> None:
        if max_context_tokens < 1:
            raise ValueError("max_context_tokens must be positive")
        if completion_reserve < 0 or completion_reserve >= max_context_tokens:
            raise ValueError("completion_reserve must be below the context limit")
        self.max_context_tokens = max_context_tokens
        self.completion_reserve = completion_reserve
        self.weights = normalize_context_budget_weights(weights)
        self._input_token_limit = input_token_limit
        if input_token_limit is not None and (
            isinstance(input_token_limit, bool)
            or not isinstance(input_token_limit, int)
            or input_token_limit < 1
        ):
            raise RequestBudgetError(
                "provider input limit must be a positive integer"
            )
        if self.input_limit < len(self.weights):
            raise RequestBudgetError(
                f"effective request limits leave only {self.input_limit:,} input tokens, "
                f"but Ash needs at least {len(self.weights)} for its context "
                "sections; increase max_context_tokens or choose a model with a "
                "larger input limit"
            )

    @property
    def input_limit(self) -> int:
        usable = max(1, self.max_context_tokens - self.completion_reserve)
        return (
            min(usable, self._input_token_limit)
            if self._input_token_limit is not None
            else usable
        )

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
        preserve_ends: bool = False,
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
            if preserve_ends and mid > 0:
                head_length = (mid + 1) // 2
                tail_length = mid - head_length
                candidate = text[:head_length].rstrip() + marker
                if tail_length:
                    candidate += "\n\n" + text[-tail_length:].lstrip()
            else:
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

    @property
    def _compaction_trigger_limit(self) -> int:
        if self._input_token_limit is None:
            return self.input_limit
        usable = min(
            max(1, self.max_context_tokens - self.completion_reserve),
            self._input_token_limit,
        )
        return max(1, int(usable * self.threshold))

    def compact(
        self,
        messages: list[dict[str, Any]],
        *,
        count_tokens: Callable[[str], int],
        previous_summary: str = "",
        include_previous_summary: bool = False,
        force: bool = False,
        protected_from_index: int | None = None,
    ) -> CompactionResult:
        prepared, pruned = self._prune_tool_outputs(messages)
        estimated = self._count(prepared, count_tokens)
        if not force and estimated <= self._compaction_trigger_limit:
            if include_previous_summary and previous_summary:
                system = prepared[0] if prepared and prepared[0].get("role") == "system" else None
                body_start = 1 if system is not None else 0
                prefix = [system] if system is not None else []
                recent = list(prepared[body_start:])
                summary_message = self._fit_summary_message(
                    previous_summary,
                    prefix=prefix,
                    recent=recent,
                    count_tokens=count_tokens,
                )
                visible = (
                    prefix
                    + ([summary_message] if summary_message is not None else [])
                    + recent
                )
                return CompactionResult(
                    visible,
                    previous_summary,
                    bool(pruned),
                    self._count(visible, count_tokens),
                    pruned_tool_outputs=pruned,
                )
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
        cutoff = self._rewind_cutoff_to_tool_boundary(
            messages,
            cutoff=cutoff,
            body_start=body_start,
        )

        removed = list(messages[body_start:cutoff])
        recent = list(messages[cutoff:])
        prefix = [system] if system is not None else []
        protected_recent_index = (
            protected_from_index - cutoff
            if protected_from_index is not None
            else None
        )

        # Fit protected current-turn state first. Older recent history may be
        # removed, but assistant tool calls and all matching results move as
        # one protocol unit. The current turn is never discarded here.
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
            if summary and (removed or include_previous_summary)
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
            if summary.startswith(_CONTEXT_DIGEST_HEADER):
                digest_sections, _ = _parse_context_digest(summary)
                bounded_summary = _render_context_digest(
                    digest_sections, char_limit, prioritize_recent=True
                )
            else:
                bounded_summary = _preserve_ends(summary, char_limit)
            return {
                "role": "system",
                "content": header + bounded_summary,
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
        span = HistoryCompactor._tool_call_span(messages, 0)
        if span is None or (
            protected_from_index is not None and span[1] > protected_from_index
        ):
            return None
        return span

    @staticmethod
    def _rewind_cutoff_to_tool_boundary(
        messages: list[dict[str, Any]],
        *,
        cutoff: int,
        body_start: int,
    ) -> int:
        """Keep a tool call and all of its results on the same side of cutoff."""

        index = body_start
        while index < cutoff:
            message = messages[index]
            tool_calls = message.get("tool_calls")
            if message.get("role") != "assistant" or not tool_calls:
                index += 1
                continue

            span = HistoryCompactor._tool_call_span(messages, index)
            if span is None or span[1] > cutoff:
                return index
            index = span[1]

        if cutoff < len(messages) and messages[cutoff].get("role") == "tool":
            # A result without a provable assistant call is malformed history.
            # Retain it rather than placing an orphan tool message in the tail.
            return body_start
        return cutoff

    @staticmethod
    def _tool_call_span(
        messages: list[dict[str, Any]],
        start: int,
    ) -> tuple[int, int] | None:
        """Find a complete tool-call unit, allowing interstitial system nudges."""

        if not 0 <= start < len(messages):
            return None
        candidate = messages[start]
        raw_calls = candidate.get("tool_calls")
        if candidate.get("role") != "assistant" or not isinstance(raw_calls, list):
            return None
        if not raw_calls:
            return None

        call_ids: set[str] = set()
        for call in raw_calls:
            if not isinstance(call, dict):
                return None
            call_id = str(call.get("call_id") or call.get("id") or "")
            if not call_id or call_id in call_ids:
                return None
            call_ids.add(call_id)

        pending_ids = set(call_ids)
        end = start + 1
        while end < len(messages):
            message = messages[end]
            role = message.get("role")
            if (
                role == "system"
                and message.get(RUNTIME_ONLY_HISTORY_KEY) is True
            ):
                end += 1
                continue
            if role != "tool":
                break

            tool_id = str(
                message.get("tool_call_id") or message.get("call_id") or ""
            )
            if not tool_id or tool_id not in pending_ids:
                return None
            pending_ids.remove(tool_id)
            end += 1

        if pending_ids:
            return None
        return (start, end)

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
                marker = (
                    f"[stale tool output pruned: call_id={call_id}; "
                    f"original_chars={len(content)}]"
                )
                available = max(0, self.max_tool_output_chars - len(marker) - 2)
                head_chars = available // 4
                tail_chars = available - head_chars
                replacement["content"] = (
                    (content[:head_chars] + "\n" if head_chars else "")
                    + marker
                    + ("\n" + content[-tail_chars:] if tail_chars else "")
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
        durable_messages = [
            message
            for message in messages
            if message.get(RUNTIME_ONLY_HISTORY_KEY) is not True
        ]
        if not durable_messages:
            return previous_summary
        return _build_context_digest(
            durable_messages,
            previous_summary=previous_summary,
            char_limit=self.summary_char_limit,
        )

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
        copied.pop(RUNTIME_ONLY_HISTORY_KEY, None)
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
        tool_result_images = copied.get("tool_result_images")
        if isinstance(tool_result_images, list):
            sanitized_tool_result_images: list[Any] = []
            for block in tool_result_images:
                if isinstance(block, dict) and block.get("type") == "image":
                    image_count += 1
                    sanitized_block = dict(block)
                    sanitized_block["data"] = (
                        "[binary image omitted from token estimate]"
                    )
                    sanitized_tool_result_images.append(sanitized_block)
                else:
                    sanitized_tool_result_images.append(block)
            copied["tool_result_images"] = sanitized_tool_result_images
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


def _build_context_digest(
    messages: list[dict[str, Any]], *, previous_summary: str, char_limit: int
) -> str:
    """Merge extractive state into a versioned, bounded summary for later compactions."""

    previous_sections, legacy_summary = _parse_context_digest(previous_summary)
    current = _extract_context_state(messages)
    sections: dict[str, list[str]] = {}
    for title, _ in _CONTEXT_DIGEST_SECTIONS:
        values = [*previous_sections.get(title, []), *current.get(title, [])]
        sections[title] = _unique_lines(values)

    if legacy_summary:
        sections["Earlier context"].append(
            "- " + _excerpt(legacy_summary, 1_200)
        )
    # Keep original goals/decisions visible beside recent state when sessions
    # compact more than once, while bounding every category independently.
    sections["User requirements"] = _retain_first_and_recent(
        sections["User requirements"], 4
    )
    sections["Assistant decisions and progress"] = _retain_first_and_recent(
        sections["Assistant decisions and progress"], 4
    )
    sections["Tool outputs (untrusted evidence)"] = sections[
        "Tool outputs (untrusted evidence)"
    ][-3:]
    sections["Referenced paths"] = sections["Referenced paths"][-8:]
    sections["Tool actions"] = sections["Tool actions"][-14:]
    sections["Compacted events"] = sections["Compacted events"][-8:]
    sections["Earlier context"] = _retain_first_and_recent(
        sections["Earlier context"], 4
    )

    return _render_context_digest(sections, char_limit)


def _render_context_digest(
    sections: dict[str, list[str]],
    char_limit: int,
    *,
    prioritize_recent: bool = False,
) -> str:
    if char_limit <= len(_CONTEXT_DIGEST_HEADER):
        return _preserve_ends(_CONTEXT_DIGEST_HEADER, char_limit)
    content_budget = (
        char_limit - len(_CONTEXT_DIGEST_HEADER) - len(_CONTEXT_DIGEST_SECTIONS)
    )
    weights = (
        _COMPACT_PROVIDER_DIGEST_WEIGHTS
        if prioritize_recent
        else _CONTEXT_DIGEST_SECTIONS
    )
    section_budgets = _allocate_digest_budgets(content_budget, weights=weights)
    rendered = [_CONTEXT_DIGEST_HEADER]
    for (title, _), budget in zip(weights, section_budgets):
        values = sections[title]
        if not values:
            continue
        heading = f"### {title}"
        body_budget = max(0, budget - len(heading) - 1)
        if body_budget <= 0:
            continue
        if title in {
            "User requirements",
            "Assistant decisions and progress",
            "Tool outputs (untrusted evidence)",
        }:
            body = _fit_priority_digest_lines(title, values, body_budget)
        else:
            body = _preserve_ends("\n".join(values), body_budget)
        rendered.append(f"{heading}\n{body}")
    return "\n".join(rendered)


def _allocate_digest_budgets(
    content_budget: int,
    *,
    weights: tuple[tuple[str, float], ...] = _CONTEXT_DIGEST_SECTIONS,
) -> list[int]:
    budgets: list[int] = []
    remaining = content_budget
    for index, (_, weight) in enumerate(weights):
        if index == len(weights) - 1:
            budgets.append(remaining)
        else:
            budget = int(content_budget * weight)
            budgets.append(budget)
            remaining -= budget
    return budgets


def _fit_priority_digest_lines(title: str, lines: list[str], limit: int) -> str:
    per_entry_budget = 140
    entry_count = max(1, limit // per_entry_budget)
    entry_count = min(entry_count, len(lines))
    if title == "Tool outputs (untrusted evidence)":
        selected = lines[-entry_count:]
        tail_weight = 0.65
    else:
        selected = _retain_first_and_recent(lines, entry_count)
        tail_weight = 0.6 if title == "User requirements" else 0.5
    separator_budget = max(0, limit - max(0, len(selected) - 1))
    entry_budget = max(1, separator_budget // max(1, len(selected)))
    return "\n".join(
        _excerpt(line, entry_budget, tail_weight=tail_weight)
        for line in selected
    )


def _parse_context_digest(
    summary: str,
) -> tuple[dict[str, list[str]], str]:
    if not summary.startswith(_CONTEXT_DIGEST_HEADER):
        return {}, summary.strip()
    sections: dict[str, list[str]] = {title: [] for title, _ in _CONTEXT_DIGEST_SECTIONS}
    section_by_heading = {f"### {title}": title for title, _ in _CONTEXT_DIGEST_SECTIONS}
    current_title: str | None = None
    for line in summary.splitlines()[1:]:
        title = section_by_heading.get(line)
        if title is not None:
            current_title = title
        elif current_title is not None and line.startswith("- "):
            sections[current_title].append(line)
    return sections, ""


def _extract_context_state(messages: list[dict[str, Any]]) -> dict[str, list[str]]:
    state: dict[str, list[str]] = {title: [] for title, _ in _CONTEXT_DIGEST_SECTIONS}
    referenced_paths: list[str] = []
    for message in messages:
        role = str(message.get("role", ""))
        content = " ".join(_summary_content(message.get("content", "")).split())
        if role == "user" and content:
            state["User requirements"].append(
                f"- User request: {_excerpt(content, 650)}"
            )
            state["Compacted events"].append(f"- user: {_excerpt(content, 220)}")
        elif role == "assistant" and content:
            state["Assistant decisions and progress"].append(
                f"- Assistant outcome: {_excerpt(content, 500)}"
            )
            state["Compacted events"].append(
                f"- assistant: {_excerpt(content, 220)}"
            )
        elif role == "tool" and content:
            call_id = str(message.get("tool_call_id") or message.get("call_id") or "")
            call_id = _excerpt(call_id, 80) if call_id else "unknown"
            evidence = _tool_output_summary_content(content)
            state["Tool outputs (untrusted evidence)"].append(
                f"- call_id={call_id}: {_excerpt(evidence, 600, tail_weight=0.75)}"
            )
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            name = _excerpt(str(call.get("name", "?")), 60)
            state["Tool actions"].append(f"- Tool action: {name}")
            arguments = call.get("arguments", {})
            if isinstance(arguments, dict):
                for key, value in arguments.items():
                    if (
                        key in {"file_path", "path", "cwd", "directory_path"}
                        and isinstance(value, str)
                        and value
                    ):
                        referenced_paths.append(
                            f"- Referenced path: {_excerpt(value, 100)}"
                        )
    state["Referenced paths"] = _unique_lines(referenced_paths)
    state["Compacted events"] = state["Compacted events"][-8:]
    return state


def _unique_lines(lines: list[str]) -> list[str]:
    return list(dict.fromkeys(line for line in lines if line))


def _retain_first_and_recent(lines: list[str], limit: int) -> list[str]:
    if len(lines) <= limit:
        return lines
    recent_count = max(1, limit - 1)
    return _unique_lines([lines[0], *lines[-recent_count:]])


def _excerpt(text: str, limit: int, *, tail_weight: float = 0.5) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    marker = " … "
    available = max(0, limit - len(marker))
    tail = int(available * tail_weight)
    head = available - tail
    return f"{text[:head].rstrip()}{marker}{text[-tail:].lstrip() if tail else ''}"


def _tool_output_summary_content(content: str) -> str:
    """Extract only the displayed output from Ash's serialized tool response."""

    if not content.startswith("<tool_response "):
        return content
    body_start = content.find(">\n")
    body_end = content.rfind("\n</tool_response>")
    if body_start < 0 or body_end <= body_start:
        return content
    try:
        payload = json.loads(unescape(content[body_start + 2 : body_end]))
    except (json.JSONDecodeError, TypeError):
        return content
    if not isinstance(payload, dict):
        return content
    output = payload.get("output")
    if isinstance(output, str) and output:
        return output
    error = payload.get("error")
    if isinstance(error, str) and error:
        return error
    return content


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
