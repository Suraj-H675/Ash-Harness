"""Policy-gated unified patch application."""

from __future__ import annotations

import asyncio
import os
import shlex
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ash.safety.environment import build_scrubbed_environment, resolve_host_executable
from ash.safety.guard import SafetyGuard, SafetyViolation
from ash.safety.scoped_io import (
    ScopedIOError,
    read_scoped_bytes,
    workspace_mutation_lock,
)
from ash.sandbox.process_utils import (
    ProcessOutputLimitExceeded,
    ProcessTreeUnavailable,
    communicate_process,
    prepare_process_tree,
    prepare_scoped_process_launch,
    settle_process_tree_after_cancellation,
    spawn_process_tree,
    terminate_process_tree_after_timeout,
)
from ash.tools.base import BaseTool, ToolExecutionOutcome, ToolResult
from ash.tools.filesystem import (
    MAX_PYTHON_SYNTAX_CHECK_BYTES,
    BinaryContentError,
    TextEncodingError,
    _decode_text_bytes,
    _python_syntax_advisory,
)


class ApplyPatchArgs(BaseModel):
    patch: str = Field(
        ...,
        min_length=1,
        description=(
            "Git-style unified diff, with --- a/path, +++ b/path, and "
            "@@ -1 +1 @@ hunk headers; hunk line counts must match the "
            "context, added, and removed lines. Do not use the Codex "
            "*** Begin Patch / *** Update File format."
        ),
    )
    dry_run: bool = False


MAX_PATCH_BYTES = 8 * 1024 * 1024
MAX_PATCH_OUTPUT_BYTES = 100_000
PATCH_OUTPUT_LIMIT_EXIT = -2
MAX_PATCH_PYTHON_SYNTAX_FILES = 12


class ApplyPatchTool(BaseTool):
    name = "apply_patch"
    description = (
        "Edit multiple files in one operation with a git-style unified diff. "
        "A failed apply can leave "
        "partial changes; inspect affected files before retrying. Include "
        "--- a/path, +++ b/path, and @@ hunk headers; Codex-style "
        "*** Begin Patch blocks are not supported."
    )
    args_schema = ApplyPatchArgs

    async def run(self, **kwargs: Any) -> ToolResult:
        args = ApplyPatchArgs(**kwargs)
        patch_bytes = args.patch.encode("utf-8")
        if len(patch_bytes) > MAX_PATCH_BYTES:
            return ToolResult(
                success=False,
                output="",
                error=f"Patch exceeds {MAX_PATCH_BYTES} bytes",
            )
        try:
            paths = extract_patch_paths(args.patch, self.safety_guard)
        except (ValueError, SafetyViolation) as exc:
            return ToolResult(success=False, output="", error=f"Invalid patch: {exc}")
        if args.dry_run:
            try:
                check = await _git_apply(
                    self.safety_guard.project_root,
                    args.patch,
                    check=True,
                )
            except asyncio.TimeoutError as exc:
                return _patch_timeout_result(exc, mutation_may_have_started=False)
            if check[0] != 0:
                return ToolResult(
                    success=False,
                    output=check[1],
                    error=_patch_check_error(check[2]),
                )
            return ToolResult(
                success=True,
                output=f"Patch is valid for {len(paths)} file(s); no files changed.",
            )
        mutation_attempted = False
        try:
            with workspace_mutation_lock():
                check = await _git_apply(
                    self.safety_guard.project_root,
                    args.patch,
                    check=True,
                )
                if check[0] != 0:
                    return ToolResult(
                        success=False,
                        output=check[1],
                        error=_patch_check_error(check[2]),
                    )
                for path in paths:
                    self.safety_guard.validate_mutation_path(path)
                mutation_attempted = True
                applied = await _git_apply(
                    self.safety_guard.project_root,
                    args.patch,
                    check=False,
                )
        except asyncio.TimeoutError as exc:
            return _patch_timeout_result(
                exc,
                mutation_may_have_started=mutation_attempted,
            )
        except SafetyViolation as exc:
            return ToolResult(
                success=False,
                output="",
                error=f"Patch path changed after validation: {exc}",
            )
        except ScopedIOError as exc:
            return ToolResult(
                success=False,
                output="",
                error=f"Patch workspace mutation was refused: {exc}",
            )
        if applied[0] == PATCH_OUTPUT_LIMIT_EXIT:
            detail = applied[2].strip()
            error = (
                "Patch apply output exceeded its capture limit; the workspace may "
                "have changed. Inspect the affected files before retrying."
            )
            if detail:
                error += f" {detail}"
            return ToolResult(
                success=False,
                output=applied[1],
                error=error,
                outcome=ToolExecutionOutcome.UNKNOWN,
                truncated=True,
            )
        if applied[0] != 0:
            return ToolResult(
                success=False,
                output=applied[1],
                error=(
                    "Patch apply failed after execution was attempted; the "
                    "workspace may have changed partially. Inspect the affected "
                    f"files before retrying. {applied[2].strip()}"
                ),
                outcome=ToolExecutionOutcome.UNKNOWN,
            )
        result = ToolResult(
            success=True,
            output="Applied patch to: " + ", ".join(sorted(paths)),
        )
        return _post_patch_python_syntax_advisories(
            result, paths=paths, guard=self.safety_guard
        )


