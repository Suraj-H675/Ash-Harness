"""Conversation budgeting and deterministic history compaction."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class CompactionResult:
    messages: list[dict[str, Any]]
    summary: str
    compacted: bool
    estimated_tokens: int
    removed_messages: int = 0
    pruned_tool_outputs: int = 0


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

    @property
    def input_limit(self) -> int:
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
        compacted = ([system] if system is not None else []) + [summary_message] + recent

        # If the recent tail itself is still too large, drop oldest complete
        # entries until it fits. Never drop the current user message.
        while len(compacted) > 3 and self._count(compacted, count_tokens) > self.input_limit:
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
                call_id = message.get("tool_call_id") or message.get("call_id") or "unknown"
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
            content = str(message.get("content", "")).strip()
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
        payload = json.dumps(messages, ensure_ascii=False, default=str)
        return max(0, int(count_tokens(payload)))
