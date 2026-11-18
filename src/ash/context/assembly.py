"""Deterministic provider-input assembly, without runtime or database effects.

The caller gathers live inputs and owns publication of usage, summary writes,
and resident-history changes. This module only chooses what the provider sees.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from ash.context.history import (
    RUNTIME_ONLY_HISTORY_KEY,
    CompactionResult,
    ContextBudgetAllocator,
    ContextBudgetExceededError,
    ContextBudgetReport,
    ContextFragmentKind,
    ContextTrust,
    HistoryCompactor,
    RequestTokenBudget,
    context_fragment,
)


@dataclass(frozen=True)
class ContextAssemblyRequest:
    """A snapshot of provider input, not a reference to the mutable AshLoop."""

    budget: RequestTokenBudget
    count_tokens: Callable[[str], int]
    budget_weights: dict[str, float]
    compaction_threshold: float
    recent_messages: int
    max_tool_result_tokens: int
    boundary: str
    core_system: str
    persistent_instructions: str
    supplemental_system: str
    external_instructions: bool
    repository: str
    memory: str
    history: list[dict[str, Any]]
    protected_history_index: int | None
    previous_summary: str
    include_previous_summary: bool
    force_compaction: bool
    tool_schema_payload: list[dict[str, Any]]
    tool_schema_content: str
    tool_schema_tokens: int
    registered_tool_count: int
    native_tools: bool


@dataclass(frozen=True)
class ContextAssemblyResult:
    """Provider payload and a proposed compaction; no effects have committed."""

    messages: list[dict[str, Any]]
    compaction: CompactionResult
    budget_report: ContextBudgetReport
    usage_event: dict[str, Any]
    tokens: int
    maximum_input: int


def _allocate_section_limits(
    limit: int, demands: list[int], weights: list[float]
) -> list[int]:
    """Share a bounded section budget; return unused room to live sources."""

    allocations = [
        min(max(0, demand), max(0, int(limit * weight)))
        for demand, weight in zip(demands, weights)
    ]
    remaining = max(0, limit - sum(allocations))
    priority = sorted(
        range(min(len(demands), len(weights))),
        key=lambda index: weights[index],
        reverse=True,
    )
    while remaining:
        progress = False
        for index in priority:
            unused_demand = max(0, demands[index] - allocations[index])
            if not unused_demand:
                continue
            added = min(remaining, unused_demand)
            allocations[index] += added
            remaining -= added
            progress = True
            if not remaining:
                break
        if not progress:
            break
    return allocations


def _tool_names(payload: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for schema in payload:
        function = schema.get("function")
        name = (
            function.get("name") if isinstance(function, dict) else schema.get("name")
        )
        if isinstance(name, str) and name not in names:
            names.append(name)
    return names


def _tool_name_preview(names: list[str]) -> str:
    visible_preview_limit = 8
    if not names:
        return "none"
    preview = ",".join(names[:visible_preview_limit])
    omitted = max(0, len(names) - visible_preview_limit)
    return f"{preview},+{omitted}_more" if omitted else preview


def assemble_context(request: ContextAssemblyRequest) -> ContextAssemblyResult:
    """Fit trusted layers, untrusted context, tools and history for one request."""

    count_tokens = request.count_tokens
    budget = request.budget
    allocator = ContextBudgetAllocator(
        max_context_tokens=budget.combined_limit,
        completion_reserve=budget.response_limit,
        weights=request.budget_weights,
        input_token_limit=budget.input_limit,
    )
    budget_limits = allocator.allocate()
    usage: dict[str, int] = {}
    truncated: set[str] = set()

    schema_tokens = request.tool_schema_tokens
    if schema_tokens >= allocator.input_limit:
        raise ContextBudgetExceededError(
            schema_tokens + 1,
            allocator.input_limit,
            protected_current_turn=True,
        )
    usage["tools"] = schema_tokens

    boundary_tokens = max(0, int(count_tokens(request.boundary)))
    system_limit = max(1, budget_limits["system"] - boundary_tokens)
    system_texts = [
        request.core_system,
        request.persistent_instructions,
        request.supplemental_system,
    ]
    demands = [max(0, int(count_tokens(part))) if part else 0 for part in system_texts]
    section_limits = _allocate_section_limits(system_limit, demands, [0.55, 0.35, 0.10])
    fits = [
        allocator.fit_text(
            part,
            limit=limit,
            count_tokens=count_tokens,
            preserve_ends=index == 1,
        )
        for index, (part, limit) in enumerate(zip(system_texts, section_limits))
        if part
    ]
    fitted_by_source = {}
    fit_index = 0
    for source, content in zip(("core", "persistent", "supplemental"), system_texts):
        if not content:
            continue
        fitted_by_source[source] = fits[fit_index]
        fit_index += 1
    system_fragment = "\n\n".join(
        part
        for part in (
            fitted_by_source["core"].text if "core" in fitted_by_source else "",
            fitted_by_source["persistent"].text
            if "persistent" in fitted_by_source
            else "",
            fitted_by_source["supplemental"].text
            if "supplemental" in fitted_by_source
            else "",
            request.boundary,
        )
        if part
    )
    usage["system"] = max(0, int(count_tokens(system_fragment)))
    if any(fit.truncated for fit in fits) or usage["system"] > budget_limits["system"]:
        truncated.add("system")
    system_metadata = {
        "injection_boundary": "true",
        "external_instructions": str(request.external_instructions).lower(),
        "persistent_instructions_truncated": str(
            fitted_by_source.get("persistent") is not None
            and fitted_by_source["persistent"].truncated
        ).lower(),
        "core_system_truncated": str(
            fitted_by_source.get("core") is not None
            and fitted_by_source["core"].truncated
        ).lower(),
        "supplemental_system_context_truncated": str(
            fitted_by_source.get("supplemental") is not None
            and fitted_by_source["supplemental"].truncated
        ).lower(),
    }
    system_parts = [system_fragment]
    repository_fragment = ""
    memory_fragment = ""

    if request.repository:
        repo_fit = allocator.fit_text(
            request.repository,
            limit=budget_limits["repo_map"],
            count_tokens=count_tokens,
        )
        usage["repo_map"] = repo_fit.tokens
        if repo_fit.truncated:
            truncated.add("repo_map")
        system_parts.append(repo_fit.text)
        repository_fragment = repo_fit.text
    else:
        usage["repo_map"] = 0

    if request.memory:
        memory_fit = allocator.fit_text(
            request.memory,
            limit=budget_limits["memory"],
            count_tokens=count_tokens,
        )
        usage["memory"] = memory_fit.tokens
        if memory_fit.truncated:
            truncated.add("memory")
        system_parts.append(memory_fit.text)
        memory_fragment = memory_fit.text
    else:
        usage["memory"] = 0

    system_content = "\n\n".join(part for part in system_parts if part)
    native_schema_tokens = schema_tokens
    if not request.native_tools:
        native_schema_tokens = 0
        if request.tool_schema_content:
            system_content = f"{system_content}\n\n{request.tool_schema_content}"
    messages = [
        {"role": "system", "content": system_content},
        *(dict(item) for item in request.history),
    ]

    reserved_message_tokens = (
        usage["system"]
        + usage["repo_map"]
        + usage["memory"]
        + (schema_tokens if native_schema_tokens == 0 else 0)
    )
    compactor = HistoryCompactor(
        max_context_tokens=budget.combined_limit,
        completion_reserve=budget.response_limit,
        threshold=request.compaction_threshold,
        recent_messages=request.recent_messages,
        max_tool_output_chars=request.max_tool_result_tokens * 4,
        input_token_limit=allocator.input_limit - native_schema_tokens,
    )
    compaction = compactor.compact(
        messages,
        count_tokens=count_tokens,
        previous_summary=request.previous_summary,
        include_previous_summary=request.include_previous_summary,
        force=request.force_compaction,
        protected_from_index=request.protected_history_index,
    )
    tokens = compaction.estimated_tokens + native_schema_tokens
    maximum_input = budget.input_limit
    if tokens > maximum_input:
        raise ContextBudgetExceededError(
            tokens,
            maximum_input,
            protected_current_turn=True,
        )
    usage["history"] = max(0, compaction.estimated_tokens - reserved_message_tokens)
    if usage["history"] > budget_limits["history"]:
        truncated.add("history")

    usage_event = {
        "type": "context.usage",
        "current": tokens,
        "maximum": maximum_input,
        "system_tokens": usage["system"],
        "tool_schema_tokens": usage["tools"],
        "history_tokens": usage["history"],
        "repo_map_tokens": usage["repo_map"],
        "memory_tokens": usage["memory"],
        "history_message_count": max(0, len(compaction.messages) - 1),
        "tool_message_count": sum(
            1 for item in compaction.messages if item.get("role") == "tool"
        ),
        "provider_replay_state_preserved": compaction.provider_replay_state_preserved,
        "tool_message_chars": sum(
            len(item["content"])
            for item in compaction.messages
            if item.get("role") == "tool" and isinstance(item.get("content"), str)
        ),
    }

    # The marker belongs to Ash's compactor, never to the provider. History
    # entries are newly serialized by the caller; no Session object is changed.
    for item in compaction.messages:
        item.pop(RUNTIME_ONLY_HISTORY_KEY, None)
    history_content = json.dumps(compaction.messages[1:], sort_keys=True, default=str)
    fragments = (
        context_fragment(
            kind=ContextFragmentKind.SYSTEM,
            source="assembled_system_prompt",
            trust=(
                ContextTrust.MIXED
                if request.external_instructions
                else ContextTrust.BUILT_IN
            ),
            content=system_fragment,
            tokens=usage["system"],
            limit=budget_limits["system"],
            truncated="system" in truncated,
            metadata=system_metadata,
        ),
        context_fragment(
            kind=ContextFragmentKind.TOOL_SCHEMA,
            source="runtime_tool_registry",
            trust=ContextTrust.MIXED,
            content=request.tool_schema_content,
            tokens=usage["tools"],
            limit=budget_limits["tools"],
            truncated="tools" in truncated,
            metadata={
                "tool_count": str(len(request.tool_schema_payload)),
                "registry_tool_count": str(request.registered_tool_count),
                "deferred_tool_count": str(
                    max(
                        0,
                        request.registered_tool_count
                        - len(request.tool_schema_payload),
                    )
                ),
                "provider_visible_tool_names": _tool_name_preview(
                    _tool_names(request.tool_schema_payload)
                ),
                "trust_boundary": "schemas_only",
            },
        ),
        context_fragment(
            kind=ContextFragmentKind.HISTORY,
            source="session_transcript",
            trust=ContextTrust.SESSION,
            content=history_content,
            tokens=usage["history"],
            limit=budget_limits["history"],
            truncated="history" in truncated,
            metadata={
                "message_count": str(max(0, len(compaction.messages) - 1)),
                "compacted": str(compaction.compacted).lower(),
                "tool_output_present": str(
                    any(item.get("role") == "tool" for item in compaction.messages)
                ).lower(),
                "untrusted_content_policy": "data_not_instructions",
            },
        ),
        context_fragment(
            kind=ContextFragmentKind.REPO_MAP,
            source="workspace_repository_map",
            trust=ContextTrust.PROJECT,
            content=repository_fragment,
            tokens=usage["repo_map"],
            limit=budget_limits["repo_map"],
            truncated="repo_map" in truncated,
            metadata={"untrusted_content_policy": "data_not_instructions"},
        ),
        context_fragment(
            kind=ContextFragmentKind.MEMORY,
            source="semantic_memory",
            trust=ContextTrust.MIXED,
            content=memory_fragment,
            tokens=usage["memory"],
            limit=budget_limits["memory"],
            truncated="memory" in truncated,
            metadata={"untrusted_content_policy": "data_not_instructions"},
        ),
    )
    return ContextAssemblyResult(
        messages=compaction.messages,
        compaction=compaction,
        budget_report=allocator.report(
            limits=budget_limits,
            usage=usage,
            truncated=truncated,
            fragments=fragments,
        ),
        usage_event=usage_event,
        tokens=tokens,
        maximum_input=maximum_input,
    )