def _post_patch_python_syntax_advisories(
    result: ToolResult, *, paths: set[str], guard: SafetyGuard
) -> ToolResult:
    """Report bounded, non-executing syntax feedback for bulk Python edits.

    The patch already succeeded. The check is advisory and must not turn a
    known successful edit into a failed or ambiguously executed operation.
    Post-patch reads use the same no-follow scoped IO as normal file tools.
    """

    python_paths = sorted(
        path for path in paths if Path(path).suffix.casefold() in {".py", ".pyi"}
    )
    for relative in python_paths[:MAX_PATCH_PYTHON_SYNTAX_FILES]:
        path = guard.project_root / relative
        try:
            _, content = read_scoped_bytes(
                path, guard, max_bytes=MAX_PYTHON_SYNTAX_CHECK_BYTES
            )
            source, _ = _decode_text_bytes(content)
        except (OSError, SafetyViolation, BinaryContentError, TextEncodingError):
            # Deletions, oversized/non-text files and races are not syntax
            # failures. The separate patch outcome remains authoritative.
            continue
        feedback = _python_syntax_advisory(
            ToolResult(success=True, output=""),
            resolved_path=path,
            root=guard.project_root,
            new_text=source,
        )
        if feedback.diagnostics:
            result.diagnostics.extend(feedback.diagnostics)
            result.output += f"\n{relative}: {feedback.output.strip()}"
    if result.diagnostics:
        result.diagnostic_summary = {
            "python_syntax_errors": len(result.diagnostics)
        }
    omitted = len(python_paths) - MAX_PATCH_PYTHON_SYNTAX_FILES
    if omitted > 0:
        result.output += (
            f"\n[Syntax advisory limited to {MAX_PATCH_PYTHON_SYNTAX_FILES} "
            f"Python paths; {omitted} further path(s) not checked.]"
        )
        result.truncated = True
    return result


def _patch_timeout_result(
    timeout_error: TimeoutError,
    *,
    mutation_may_have_started: bool,
) -> ToolResult:
    if mutation_may_have_started:
        error = (
            "Patch apply timed out after 30 seconds; the workspace may have "
            "changed. Inspect the affected files before retrying."
        )
    else:
        error = "Patch validation timed out after 30 seconds; no files were changed."
    notes = getattr(timeout_error, "__notes__", ())
    if notes:
        error += " " + " ".join(notes)
    return ToolResult(
        success=False,
        output="",
        error=error,
        outcome=(
            ToolExecutionOutcome.UNKNOWN
            if mutation_may_have_started
            else ToolExecutionOutcome.COMPLETED
        ),
    )


def _patch_check_error(stderr: str) -> str:
    detail = stderr.strip()
    if "corrupt patch at" in detail:
        detail += (
            " Check each @@ -start,count +start,count @@ hunk header: context "
            "lines count on both sides, removed lines on the old side, and "
            "added lines on the new side. If necessary, use replace_file_edits "
            "instead of repeatedly submitting malformed diffs."
        )
    return f"Patch check failed: {detail}"


def extract_patch_paths(patch: str, guard: SafetyGuard) -> set[str]:
    if patch.lstrip().startswith("*** Begin Patch"):
        raise ValueError(
            "Codex-style *** Begin Patch blocks are not supported; provide "
            "a git-style unified diff with --- a/path, +++ b/path, and @@ hunks"
        )
    paths: set[str] = set()
    for line in patch.splitlines():
        candidate = ""
        if line.startswith(("--- ", "+++ ")):
            candidate = line[4:].split("\t", 1)[0].strip()
        elif line.startswith(("rename from ", "rename to ", "copy from ", "copy to ")):
            candidate = line.split(" ", 2)[2].strip()
        elif line.startswith("diff --git "):
            parts = shlex.split(line)
            if len(parts) != 4:
                raise ValueError("unsupported diff header")
            for item in parts[2:]:
                normalized = _normalize_patch_path(item)
                guard.validate_mutation_path(normalized)
                paths.add(normalized)
            continue
        if not candidate or candidate == "/dev/null":
            continue
        normalized = _normalize_patch_path(candidate)
        guard.validate_mutation_path(normalized)
        paths.add(normalized)
    if not paths:
        raise ValueError("no file paths were found")
    return paths


