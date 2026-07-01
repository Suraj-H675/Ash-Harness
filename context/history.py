"""Conversation budgeting and deterministic history compaction."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable


DEFAULT_CONTEXT_BUDGET_WEIGHTS: dict[str, float] = {
    "system": 0.20,
    "tools": 0.15,
    "history": 0.45,
    "repo_map": 0.10,
    "memory": 0.10,
}
IMAGE_TOKEN_ESTIMATE = 1024


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
    if any(value < 0 for value in source.values()):
        raise ValueError("context budget weights must be non-negative")
    total = sum(source.values())
    if total <= 0:
        raise ValueError("at least one context budget weight must be positive")
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
        if len(messages) <= 2:
            return CompactionResult(
                messages,
                previous_summary,
                bool(pruned),
                estimated,
                pruned_tool_outputs=pruned,
            )

        system = messages[0] if messages[0].get("role") == "system" else None
        body_start = 1 if system is not None else 0
        cutoff = max(body_start, len(messages) - self.recent_messages)
        while cutoff > body_start and messages[cutoff].get("role") == "tool":
            cutoff -= 1

        removed = messages[body_start:cutoff]
        recent = messages[cutoff:]
        if not removed:
            return CompactionResult(
                messages,
                previous_summary,
                bool(pruned),
                estimated,
                pruned_tool_outputs=pruned,
            )

        summary = self._summarize(removed, previous_summary)
        summary_message = {
            "role": "system",
            "content": "## Compacted conversation summary\n" + summary,
        }
        compacted = (
            ([system] if system is not None else []) + [summary_message] + recent
        )

        # If the recent tail itself is still too large, drop oldest complete
        # entries until it fits. Never drop the current user message.
        while (
            len(compacted) > 3
            and self._count(compacted, count_tokens) > self.input_limit
        ):
            drop_at = 2 if system is not None else 1
            candidate = compacted[drop_at]
            if candidate.get("role") == "tool":
                break
            if candidate.get("role") == "assistant" and candidate.get("tool_calls"):
                # A tool result is meaningful only with its originating call.
                # Keep the newest pair intact even if the estimate remains high.
                break
            del compacted[drop_at]

        final_estimate = self._count(compacted, count_tokens)
        return CompactionResult(
            messages=compacted,
            summary=summary,
            compacted=True,
            estimated_tokens=final_estimate,
            removed_messages=len(removed),
            pruned_tool_outputs=pruned,
        )

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
        lines: list[str] = []
        if previous_summary:
            lines.append("Earlier summary:")
            lines.append(previous_summary[-self.summary_char_limit // 2 :])
        lines.append("Compacted events:")
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
                lines.append(f"- {role}: {content}")
        summary = "\n".join(lines)
        return summary[-self.summary_char_limit :]

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
