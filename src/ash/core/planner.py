"""Planner — decompose a user request into a Sprint contract + checklist.

The planner is the V5 architect-side of the system: it sends the user
request to the LLM using the Architect Mode system prompt (see
SYSTEM_PROMPTS_AND_TEMPLATES.md section 2.1), then parses the
structured markdown response into a :class:`SprintContract` and a
list of :class:`ChecklistItem` records.

The :meth:`Planner.decompose` call is the only entry point the rest
of the codebase needs. It returns a fully populated
:class:`SprintExecution` that the loop can then ask the user to
approve, persist, and execute.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import Counter
from contextlib import aclosing
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Literal, Sequence
from uuid import uuid4

from ash.context.history import RequestBudgetError, RequestTokenBudget
from ash.core.sprint import (
    ChecklistItem,
    ChecklistStatus,
    SprintContract,
    SprintExecution,
    SprintState,
)
from ash.providers.base import (
    CompletionOutcome,
    CompletionStopCategory,
    ProviderABC,
    TokenCounterLike,
    completion_stop_category,
)
from ash.providers.failover import FailoverProvider


# --- architect mode system prompt (Section 2.1 of the spec) --------------


ARCHITECT_MODE_PROMPT = """You are Ash in Architect Mode. Your sole responsibility is to evaluate requirements, model the database schema, design module interfaces, and plan execution phases.

### Constraints
1. DO NOT write or edit source code files.
2. DO NOT execute commands.
3. Your output must strictly be a structural markdown design document detailing:
   - System components and boundaries.
   - API endpoints, data models, and database migrations.
   - Validation constraints.
   - An incremental execution checklist.

### User Request
{user_request}

### Workspace Context
- Project root: {project_root}
- Repository map (top files):
{repo_map}

### Output Format (REQUIRED)
Respond with markdown in EXACTLY this structure. Every section is required.
Do not include code; this is a design document, not implementation.

## Goal
<one-line restatement of the user's goal>

## Definition of Done
- <checkable success criterion 1>
- <checkable success criterion 2>
- ...

## Files in Scope
- <relative/path/that/may/be/touched>
- ...

## Files Off Limits
- <relative/path/that/must/not/be/touched>
- ...

## Test Command
<shell command to run tests after every significant change>

## Rollback Plan
<one-sentence plan for rolling back if things go wrong>

## Checklist

### Research
- [ ] <research step 1>
- [ ] <research step 2>

### Implementation
- [ ] <implementation step 1>
- [ ] <implementation step 2>

