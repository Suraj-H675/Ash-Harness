"""Safety validation for filesystem paths and shell commands."""

import re
from pathlib import Path
from typing import Tuple

from safety.path_scope import (
    lexical_target_path,
    normalize_allowed_directories,
    normalize_project_root,
    path_has_link_component,
    path_is_in_scope,
    resolve_target_path,
)


class SafetyViolation(Exception):
    """Raised when a requested action violates Ash safety boundaries."""


class SafetyGuard:
    """Validate paths and command strings before consequential actions run."""

    LINUX_BLOCKLIST = (
        "rm -rf /",
        "mkfs",
        "dd if=",
        "chmod -r 777 /",
        "chmod -R 777 /",
        "chown",
        "shutdown",
        "reboot",
        "passwd",
    )
    WINDOWS_BLOCKLIST = (
        "format",
        "format-volume",
        "remove-item * -recurse",
        "remove-item -recurse c:\\",
        "del /s /q c:\\*",
        "diskpart",
        "bootrec",
        "net user",
        "reg delete",
    )
    _DEFAULT_BLOCKLIST_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
        ("rm -rf /", re.compile(r"\brm\s+-[a-z]*r[a-z]*f[a-z]*\s+/", re.IGNORECASE)),
        ("rm -rf /", re.compile(r"\brm\s+-[a-z]*f[a-z]*r[a-z]*\s+/", re.IGNORECASE)),
        ("mkfs", re.compile(r"\bmkfs(?:\.[\w-]+)?\b", re.IGNORECASE)),
        ("dd if=", re.compile(r"\bdd\b.*\bif=", re.IGNORECASE)),
        (
            "chmod -R 777 /",
            re.compile(r"\bchmod\s+-[a-z]*r[a-z]*\s+777\s+/", re.IGNORECASE),
        ),
        ("chown", re.compile(r"\bchown\b", re.IGNORECASE)),
        ("shutdown", re.compile(r"\bshutdown\b", re.IGNORECASE)),
        ("reboot", re.compile(r"\breboot\b", re.IGNORECASE)),
        ("passwd", re.compile(r"\bpasswd\b", re.IGNORECASE)),
        ("format", re.compile(r"\bformat(?:\.com)?\b", re.IGNORECASE)),
        ("format-volume", re.compile(r"\bformat-volume\b", re.IGNORECASE)),
        (
            "remove-item * -recurse",
            re.compile(r"\bremove-item\b.*(?:^|\s)-recurse\b", re.IGNORECASE),
        ),
        (
            "del /s /q c:\\*",
            re.compile(r"\bdel\b(?=.*\s/s\b)(?=.*\s/q\b).*c:\\", re.IGNORECASE),
        ),
        ("diskpart", re.compile(r"\bdiskpart\b", re.IGNORECASE)),
        ("bootrec", re.compile(r"\bbootrec\b", re.IGNORECASE)),
        ("net user", re.compile(r"\bnet\s+user\b", re.IGNORECASE)),
        ("reg delete", re.compile(r"\breg\s+delete\b", re.IGNORECASE)),
    )

    def __init__(
        self,
        project_root: Path,
        allowed_directories: list[str | Path] | None = None,
        blocklist_commands: list[str] | None = None,
    ) -> None:
        self.project_root = normalize_project_root(project_root)
        try:
            self.allowed_directories = normalize_allowed_directories(
                self.project_root,
                allowed_directories,
            )
        except ValueError as exc:
            raise SafetyViolation(str(exc)) from exc
        self.blocklist_commands = list(blocklist_commands or self.default_blocklist())

    @classmethod
    def default_blocklist(cls) -> tuple[str, ...]:
        return cls.LINUX_BLOCKLIST + cls.WINDOWS_BLOCKLIST

    def validate_path(self, target_path: str | Path) -> Path:
        """
        Resolve and validate a target path against the project safety boundary.

        Relative paths are interpreted from project_root. Existing symlink
        components are resolved, so symlink escapes are treated as out of scope.
        """

        resolved = resolve_target_path(target_path, self.project_root)
        if path_is_in_scope(resolved, self.project_root, self.allowed_directories):
            return resolved

        raise SafetyViolation(
            f"Access denied: path '{target_path}' is outside project scope."
        )

    def validate_mutation_path(self, target_path: str | Path) -> Path:
        """Validate a write target and reject link-based path indirection."""

        lexical = lexical_target_path(target_path, self.project_root)
        resolved = resolve_target_path(lexical, self.project_root)
        if not path_is_in_scope(
            resolved,
            self.project_root,
            self.allowed_directories,
        ) or not path_is_in_scope(
            lexical,
            self.project_root,
            self.allowed_directories,
        ):
            raise SafetyViolation(
                f"Access denied: path '{target_path}' is outside project scope."
            )
        link = path_has_link_component(lexical, self.project_root)
        if link is not None:
            raise SafetyViolation(
                f"Access denied: mutation path contains a symlink or junction: {link}"
            )
        return lexical

    def validate_command(self, command_str: str) -> Tuple[bool, str]:
        """
        Scan a command string for Linux and Windows destructive patterns.

        Returns (True, "") when no blocked pattern is found. Raises
        SafetyViolation when a blocklisted command pattern is present.
        """

        normalized = self._normalize_command(command_str)
        if self.blocklist_commands == list(self.default_blocklist()):
            for pattern, regex in self._DEFAULT_BLOCKLIST_PATTERNS:
                if regex.search(command_str):
                    reason = f"Blocked command pattern: {pattern}"
                    raise SafetyViolation(reason)
        else:
            for pattern in self.blocklist_commands:
                normalized_pattern = self._normalize_command(pattern)
                if normalized_pattern in normalized:
                    reason = f"Blocked command pattern: {pattern}"
                    raise SafetyViolation(reason)

        return True, ""

    @staticmethod
    def _normalize_command(command_str: str) -> str:
        return " ".join(command_str.casefold().split())
