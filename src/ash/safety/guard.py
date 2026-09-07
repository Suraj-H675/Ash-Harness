"""Safety validation for filesystem paths and shell commands."""

import os
import re
import shlex
from pathlib import Path
from typing import Tuple

from ash.safety.path_scope import (
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
        (
            "rm -rf /",
            re.compile(
                r"\brm(?:\s+--?[\w-]+)*(?:\s+--)?\s+[\"']?/",
                re.IGNORECASE,
            ),
        ),
        ("mkfs", re.compile(r"\bmkfs(?:\.[\w-]+)?\b", re.IGNORECASE)),
        ("dd if=", re.compile(r"\bdd\b.*\bif=", re.IGNORECASE)),
        (
            "chmod -R 777 /",
            re.compile(
                r"\bchmod(?:\s+--?[\w-]+)*(?:\s+--)?\s+0?777"
                r"(?:\s+--?[\w-]+)*(?:\s+--)?\s+[\"']?/",
                re.IGNORECASE,
            ),
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

        scan_values = self._command_scan_values(command_str)
        default_patterns_enabled = self._default_patterns_enabled()
        if default_patterns_enabled:
            dynamic_executable = self._dynamic_executable_expansion(command_str)
            if dynamic_executable is not None:
                raise SafetyViolation(
                    "Blocked command pattern: dynamic executable expansion "
                    f"({dynamic_executable})"
                )
            ambiguous_pattern = self._ambiguous_destructive_posix_pattern(command_str)
            if ambiguous_pattern is not None:
                raise SafetyViolation(f"Blocked command pattern: {ambiguous_pattern}")
            for pattern, regex in self._DEFAULT_BLOCKLIST_PATTERNS:
                if any(regex.search(value) for value in scan_values):
                    reason = f"Blocked command pattern: {pattern}"
                    raise SafetyViolation(reason)

        default_patterns = {
            self._normalize_command(pattern) for pattern in self.default_blocklist()
        }
        for pattern in self.blocklist_commands:
            normalized_pattern = self._normalize_command(pattern)
            if default_patterns_enabled and normalized_pattern in default_patterns:
                continue
            if any(
                normalized_pattern in self._normalize_command(value)
                for value in scan_values
            ):
                reason = f"Blocked command pattern: {pattern}"
                raise SafetyViolation(reason)

        return True, ""

    @staticmethod
    def _normalize_command(command_str: str) -> str:
        return " ".join(command_str.casefold().split())

    def _default_patterns_enabled(self) -> bool:
        configured = {
            self._normalize_command(pattern) for pattern in self.blocklist_commands
        }
        defaults = {
            self._normalize_command(pattern) for pattern in self.default_blocklist()
        }
        return defaults.issubset(configured)

    @staticmethod
    def _posix_command_segments(command_str: str) -> tuple[tuple[str, ...], ...]:
        if os.name == "nt":
            return ()
        try:
            lexer = shlex.shlex(
                command_str,
                posix=True,
                punctuation_chars=";&|<>\n",
            )
            lexer.whitespace_split = True
            lexer.commenters = ""
            tokens = list(lexer)
        except ValueError:
            return ()

        punctuation = frozenset(";&|<>\n")
        segments: list[tuple[str, ...]] = []
        current: list[str] = []
        for token in tokens:
            if token and set(token) <= punctuation:
                if current:
                    segments.append(tuple(current))
                    current = []
                continue
            current.append(token)
        if current:
            segments.append(tuple(current))
        return tuple(segments)

    @staticmethod
    def _has_shell_expansion(token: str) -> bool:
        return "$" in token or "`" in token

    @staticmethod
    def _is_shell_assignment(token: str) -> bool:
        name, separator, _ = token.partition("=")
        return bool(
            separator
            and name
            and (name[0].isalpha() or name[0] == "_")
            and all(character.isalnum() or character == "_" for character in name)
        )

    @classmethod
    def _dynamic_executable_expansion(cls, command_str: str) -> str | None:
        """Reject a shell-expanded command name that could conceal the blocklist."""

        for segment in cls._posix_command_segments(command_str):
            executable = next(
                (token for token in segment if not cls._is_shell_assignment(token)),
                None,
            )
            if executable is not None and cls._has_shell_expansion(executable):
                return executable
        return None

    @classmethod
    def _ambiguous_destructive_posix_pattern(cls, command_str: str) -> str | None:
        """Fail closed when shell expansion can hide a protected absolute target."""

        for segment in cls._posix_command_segments(command_str):
            for index, token in enumerate(segment):
                command = token.rsplit("/", 1)[-1].casefold()
                arguments = segment[index + 1 :]
                if command == "rm" and cls._ambiguous_recursive_rm(arguments):
                    return "rm -rf /"
                if command == "chmod" and cls._ambiguous_recursive_chmod(arguments):
                    return "chmod -R 777 /"
        return None

    @classmethod
    def _ambiguous_recursive_rm(cls, arguments: tuple[str, ...]) -> bool:
        recursive = False
        force = False
        ambiguous_options = False
        targets: list[str] = []
        parse_options = True
        for token in arguments:
            if parse_options and token == "--":
                parse_options = False
                continue
            if parse_options and token.startswith("--"):
                option = token.split("=", 1)[0].casefold()
                recursive = recursive or option == "--recursive"
                force = force or option == "--force"
                continue
            if parse_options and token.startswith("-") and token != "-":
                option = token[1:].casefold()
                recursive = recursive or "r" in option
                force = force or "f" in option
                continue
            if parse_options and cls._has_shell_expansion(token):
                ambiguous_options = True
            targets.append(token)
        protected_target = "/" in targets
        ambiguous_target = any(cls._has_shell_expansion(t) for t in targets)
        return (recursive and force and (protected_target or ambiguous_target)) or (
            protected_target and ambiguous_options
        )

    @classmethod
    def _ambiguous_recursive_chmod(cls, arguments: tuple[str, ...]) -> bool:
        recursive = False
        mode_777 = False
        mode_seen = False
        ambiguous_mode = False
        ambiguous_recursive = False
        targets: list[str] = []
        parse_options = True
        for token in arguments:
            if parse_options and token == "--":
                parse_options = False
                continue
            if parse_options and token.startswith("--"):
                recursive = recursive or token.split("=", 1)[0].casefold() == "--recursive"
                continue
            if parse_options and token.startswith("-") and token != "-":
                recursive = recursive or "r" in token[1:].casefold()
                continue
            if not mode_seen and cls._has_shell_expansion(token):
                ambiguous_mode = True
                if parse_options:
                    ambiguous_recursive = True
                continue
            if not mode_seen and token != "/":
                mode_seen = True
                mode_777 = token in {"777", "0777"}
                continue
            targets.append(token)
        protected_target = "/" in targets
        ambiguous_target = any(cls._has_shell_expansion(target) for target in targets)
        return (
            recursive and mode_777 and (protected_target or ambiguous_target)
        ) or (
            protected_target
            and (
                (recursive and ambiguous_mode)
                or (mode_777 and ambiguous_recursive)
                or (ambiguous_mode and ambiguous_recursive)
            )
        )

    @staticmethod
    def _command_scan_values(command_str: str) -> tuple[str, ...]:
        """Return raw and dequoted POSIX shell spellings for blocklist checks."""

        values = [command_str]
        if os.name != "nt":
            try:
                lexical = " ".join(shlex.split(command_str, posix=True))
            except ValueError:
                lexical = ""
            if lexical and lexical != command_str:
                values.append(lexical)
        return tuple(values)