### Testing
- [ ] <testing step 1>
- [ ] <testing step 2>
"""


# --- parser ----------------------------------------------------------------


_SECTION_HEADING = re.compile(r"^###\s+(.+?)\s*$", re.MULTILINE)
_CHECKBOX_ITEM = re.compile(r"^\s*-\s*\[\s*([ xX])\s*\]\s*(.+?)\s*$", re.MULTILINE)
_BULLET_ITEM = re.compile(r"^\s*-\s+(.+?)\s*$", re.MULTILINE)
_FIELD_PATTERN = re.compile(
    r"^##\s+(?P<key>Goal|Definition of Done|Files in Scope|Files Off Limits|Test Command|Rollback Plan|Checklist)\s*$\n(?P<body>.*?)(?=^##\s+|\Z)",
    re.MULTILINE | re.DOTALL,
)


MAX_PLANNER_RESPONSE_BYTES = 256 * 1024


def _route_provider(provider: ProviderABC, *, first: bool = False) -> ProviderABC:
    if isinstance(provider, FailoverProvider) and provider.providers:
        if first:
            return provider.providers[0]
        index = provider.active_index
        if 0 <= index < len(provider.providers):
            return provider.providers[index]
    return provider


def _provider_route(
    provider: ProviderABC, configured_model: str | None = None
) -> str:
    family = str(getattr(provider, "provider_family", "custom") or "custom")
    model_name = str(provider.model_name)
    if family == "custom" and configured_model:
        configured_family, separator, configured_name = configured_model.partition("/")
        if separator and configured_name == model_name:
            return configured_model
    return f"{family}/{model_name}"


class PlannerError(RuntimeError):
    """Raised when the LLM response cannot be parsed into a sprint."""


# --- planner ---------------------------------------------------------------


class Planner:
    """
    Decompose a user request into a :class:`SprintExecution`.

    The planner is intentionally single-purpose: it does not execute
    anything. The :class:`~ash.core.loop.AshLoop` orchestrates the
    approve-then-execute flow.
    """

    def __init__(
        self,
        provider: ProviderABC,
        *,
        system_prompt: str | None = None,
        token_counter: TokenCounterLike | None = None,
    ) -> None:
        self._provider = provider
        self._system_prompt = system_prompt or ARCHITECT_MODE_PROMPT
        self._token_counter = token_counter

    def set_provider(self, provider: ProviderABC) -> None:
        """Follow the runtime's active provider after an atomic route switch."""

        self._provider = provider

    @property
    def provider(self) -> ProviderABC:
        """Return the provider whose capabilities and request path the planner uses."""

        return self._provider

    async def decompose(
        self,
        user_request: str,
        *,
        project_root: Path | None = None,
        repo_map_excerpt: str = "",
    ) -> SprintExecution:
        """
        Run the architect call and return a populated :class:`SprintExecution`.

        The provider's stream is collected into a single string; the
        parser is then responsible for splitting it into the contract
        and checklist fields. The :class:`Planner` does not surface
        its own UI; the loop is responsible for showing the result
        and asking for approval.
        """

        execution, _ = await self.decompose_with_usage(
            user_request, project_root=project_root, repo_map_excerpt=repo_map_excerpt
        )
        return execution

    async def decompose_with_usage(
        self,
        user_request: str,
        *,
        project_root: Path | None = None,
        repo_map_excerpt: str = "",
        timeout_seconds: float = 1800.0,
        total_token_budget: int = 0,
        max_completion_tokens: int | None = None,
        request_budget: RequestTokenBudget | None = None,
        configured_model: str | None = None,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
        context_event_sink: Callable[
            [str, int | None, int | None, str, bool], None
        ]
        | None = None,
    ) -> tuple[SprintExecution, CompletionOutcome]:
        """Return a sprint and its architect request usage for turn accounting."""

        if not user_request.strip():
            raise PlannerError("user_request is empty")

        prompt = self._system_prompt.format(
            user_request=user_request.strip(),
            project_root=str(project_root) if project_root else "(unspecified)",
            repo_map=repo_map_excerpt or "(no repo map supplied)",
        )

        messages: list[dict[str, Any]] = [
            {"role": "user", "content": prompt},
        ]
        context_id = str(uuid4())
        operation_id = str(uuid4())
        requested_provider = _route_provider(self._provider, first=True)
        requested_route = _provider_route(requested_provider, configured_model)
        requested_family = requested_route.split("/", 1)[0]
        estimator_route = (
            "unknown"
            if isinstance(self._provider, FailoverProvider)
            or self._token_counter is not None
            else _provider_route(_route_provider(self._provider), configured_model)
        )
        estimated_input: int | None = None
        context_event_emitted = False

        def emit_event(payload: dict[str, Any]) -> None:
            if event_sink is not None:
                try:
                    event_sink(payload)
                except Exception:  # noqa: BLE001 - event reporting is non-critical
                    pass

        def emit_context_event(
            current: int | None,
            maximum: int | None,
            *,
            available: bool,
        ) -> None:
            nonlocal context_event_emitted
            if context_event_sink is None or context_event_emitted:
                return
            try:
                context_event_sink(
                    context_id,
                    current,
                    maximum,
                    estimator_route,
                    available,
                )
            except Exception:  # noqa: BLE001 - event reporting is non-critical
                pass
            context_event_emitted = True

        def emit_rejection(
            *,
            cause: str,
            required: int,
            input_limit: int | None,
        ) -> None:
            emit_context_event(required, input_limit, available=True)
            emit_event(
                {
                    "type": "context.request.rejected",
                    "context_id": context_id,
                    "cause": cause,
                    "scope": "request_attempt",
                    "dispatch_status": "not_dispatched",
                    "required_input_tokens": max(0, int(required)),
                    "input_limit": input_limit,
                    "protected_current_turn": True,
                    "opaque_provider_replay_state_preserved": False,
                    "required_history_summary": False,
                    **(
                        {
                            "turn_tokens_used": 0,
                            "turn_token_limit": total_token_budget,
                        }
                        if total_token_budget > 0
                        else {}
                    ),
                }
            )

        if request_budget is not None:
            counter = (
                self._token_counter.count
                if self._token_counter is not None
                else self._provider.count_tokens
            )
            estimated_input = max(
                1, int(counter(json.dumps(messages, ensure_ascii=False)))
            )
            if estimated_input > request_budget.input_limit:
                emit_rejection(
                    cause="input_limit",
                    required=estimated_input,
                    input_limit=request_budget.input_limit,
                )
                raise RequestBudgetError(
                    f"planner prompt estimate ({estimated_input:,}) exceeds the "
                    f"provider input limit ({request_budget.input_limit:,})"
                )
            output_limit = request_budget.response_limit_for_request(
                estimated_input_tokens=estimated_input,
                remaining_turn_tokens=(
                    total_token_budget if total_token_budget > 0 else None
                ),
            )
            if output_limit is None:
                emit_rejection(
                    cause="turn_budget",
                    required=estimated_input,
                    input_limit=request_budget.input_limit,
                )
                raise PlannerError(
                    "planner input exceeds turn token budget or leaves no "
                    "response tokens within it"
                )
            self._provider.configure_max_tokens(output_limit)
        elif total_token_budget > 0:
            counter = (
                self._token_counter.count
                if self._token_counter is not None
                else self._provider.count_tokens
            )
            estimated_input = max(
                1, int(counter(json.dumps(messages, ensure_ascii=False)))
            )
            if estimated_input >= total_token_budget:
                emit_rejection(
                    cause="turn_budget",
                    required=estimated_input,
                    input_limit=None,
                )
                raise PlannerError("planner input exceeds turn token budget")
            output_limit = total_token_budget - estimated_input
            if max_completion_tokens is not None:
                output_limit = min(output_limit, max_completion_tokens)
            self._provider.configure_max_tokens(max(1, output_limit))
        emit_context_event(
            estimated_input,
            request_budget.input_limit if request_budget is not None else None,
            available=estimated_input is not None,
        )
        emit_event(
            {
                "type": "model.request.started",
                "operation_id": operation_id,
                "context_id": context_id,
                "provider": requested_family,
                "model": requested_route,
                "requested_route": requested_route,
                "effective_route": "unknown",
                "attempt": 1,
                "max_attempts": 1,
                "message_count": len(messages),
                "tool_count": 0,
                "native_tools": False,
            }
        )
        try:
            completion = await self._collect_stream(
                messages, timeout_seconds=timeout_seconds
            )
        except asyncio.CancelledError:
            emit_event(
                {
                    "type": "model.request.cancelled",
                    "operation_id": operation_id,
                    "context_id": context_id,
                    "provider": requested_family,
                    "model": requested_route,
                    "requested_route": requested_route,
                    "effective_route": "unknown",
                    "attempt": 1,
                }
            )
            raise
        except Exception as exc:
            emit_event(
                {
                    "type": "model.request.error",
                    "operation_id": operation_id,
                    "context_id": context_id,
                    "provider": requested_family,
                    "model": requested_route,
                    "requested_route": requested_route,
                    "effective_route": "unknown",
                    "attempt": 1,
                    "max_attempts": 1,
                    "status_code": None,
                    "retriable": False,
                    "failure_category": "unknown",
                    "error_type": type(exc).__name__,
                }
            )
            raise
        serving_provider = _route_provider(self._provider)
        effective_route = _provider_route(serving_provider, configured_model)
        emit_event(
            {
                "type": "model.request.completed",
                "operation_id": operation_id,
                "context_id": context_id,
                "provider": effective_route.split("/", 1)[0],
                "model": effective_route,
                "requested_route": requested_route,
                "effective_route": effective_route,
                "attempt": 1,
                "prompt_tokens": completion.prompt_tokens,
                "completion_tokens": completion.completion_tokens,
                "cache_read_tokens": completion.cache_read_tokens,
                "cache_write_tokens": completion.cache_write_tokens,
                "usage_source": completion.usage_source,
                "stop_category": completion_stop_category(
                    completion.stop_reason
                ).value,
            }
        )
        return (
            parse_sprint_response(completion.text, fallback_goal=user_request.strip()),
            completion,
        )

    async def _collect_stream(
        self, messages: list[dict[str, Any]], *, timeout_seconds: float
    ) -> CompletionOutcome:
        chunks: list[str] = []
        total_bytes = 0
        terminal = False
        stop_reason: str | None = None
        usage = (0, 0, 0, 0)
        usage_source: Literal["provider", "estimated", "unavailable"] = "unavailable"
        try:
            async with asyncio.timeout(timeout_seconds):
                async with aclosing(self._provider.stream_chat(messages)) as stream:
                    chunk_count = 0
                    async for chunk in stream:
                        chunk_count += 1
                        if chunk_count > 100_000:
                            raise PlannerError("planner stream exceeded 100000 chunks")
                        fragment = chunk.content
                        if not isinstance(fragment, str):
                            raise PlannerError("planner response must be text")
                        if terminal and (
                            fragment or chunk.tool_call_delta or chunk.native_tool_calls
                        ):
                            raise PlannerError(
                                "planner returned output after completion"
                            )
                        if chunk.tool_call_delta or chunk.native_tool_calls:
                            raise PlannerError(
                                "planner response cannot contain tool calls"
                            )
                        remaining = MAX_PLANNER_RESPONSE_BYTES - total_bytes
                        if len(fragment) > remaining:
                            raise PlannerError("planner response exceeds 256 KiB")
                        try:
                            fragment_bytes = len(fragment.encode("utf-8"))
                        except UnicodeEncodeError as exc:
                            raise PlannerError(
                                "planner response must be valid UTF-8"
                            ) from exc
                        if fragment_bytes > remaining:
                            raise PlannerError("planner response exceeds 256 KiB")
                        total_bytes += fragment_bytes
                        chunks.append(fragment)
                        if chunk.is_done:
                            terminal = True
                            if chunk.stop_reason is not None:
                                if (
                                    stop_reason is not None
                                    and chunk.stop_reason != stop_reason
                                ):
                                    raise PlannerError(
                                        "planner returned conflicting stop reasons"
                                    )
                                stop_reason = chunk.stop_reason
                            next_usage = (
                                chunk.prompt_tokens,
                                chunk.completion_tokens,
                                chunk.cache_read_tokens,
                                chunk.cache_write_tokens,
                            )
                            if (
                                usage_source == "unavailable"
                                or any(next_usage)
                                or chunk.usage_source != "unavailable"
                            ):
                                usage = next_usage
                                usage_source = chunk.usage_source
        except TimeoutError as exc:
            raise PlannerError("planner provider request timed out") from exc
        if not terminal:
            raise PlannerError("planner provider stream ended without completion")
        if completion_stop_category(stop_reason) is not CompletionStopCategory.COMPLETE:
            raise PlannerError(
                "planner provider response was incomplete or unsuccessful"
            )
        if usage_source == "unavailable" and any(usage):
            usage_source = "provider"
        if usage_source == "unavailable":
            counter = (
                self._token_counter.count
                if self._token_counter is not None
                else self._provider.count_tokens
            )
            usage = (
                max(0, int(counter(json.dumps(messages, ensure_ascii=False)))),
                max(0, int(counter("".join(chunks)))),
                0,
                0,
            )
            usage_source = "estimated"
        return CompletionOutcome(
            text="".join(chunks),
            prompt_tokens=usage[0],
            completion_tokens=usage[1],
            cache_read_tokens=usage[2],
            cache_write_tokens=usage[3],
            usage_source=usage_source,
            stop_reason=stop_reason,
        )


