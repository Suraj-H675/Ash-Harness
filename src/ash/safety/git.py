"""Safe invocation policy for Ash-owned read-only Git operations."""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from pathlib import Path

from ash.safety.environment import build_scrubbed_environment


_DIFF_COMMANDS = frozenset(
    {"diff", "show", "diff-tree", "diff-index", "diff-files"}
)
_ATTRIBUTE_COMMANDS = _DIFF_COMMANDS | {"status"}
_EXCLUDE_COMMANDS = frozenset({"status", "ls-files", "check-ignore"})
_MAILMAP_COMMANDS = frozenset({"log", "show"})


def read_only_git_args(
    arguments: Sequence[str],
    *,
    filter_drivers: Iterable[str] = (),
) -> list[str]:
    """Add Ash-local protections to a read-only Git invocation.

    Repository configuration is untrusted input for Ash-owned inspection.
    These options disable the Git extension points that can execute configured
    programs while retaining normal repository-reading behavior.
    """

    args = list(arguments)
    prefix = [
        "--no-pager",
        "--work-tree=.",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "log.showSignature=false",
    ]
    for driver in filter_drivers:
        prefix.extend(
            [
                "-c",
                f"filter.{driver}.clean=",
                "-c",
                f"filter.{driver}.smudge=",
                "-c",
                f"filter.{driver}.process=",
                "-c",
                f"filter.{driver}.required=false",
            ]
        )
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


def read_only_git_config_probe_args(arguments: Sequence[str]) -> list[str]:
    """Build a no-include provenance probe for repository-controlled config.

    System/global configuration belongs to the user or administrator and may
    intentionally reference host files.  Local/worktree configuration belongs
    to the repository boundary and must not introduce host-path indirections
    into Ash-owned read-only inspection.
    """

    command = str(arguments[0]).casefold() if arguments else ""
    patterns = [r"include\.path", r"includeif\..*\.path"]
    if command in _ATTRIBUTE_COMMANDS:
        patterns.append(r"core\.attributesfile")
    if command in _EXCLUDE_COMMANDS:
        patterns.append(r"core\.excludesfile")
    if command in _DIFF_COMMANDS:
        patterns.append(r"diff\.orderfile")
    if command in _MAILMAP_COMMANDS:
        patterns.append(r"mailmap\.file")
    expression = "^(" + "|".join(patterns) + ")$"
    return [
        "--no-pager",
        "--work-tree=.",
        "config",
        "--no-includes",
        "--show-scope",
        "--null",
        "--name-only",
        "--get-regexp",
        expression,
    ]


def read_only_git_untrusted_config_keys(output: str) -> tuple[str, ...]:
    """Parse ``git config --show-scope --null --name-only`` output.

    Return only names originating in the repository-owned local/worktree
    scopes.  Malformed output raises ``ValueError`` so callers can fail closed.
    """

    fields = output.split("\0")
    if fields and fields[-1] == "":
        fields.pop()
    if len(fields) % 2 != 0:
        raise ValueError("Git config provenance output is malformed")
    rejected: list[str] = []
    for index in range(0, len(fields), 2):
        scope = fields[index].casefold()
        key = fields[index + 1]
        if scope in {"local", "worktree"}:
            rejected.append(key)
    return tuple(dict.fromkeys(rejected))


def managed_worktree_git_config_probe_args() -> list[str]:
    """Probe repository-owned config that can escape worktree orchestration."""

    expression = (
        r"^(include\.path|includeif\..*\.path|"
        r"filter\..*\.(clean|smudge|process)|merge\..*\.driver|"
        r"diff\..*\.(command|textconv)|diff\.orderfile|mailmap\.file|"
        r"core\.(attributesfile|excludesfile|worktree))$"
    )
    return [
        "--no-pager",
        "--work-tree=.",
        "config",
        "--no-includes",
        "--show-scope",
        "--null",
        "--name-only",
        "--get-regexp",
        expression,
    ]


def managed_worktree_git_args(
    arguments: Sequence[str],
    *,
    hooks_path: str | Path,
) -> list[str]:
    """Apply invariant host-side policy to one Ash-managed worktree command."""

    args = list(arguments)
    command_index = _git_command_index(args)
    if command_index is None:
        leading = args
        command_and_tail: list[str] = []
    else:
        leading = args[:command_index]
        command_and_tail = args[command_index:]
    protected = [
        "--no-pager",
        "--work-tree=.",
        *leading,
        "-c",
        f"core.hooksPath={hooks_path}",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "log.showSignature=false",
        *command_and_tail,
    ]
    if command_index is None or not command_and_tail:
        return protected
    command = command_and_tail[0]
    if command not in _DIFF_COMMANDS:
        return protected
    actual_command_index = protected.index(command, len(leading) + 2)
    remainder = protected[actual_command_index + 1 :]
    additions: list[str] = []
    if "--no-ext-diff" not in remainder:
        additions.append("--no-ext-diff")
    if "--no-textconv" not in remainder:
        additions.append("--no-textconv")
    if additions:
        protected[actual_command_index + 1 : actual_command_index + 1] = additions
    return protected


def _git_command_index(arguments: Sequence[str]) -> int | None:
    """Locate the command after the small set of global options Ash emits."""

    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "-c":
            index += 2
            continue
        if argument == "--no-pager" or argument.startswith("--work-tree="):
            index += 1
            continue
        return index
    return None


def isolated_git_environment(home: str | Path) -> dict[str, str]:
    """Build a non-interactive Git environment isolated from user config/secrets."""

    isolated_home = str(Path(home))
    return build_scrubbed_environment(
        overrides={
            "HOME": isolated_home,
            "XDG_CONFIG_HOME": isolated_home,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_TEMPLATE_DIR": isolated_home,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": os.devnull,
            "SSH_ASKPASS": os.devnull,
            "GIT_PAGER": "cat",
        }
    )
