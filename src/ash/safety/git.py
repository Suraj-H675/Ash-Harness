"""Safe invocation policy for Ash-owned read-only Git operations."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from ash.safety.environment import build_scrubbed_environment


_DIFF_COMMANDS = frozenset({"diff", "show"})


def read_only_git_args(arguments: Sequence[str]) -> list[str]:
    """Add Ash-local protections to a read-only Git invocation.

    Repository configuration is untrusted input for Ash-owned inspection.
    These options disable the Git extension points that can execute configured
    programs while retaining normal repository-reading behavior.
    """

    args = list(arguments)
    prefix = ["--no-pager", "-c", "core.fsmonitor=false"]
    if not args:
        return prefix
    command, remainder = args[0], args[1:]
    if command in _DIFF_COMMANDS:
        if "--no-ext-diff" not in remainder:
            remainder.insert(0, "--no-ext-diff")
        if "--no-textconv" not in remainder:
            remainder.insert(1, "--no-textconv")
    return [*prefix, command, *remainder]


def read_only_git_environment(allowed_names: Iterable[str] = ()) -> dict[str, str]:
    """Build an environment that keeps read-only Git observational."""

    environment = build_scrubbed_environment(allowed_names)
    environment.update(
        {
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return environment