# --- public parser --------------------------------------------------------


def parse_sprint_response(raw: str, *, fallback_goal: str) -> SprintExecution:
    """
    Parse a markdown response from Architect Mode into a sprint.

    Tolerant of section ordering and missing fields: missing
    checklists produce an empty item list, missing DoD produces an
    empty tuple, etc. The fallback_goal is used when the response
    has no ``## Goal`` section so the contract still has a name.
    """

    fields = _split_sections(raw)

    goal = fields.get("Goal") or fallback_goal
    goal = _first_nonempty_line(goal) or fallback_goal

    definition_of_done = tuple(_bullet_lines(fields.get("Definition of Done", "")))
    files_in_scope = tuple(_to_paths(_bullet_lines(fields.get("Files in Scope", ""))))
    files_off_limits = tuple(
        _to_paths(_bullet_lines(fields.get("Files Off Limits", "")))
    )
    test_command = (
        _first_nonempty_line(fields.get("Test Command", "")) or "pytest tests/"
    )
    rollback_plan = (
        _first_nonempty_line(fields.get("Rollback Plan", "")) or "git revert HEAD"
    )

    items = _parse_checklist(fields.get("Checklist", ""))
    estimated_steps = len(items)

    contract = SprintContract(
        goal=goal,
        definition_of_done=definition_of_done,
        files_in_scope=files_in_scope,
        files_off_limits=files_off_limits,
        test_command=test_command,
        rollback_plan=rollback_plan,
        estimated_steps=estimated_steps,
    )
    execution = SprintExecution(contract=contract, state=SprintState.PLANNING)
    execution.set_items(items)
    return execution