def _normalize_patch_path(path: str) -> str:
    if path.startswith(("a/", "b/")):
        path = path[2:]
    candidate = Path(path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"out-of-scope path: {path}")
    return candidate.as_posix()


async def _git_apply(cwd: Path, patch: str, *, check: bool) -> tuple[int, str, str]:
    expected_cwd_identity: tuple[int, int] | None = None
    try:
        metadata = os.stat(cwd)
    except OSError:
        pass
    else:
        expected_cwd_identity = (metadata.st_dev, metadata.st_ino)
    git = resolve_host_executable("git", workspace_root=cwd, cwd=cwd)
    if git is None:
        return 127, "", "git is unavailable outside the workspace"
    environment = build_scrubbed_environment(
        overrides={
            "GIT_PAGER": "cat",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": os.devnull,
            "SSH_ASKPASS": os.devnull,
        }
    )
    command = [git, "--no-pager", "--work-tree=.", "apply", "--whitespace=nowarn"]
    if check:
        command.append("--check")
    command.append("-")
    cwd_guard = SafetyGuard(cwd)
    try:
        with prepare_scoped_process_launch(
            command,
            cwd=cwd,
            guard=cwd_guard,
            search_path=os.environ.get("PATH"),
            expected_cwd_identity=expected_cwd_identity,
        ) as launch:
            try:
                process_tree_plan = prepare_process_tree(
                    workspace_root=cwd_guard.project_root
                )
            except ProcessTreeUnavailable as exc:
                return 126, "", f"git apply was not started: {exc}"
            process = await spawn_process_tree(
                process_tree_plan,
                *launch.argv,
                cwd=launch.cwd,
                env=environment,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                pass_fds=launch.pass_fds or None,
            )
    except (ProcessTreeUnavailable, SafetyViolation, ScopedIOError) as exc:
        return 126, "", f"git apply was not started: {exc}"
    try:
        stdout, stderr = await asyncio.wait_for(
            communicate_process(
                process,
                input_data=patch.encode("utf-8"),
                max_output_bytes=MAX_PATCH_OUTPUT_BYTES,
                process_tree_plan=process_tree_plan,
            ),
            timeout=30,
        )
    except ProcessOutputLimitExceeded as exc:
        detail = exc.stderr.decode("utf-8", errors="replace").strip()
        return (
            PATCH_OUTPUT_LIMIT_EXIT,
            exc.stdout.decode("utf-8", errors="replace"),
            (
                detail or f"git apply output exceeded {MAX_PATCH_OUTPUT_BYTES} bytes"
            )
            + (
                f"; process-tree cleanup failed: {exc.cleanup_error}"
                if exc.cleanup_error is not None
                else ""
            ),
        )
    except asyncio.TimeoutError as timeout_error:
        cleanup_error = await terminate_process_tree_after_timeout(
            process, plan=process_tree_plan
        )
        if cleanup_error is not None:
            timeout_error.add_note(f"Process-tree cleanup failed: {cleanup_error}")
        raise
    except asyncio.CancelledError as cancellation:
        cleanup_error, cleanup_cancelled = (
            await settle_process_tree_after_cancellation(
                process, plan=process_tree_plan
            )
        )
        if cleanup_error is not None:
            cancellation.add_note(f"Process-tree cleanup failed: {cleanup_error}")
        if cleanup_cancelled:
            cancellation.add_note("Process-tree cleanup was cancelled")
        raise
    except Exception as primary_error:
        cleanup_error, cleanup_cancelled = (
            await settle_process_tree_after_cancellation(
                process, plan=process_tree_plan
            )
        )
        if cleanup_error is not None:
            primary_error.add_note(f"Process-tree cleanup failed: {cleanup_error}")
        if cleanup_cancelled:
            cleanup_cancellation = asyncio.CancelledError()
            cleanup_cancellation.add_note(
                "git apply failed before process-tree cleanup was cancelled"
            )
            if cleanup_error is not None:
                cleanup_cancellation.add_note("Process-tree cleanup also failed")
            raise cleanup_cancellation from primary_error
        raise
    return (
        process.returncode if process.returncode is not None else -1,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )
