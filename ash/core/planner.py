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

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from ash.core.sprint import (
    ChecklistItem,
    ChecklistStatus,
    SprintContract,
    SprintExecution,
    SprintState,
)
from ash.providers.base import ProviderABC, StreamChunk, TokenCounterLike


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


@dataclass(frozen=True)
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

        raw = await self._collect_stream(messages)
        return parse_sprint_response(raw, fallback_goal=user_request.strip())

    async def _collect_stream(self, messages: list[dict[str, Any]]) -> str:
        chunks: list[str] = []
        async for chunk in self._provider.stream_chat(messages):
            chunks.append(chunk.content)
        return "".join(chunks)


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
    files_off_limits = tuple(_to_paths(_bullet_lines(fields.get("Files Off Limits", ""))))
    test_command = _first_nonempty_line(fields.get("Test Command", "")) or "pytest tests/"
    rollback_plan = _first_nonempty_line(fields.get("Rollback Plan", "")) or "git revert HEAD"

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
            mark = "x" if item.status in {ChecklistStatus.DONE, ChecklistStatus.SKIPPED} else " "
            lines.append(f"- [{mark}] {item.description}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


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
    return [m.group(1).strip() for m in _BULLET_ITEM.finditer(block) if m.group(1).strip()]


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