def render_sprint_markdown(execution: SprintExecution) -> str:
    """Render a :class:`SprintExecution` back into the markdown format."""

    contract = execution.contract
    lines: list[str] = ["## Goal", contract.goal, ""]
    lines.append("## Definition of Done")
    if contract.definition_of_done:
        lines.extend(f"- {item}" for item in contract.definition_of_done)
    else:
        lines.append("- (none)")
    lines.append("")
    lines.append("## Files in Scope")
    if contract.files_in_scope:
        lines.extend(f"- {p}" for p in contract.files_in_scope)
    else:
        lines.append("- (none)")
    lines.append("")
    lines.append("## Files Off Limits")
    if contract.files_off_limits:
        lines.extend(f"- {p}" for p in contract.files_off_limits)
    else:
        lines.append("- (none)")
    lines.append("")
    lines.append("## Test Command")
    lines.append(contract.test_command)
    lines.append("")
    lines.append("## Rollback Plan")
    lines.append(contract.rollback_plan)
    lines.append("")
    lines.append("## Checklist")
    by_section: dict[str, list[ChecklistItem]] = {}
    for item in execution.items:
        by_section.setdefault(item.section, []).append(item)
    for section, items in by_section.items():
        lines.append(f"### {section}")
        for item in items:
            mark = (
                "x"
                if item.status in {ChecklistStatus.DONE, ChecklistStatus.SKIPPED}
                else " "
            )
            lines.append(f"- [{mark}] {item.description}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def apply_sprint_markdown_edit(execution: SprintExecution, markdown: str) -> None:
    """Validate edited plan Markdown and update an existing planning execution."""

    if execution.state != SprintState.PLANNING:
        raise PlannerError("only planning sprints can be edited")
    edit_bytes = 0
    for character in markdown:
        codepoint = ord(character)
        if 0xD800 <= codepoint <= 0xDFFF:
            raise PlannerError("edited plan must be valid UTF-8")
        if codepoint <= 0x7F:
            edit_bytes += 1
        elif codepoint <= 0x7FF:
            edit_bytes += 2
        elif codepoint <= 0xFFFF:
            edit_bytes += 3
        else:
            edit_bytes += 4
        if edit_bytes > MAX_PLANNER_RESPONSE_BYTES:
            raise PlannerError("edited plan exceeds 256 KiB")
    if "## Goal" not in markdown or "## Checklist" not in markdown:
        raise PlannerError("edited plan must include Goal and Checklist sections")
    edited = parse_sprint_response(markdown, fallback_goal=execution.contract.goal)
    if not edited.items:
        raise PlannerError("edited plan must contain at least one checklist item")
    _validate_plan_paths(edited.contract.files_in_scope, "Files in Scope")
    _validate_plan_paths(edited.contract.files_off_limits, "Files Off Limits")
    overlap = set(edited.contract.files_in_scope) & set(
        edited.contract.files_off_limits
    )
    if overlap:
        raise PlannerError(
            "plan paths cannot be both in scope and off limits: "
            + ", ".join(str(path) for path in sorted(overlap))
        )
    execution.contract = replace(
        edited.contract,
        contract_id=execution.contract.contract_id,
        max_cost_inr=execution.contract.max_cost_inr,
    )
    previous_counts = Counter(
        (item.section, item.description) for item in execution.items
    )
    edited_counts = Counter((item.section, item.description) for item in edited.items)
    previous_by_identity = {
        (item.section, item.description): item
        for item in execution.items
        if previous_counts[(item.section, item.description)] == 1
    }
    updated_items: list[ChecklistItem] = []
    for item in edited.items:
        identity = (item.section, item.description)
        previous = previous_by_identity.get(identity)
        if previous is not None and edited_counts[identity] == 1:
            item = replace(item, status=previous.status, notes=previous.notes)
        updated_items.append(item)
    execution.set_items(updated_items)


def _validate_plan_paths(paths: tuple[Path, ...], section: str) -> None:
    for path in paths:
        if path.is_absolute() or ".." in path.parts:
            raise PlannerError(f"{section} contains an unsafe path: {path}")


# --- internal helpers -----------------------------------------------------


def _split_sections(raw: str) -> dict[str, str]:
    """Map ``## Heading`` sections to their markdown body."""

    sections: dict[str, str] = {}
    for match in _FIELD_PATTERN.finditer(raw):
        key = match.group("key")
        body = match.group("body").strip()
        if key == "Checklist":
            sections[key] = body
        else:
            sections[key] = body.strip()
    return sections


def _bullet_lines(block: str) -> list[str]:
    return [
        m.group(1).strip() for m in _BULLET_ITEM.finditer(block) if m.group(1).strip()
    ]


def _to_paths(items: Sequence[str]) -> list[Path]:
    out: list[Path] = []
    for raw in items:
        text = raw.strip()
        if not text or text.lower() in {"(none)", "none"}:
            continue
        out.append(Path(text))
    return out


def _first_nonempty_line(block: str) -> str:
    for line in block.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _parse_checklist(body: str) -> list[ChecklistItem]:
    """
    Walk the ``## Checklist`` body, splitting on ``### Section``
    headings and emitting a :class:`ChecklistItem` per checkbox line.
    """

    items: list[ChecklistItem] = []
    current_section = "General"
    idx = 0
    for line in body.splitlines():
        section_match = _SECTION_HEADING.match(line)
        if section_match is not None:
            current_section = section_match.group(1).strip()
            continue
        checkbox = _CHECKBOX_ITEM.match(line)
        if checkbox is None:
            continue
        idx += 1
        description = checkbox.group(2).strip()
        items.append(
            ChecklistItem(
                idx=idx,
                section=current_section,
                description=description,
                status=ChecklistStatus.PENDING,
            )
        )
    return items


# --- JSON helpers (useful for callers that want to round-trip) -----------


def sprint_to_json(execution: SprintExecution) -> str:
    """Serialize a :class:`SprintExecution` to JSON."""

    return json.dumps(execution.to_dict(), indent=2, ensure_ascii=False)
