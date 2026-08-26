"""Versioned, atomic, project-scoped permission rules."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from ash.safety.trust import canonical_workspace


CURRENT_PERMISSION_RULE_VERSION = 2
MAX_RULE_FILE_BYTES = 1_000_000
MAX_MANAGED_RULE_FILES = 16
MAX_EXACT_VALUE_BYTES = 8192
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]+$")
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$", re.DOTALL)
_BULK_ARGUMENTS = frozenset(
    {
        "content",
        "edits",
        "patch",
        "replacement_content",
        "target_content",
    }
)


class PermissionGrantError(ValueError):
    """Raised when persisted permission policy cannot be trusted."""


class RuleEffect(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class MatchOperator(StrEnum):
    EXACT = "exact"
    CONTAINS = "contains"
    MAX = "max"
    IN_SET = "in"
    PREFIX = "prefix"
    COMMAND_PREFIX = "command_prefix"
    PATH_PREFIX = "path_prefix"
    SUFFIX = "suffix"
    DOMAIN = "domain"


_PATH_ARGUMENTS = frozenset({"file_path", "path", "cwd", "directory_path"})
_SUFFIX_ARGUMENTS = frozenset(
    {
        "file_path",
        "path",
        "directory_path",
        "target_path",
        "source_path",
        "destination_path",
    }
)
_DOMAIN_ARGUMENTS = frozenset({"url", "domain"})


def _validate_identifier(value: str, *, label: str) -> str:
    if not value or not _IDENTIFIER.fullmatch(value):
        raise PermissionGrantError(
            f"{label} must contain only letters, numbers, '.', '_', ':', or '-'"
        )
    return value


def _json_size(value: Any) -> int:
    try:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise PermissionGrantError("exact matcher value must be valid JSON") from exc
    return len(serialized.encode("utf-8"))


def _safe_command_tokens(command_line: str) -> tuple[str, ...] | None:
    """Tokenize one simple command; ambiguous shell programs never match."""

    if not command_line.strip():
        return None
    # Command substitution and process substitution can hide unrelated commands.
    if (
        "$(" in command_line
        or "`" in command_line
        or "<(" in command_line
        or ">(" in command_line
    ):
        return None
    try:
        detector = shlex.shlex(
            command_line,
            posix=True,
            punctuation_chars=";&|<>\n",
        )
        detector.whitespace_split = True
        detector.commenters = ""
        detected = list(detector)
    except ValueError:
        return None
    shell_punctuation = frozenset(";&|<>\n")
    if any(token and set(token) <= shell_punctuation for token in detected):
        return None
    try:
        tokens = shlex.split(command_line, posix=os.name != "nt")
    except ValueError:
        return None
    normalized = [
        token[1:-1]
        if len(token) >= 2 and token[0] == token[-1] and token[0] in {"'", '"'}
        else token
        for token in tokens
    ]
    while normalized and _ENV_ASSIGNMENT.fullmatch(normalized[0]):
        normalized.pop(0)
    return tuple(normalized) if normalized else None


def _hostname_from_candidate(candidate: str) -> str | None:
    """Extract a lowercase hostname from a URL or bare hostname safely."""

    from urllib.parse import urlsplit

    value = candidate.strip()
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname or ""
        if parsed.scheme and not hostname:
            return None
        if not parsed.scheme:
            candidate_host = value.split("/", 1)[0]
            if "@" in candidate_host or ":" in candidate_host or not candidate_host:
                return None
            hostname = candidate_host
        return hostname.casefold().rstrip(".")
    except ValueError:
        return None


def _lexical_path_prefix_matches(candidate: str, expected_prefix: str) -> bool:
    """Match a workspace-relative path without following links or allowing traversal."""

    normalized = candidate.replace("\\", "/").strip("/")
    if not normalized:
        return False
    parts: list[str] = []
    for part in normalized.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                return False
            parts.pop()
            continue
        parts.append(part)
    workspace_path = "/".join(parts) + "/"
    return workspace_path.startswith(expected_prefix)


@dataclass(frozen=True)
class ArgumentMatcher:
    """One argument condition in a permission rule."""

    argument: str
    operator: MatchOperator
    value: Any

    def __post_init__(self) -> None:
        _validate_identifier(self.argument, label="argument name")
        if self.operator == MatchOperator.EXACT:
            if _json_size(self.value) > MAX_EXACT_VALUE_BYTES:
                raise PermissionGrantError("exact matcher value exceeds 8 KiB")
            return
        if self.operator == MatchOperator.CONTAINS:
            self._validated_text("contains")
            return
        if self.operator == MatchOperator.MAX:
            if (
                not isinstance(self.value, (int, float))
                or isinstance(self.value, bool)
                or not isinstance(self.value, int)
            ):
                raise PermissionGrantError("max requires an integer threshold")
            if self.value < 1:
                raise PermissionGrantError("max requires a positive integer")
            return
        if self.operator == MatchOperator.IN_SET:
            if (
                not isinstance(self.value, (list, tuple))
                or not 1 <= len(self.value) <= 32
                or any(
                    not isinstance(choice, str)
                    or not choice.strip()
                    or len(choice) > 512
                    for choice in self.value
                )
            ):
                raise PermissionGrantError(
                    "in requires 1 to 32 non-empty string choices"
                )
            object.__setattr__(
                self, "value", tuple(choice.strip() for choice in self.value)
            )
        if self.operator == MatchOperator.PREFIX:
            if (
                not isinstance(self.value, str)
                or not self.value
                or len(self.value) > 2048
            ):
                raise PermissionGrantError(
                    "prefix matcher value must be a non-empty string up to 2048 characters"
                )
            return
        if self.operator == MatchOperator.COMMAND_PREFIX:
            if self.argument != "command_line":
                raise PermissionGrantError(
                    "command_prefix can only match the command_line argument"
                )
            if (
                not isinstance(self.value, (list, tuple))
                or not 1 <= len(self.value) <= 16
                or any(
                    not isinstance(token, str) or not token or len(token) > 512
                    for token in self.value
                )
            ):
                raise PermissionGrantError(
                    "command_prefix requires 1 to 16 non-empty argv tokens"
                )
            object.__setattr__(self, "value", tuple(self.value))
        if self.operator == MatchOperator.PATH_PREFIX:
            if self.argument not in _PATH_ARGUMENTS:
                allowed = ", ".join(sorted(_PATH_ARGUMENTS))
                raise PermissionGrantError(
                    f"path_prefix can only match workspace path arguments: {allowed}"
                )
            value = self._validated_text("path_prefix")
            if "\\" in value or "\x00" in value:
                raise PermissionGrantError(
                    "path_prefix must use POSIX-style workspace paths"
                )
            normalized = value.strip("/")
            if not normalized or ".." in normalized.split("/"):
                raise PermissionGrantError(
                    "path_prefix must be a relative workspace path without traversal"
                )
            object.__setattr__(self, "value", normalized + "/")
        if self.operator == MatchOperator.SUFFIX:
            if self.argument not in _SUFFIX_ARGUMENTS:
                raise PermissionGrantError(
                    "suffix can only match path-like string arguments"
                )
            suffix = self._validated_text("suffix")
            if (
                "\\" in suffix
                or "\x00" in suffix
                or "/" in suffix
                or not suffix.startswith(".")
                or "." in suffix[1:]
                or not suffix[1:]
            ):
                raise PermissionGrantError(
                    "suffix must be one POSIX filename extension, such as '.md'"
                )
            object.__setattr__(self, "value", suffix.casefold())
        if self.operator == MatchOperator.DOMAIN:
            if self.argument not in _DOMAIN_ARGUMENTS:
                allowed = ", ".join(sorted(_DOMAIN_ARGUMENTS))
                raise PermissionGrantError(
                    f"domain can only match URL or domain arguments: {allowed}"
                )
            domain = self._validated_text("domain").strip(".").casefold()
            if (
                "/" in domain
                or ":" in domain
                or "@" in domain
                or "*" in domain.replace("*.", "")
                or not domain
            ):
                raise PermissionGrantError(
                    "domain must be a hostname, optionally starting with '*.'"
                )
            labels = domain.split(".")
            if len(labels) < 2 or any(not label for label in labels):
                raise PermissionGrantError("domain must include at least two labels")
            object.__setattr__(self, "value", domain)

    def _validated_text(self, operator: str) -> str:
        if (
            not isinstance(self.value, str)
            or not self.value.strip()
            or len(self.value) > 2048
        ):
            raise PermissionGrantError(
                f"{operator} requires a non-empty string up to 2048 characters"
            )
        return self.value.strip()

    def matches(self, arguments: Mapping[str, Any]) -> bool:
        if self.argument not in arguments:
            return False
        candidate = arguments[self.argument]
        if self.operator == MatchOperator.EXACT:
            return candidate == self.value
        if self.operator == MatchOperator.CONTAINS:
            return isinstance(candidate, str) and str(self.value) in candidate
        if self.operator == MatchOperator.MAX:
            return (
                isinstance(candidate, int)
                and not isinstance(candidate, bool)
                and candidate <= int(self.value)
            )
        if self.operator == MatchOperator.IN_SET:
            return candidate in self.value if isinstance(candidate, str) else False
        if self.operator == MatchOperator.PREFIX:
            return isinstance(candidate, str) and candidate.startswith(self.value)
        if not isinstance(candidate, str):
            return False
        if self.operator == MatchOperator.PATH_PREFIX:
            return _lexical_path_prefix_matches(candidate, str(self.value))
        if self.operator == MatchOperator.SUFFIX:
            basename = candidate.replace("\\", "/").rstrip("/").split("/")[-1]
            return bool(basename) and basename.casefold().endswith(str(self.value))
        if self.operator == MatchOperator.DOMAIN:
            hostname = _hostname_from_candidate(candidate)
            expected_domain = str(self.value)
            if hostname is None:
                return False
            if expected_domain.startswith("*."):
                base_domain = expected_domain[2:]
                matches_domain = hostname == base_domain or hostname.endswith(
                    f".{base_domain}"
                )
            else:
                matches_domain = hostname == expected_domain or hostname.endswith(
                    f".{expected_domain}"
                )
            return matches_domain
        if not isinstance(candidate, str):
            return False
        tokens = _safe_command_tokens(candidate)
        if tokens is None or len(tokens) < len(self.value):
            return False
        expected = tuple(self.value) if isinstance(self.value, (list, tuple)) else ()
        if not all(isinstance(token, str) for token in expected):
            return False
        if os.name == "nt":
            return tuple(
                token.casefold() for token in tokens[: len(expected)]
            ) == tuple(token.casefold() for token in expected)
        return tokens[: len(expected)] == expected

    def as_payload(self) -> dict[str, Any]:
        value = list(self.value) if isinstance(self.value, tuple) else self.value
        return {
            "argument": self.argument,
            "operator": self.operator.value,
            "value": value,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> "ArgumentMatcher":
        if not isinstance(payload, dict):
            raise PermissionGrantError("permission matcher must be an object")
        try:
            argument = payload["argument"]
            operator = MatchOperator(payload["operator"])
            value = payload["value"]
        except (KeyError, ValueError) as exc:
            raise PermissionGrantError("permission matcher is invalid") from exc
        if not isinstance(argument, str):
            raise PermissionGrantError("permission matcher argument must be a string")
        return cls(argument, operator, value)


def _rule_identifier(
    effect: RuleEffect,
    tool_name: str,
    matchers: Iterable[ArgumentMatcher],
) -> str:
    content = {
        "effect": effect.value,
        "tool": tool_name,
        "matches": [matcher.as_payload() for matcher in matchers],
    }
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class PermissionRule:
    """A durable allow, ask, or deny rule with AND-combined matchers."""

    rule_id: str
    effect: RuleEffect
    tool_name: str
    matchers: tuple[ArgumentMatcher, ...] = ()

    @classmethod
    def create(
        cls,
        effect: str | RuleEffect,
        tool_name: str,
        matchers: Iterable[ArgumentMatcher] = (),
    ) -> "PermissionRule":
        normalized_effect = RuleEffect(effect)
        normalized_tool = _validate_identifier(tool_name, label="tool name")
        normalized_matchers = tuple(matchers)
        arguments = [matcher.argument for matcher in normalized_matchers]
        if len(arguments) != len(set(arguments)):
            raise PermissionGrantError(
                "a permission rule cannot match the same argument more than once"
            )
        return cls(
            _rule_identifier(
                normalized_effect,
                normalized_tool,
                normalized_matchers,
            ),
            normalized_effect,
            normalized_tool,
            normalized_matchers,
        )

    @property
    def scoped(self) -> bool:
        return bool(self.matchers)

    def matches(self, tool_name: str, arguments: Mapping[str, Any]) -> bool:
        return self.tool_name == tool_name and all(
            matcher.matches(arguments) for matcher in self.matchers
        )

    def as_payload(self) -> dict[str, Any]:
        return {
            "id": self.rule_id,
            "effect": self.effect.value,
            "tool": self.tool_name,
            "matches": [matcher.as_payload() for matcher in self.matchers],
        }

    @classmethod
    def from_payload(cls, payload: Any) -> "PermissionRule":
        if not isinstance(payload, dict):
            raise PermissionGrantError("permission rule must be an object")
        try:
            effect = payload["effect"]
            tool_name = payload["tool"]
            raw_matchers = payload.get("matches", [])
        except KeyError as exc:
            raise PermissionGrantError(
                "permission rule is missing required fields"
            ) from exc
        if not isinstance(effect, str) or not isinstance(tool_name, str):
            raise PermissionGrantError(
                "permission rule effect and tool must be strings"
            )
        if not isinstance(raw_matchers, list):
            raise PermissionGrantError("permission rule matches must be a list")
        try:
            rule = cls.create(
                effect,
                tool_name,
                (ArgumentMatcher.from_payload(item) for item in raw_matchers),
            )
        except ValueError as exc:
            raise PermissionGrantError("permission rule is invalid") from exc
        supplied_id = payload.get("id", rule.rule_id)
        if supplied_id != rule.rule_id:
            raise PermissionGrantError(
                f"permission rule id does not match its content: {supplied_id!r}"
            )
        return rule


def build_exact_scope_matchers(
    arguments: Mapping[str, Any],
) -> list[ArgumentMatcher]:
    """Build a durable exact scope while excluding bulk content payloads."""

    matchers: list[ArgumentMatcher] = []
    for argument, value in sorted(arguments.items()):
        if argument in _BULK_ARGUMENTS:
            continue
        matchers.append(ArgumentMatcher(argument, MatchOperator.EXACT, value))
    if not matchers:
        raise PermissionGrantError(
            "this call has no bounded non-content arguments to scope safely"
        )
    return matchers


def build_command_prefix_matcher(
    command_line: str,
    prefix_text: str,
) -> ArgumentMatcher:
    """Parse and verify a user-selected argv prefix for the current command."""

    try:
        prefix = shlex.split(prefix_text, posix=os.name != "nt")
    except ValueError as exc:
        raise PermissionGrantError(f"invalid command prefix: {exc}") from exc
    matcher = ArgumentMatcher(
        "command_line",
        MatchOperator.COMMAND_PREFIX,
        prefix,
    )
    if not matcher.matches({"command_line": command_line}):
        raise PermissionGrantError(
            "command prefix must match the current simple command; "
            "compound commands, redirection, and substitution require exact approval"
        )
    return matcher


def grants_path() -> Path:
    return Path.home() / ".ash" / "permission-grants.json"


def _read_payload(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"version": CURRENT_PERMISSION_RULE_VERSION, "workspaces": {}}
    try:
        if path.stat().st_size > MAX_RULE_FILE_BYTES:
            raise PermissionGrantError("permission rule file exceeds 1 MB")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except PermissionGrantError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PermissionGrantError(f"cannot read permission rule file: {exc}") from exc
    if not isinstance(payload, dict):
        raise PermissionGrantError("permission rule file root must be an object")
    version = payload.get("version", 1)
    if not isinstance(version, int) or version < 1:
        raise PermissionGrantError("permission rule file version is invalid")
    if version > CURRENT_PERMISSION_RULE_VERSION:
        raise PermissionGrantError(
            f"permission rule file version {version} is newer than supported "
            f"version {CURRENT_PERMISSION_RULE_VERSION}"
        )
    if not isinstance(payload.get("workspaces", {}), dict):
        raise PermissionGrantError("permission rule workspaces must be an object")
    return payload


def _normalized_workspaces(
    payload: Mapping[str, Any],
) -> dict[str, list[PermissionRule]]:
    version = int(payload.get("version", 1))
    raw_workspaces = payload.get("workspaces", {})
    assert isinstance(raw_workspaces, dict)
    normalized: dict[str, list[PermissionRule]] = {}
    for workspace, values in raw_workspaces.items():
        if not isinstance(workspace, str) or not isinstance(values, list):
            raise PermissionGrantError("permission workspace entry is invalid")
        if version == 1:
            if any(not isinstance(value, str) for value in values):
                raise PermissionGrantError(
                    "legacy permission grants must be tool names"
                )
            rules = [PermissionRule.create(RuleEffect.ALLOW, value) for value in values]
        else:
            rules = [PermissionRule.from_payload(value) for value in values]
        if len({rule.rule_id for rule in rules}) != len(rules):
            raise PermissionGrantError("permission workspace contains duplicate rules")
        if rules:
            normalized[workspace] = rules
    return normalized


def load_permission_rules(workspace: Path) -> list[PermissionRule]:
    payload = _read_payload(grants_path())
    workspaces = _normalized_workspaces(payload)
    return list(workspaces.get(canonical_workspace(workspace), ()))


def managed_policy_paths() -> tuple[Path, ...]:
    """Return administrator-owned policy files in increasing authority order."""

    if os.name == "nt":
        program_data = os.environ.get("ProgramData")
        return (Path(program_data) / "Ash" / "policy",) if program_data else ()
    return (
        Path("/etc/ash/policy"),
        Path("/Library/Application Support/Ash/policy"),
    )


def load_managed_permission_rules(workspace: Path) -> list[PermissionRule]:
    """Load immutable admin policy for one workspace.

    A malformed or unreadable file fails closed. Files are read in sorted order,
    with later paths taking precedence when rule identifiers conflict.
    """

    key = canonical_workspace(workspace)
    by_id: dict[str, PermissionRule] = {}
    for directory in managed_policy_paths():
        if not directory.exists():
            continue
        try:
            files = sorted(path for path in directory.iterdir() if path.is_file())
        except OSError as exc:
            raise PermissionGrantError(f"cannot read managed policy: {exc}") from exc
        if len(files) > MAX_MANAGED_RULE_FILES:
            raise PermissionGrantError("managed policy contains more than 16 files")
        for path in files:
            try:
                payload = _read_payload(path)
                workspaces = _normalized_workspaces(payload)
                rules = workspaces.get(key, ())
            except PermissionGrantError as exc:
                raise PermissionGrantError(
                    f"invalid managed policy {path}: {exc}"
                ) from exc
            for rule in rules:
                by_id[rule.rule_id] = rule
    return list(by_id.values())


def load_tool_grants(workspace: Path) -> set[str]:
    """Return legacy unscoped allow grants for compatibility."""

    return {
        rule.tool_name
        for rule in load_permission_rules(workspace)
        if rule.effect == RuleEffect.ALLOW and not rule.scoped
    }


@contextmanager
def _locked_rule_file(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    deadline = time.monotonic() + 3.0
    descriptor: int | None = None
    while descriptor is None:
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                stale = time.time() - lock_path.stat().st_mtime > 30
            except OSError:
                stale = False
            if stale:
                try:
                    lock_path.unlink()
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise PermissionGrantError("timed out waiting for permission rule lock")
            time.sleep(0.025)
    try:
        yield
    finally:
        os.close(descriptor)
        try:
            lock_path.unlink()
        except OSError:
            pass


def _write_workspaces(
    path: Path, workspaces: Mapping[str, list[PermissionRule]]
) -> None:
    payload = {
        "version": CURRENT_PERMISSION_RULE_VERSION,
        "workspaces": {
            workspace: [rule.as_payload() for rule in rules]
            for workspace, rules in sorted(workspaces.items())
            if rules
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _update_rules(
    workspace: Path,
    update: Callable[[list[PermissionRule]], list[PermissionRule]],
) -> list[PermissionRule]:
    path = grants_path()
    with _locked_rule_file(path):
        workspaces = _normalized_workspaces(_read_payload(path))
        key = canonical_workspace(workspace)
        rules = update(list(workspaces.get(key, ())))
        if rules:
            workspaces[key] = rules
        else:
            workspaces.pop(key, None)
        _write_workspaces(path, workspaces)
    return rules


def add_permission_rule(workspace: Path, rule: PermissionRule) -> list[PermissionRule]:
    def add(rules: list[PermissionRule]) -> list[PermissionRule]:
        if all(existing.rule_id != rule.rule_id for existing in rules):
            rules.append(rule)
        return rules

    return _update_rules(workspace, add)


def remove_permission_rule(workspace: Path, rule_id: str) -> list[PermissionRule]:
    _validate_identifier(rule_id, label="rule id")

    def remove(rules: list[PermissionRule]) -> list[PermissionRule]:
        remaining = [rule for rule in rules if rule.rule_id != rule_id]
        if len(remaining) == len(rules):
            raise PermissionGrantError(f"permission rule not found: {rule_id}")
        return remaining

    return _update_rules(workspace, remove)


def remove_permission_rules_for_tool(
    workspace: Path,
    tool_name: str,
    *,
    effect: RuleEffect | None = None,
) -> list[PermissionRule]:
    _validate_identifier(tool_name, label="tool name")

    def remove(rules: list[PermissionRule]) -> list[PermissionRule]:
        return [
            rule
            for rule in rules
            if not (
                rule.tool_name == tool_name
                and (effect is None or rule.effect == effect)
            )
        ]

    return _update_rules(workspace, remove)


def set_tool_grant(workspace: Path, tool_name: str, allowed: bool) -> None:
    """Maintain the version-1 unscoped allow API while writing version 2."""

    rule = PermissionRule.create(RuleEffect.ALLOW, tool_name)
    if allowed:
        add_permission_rule(workspace, rule)
        return

    def remove(rules: list[PermissionRule]) -> list[PermissionRule]:
        return [existing for existing in rules if existing.rule_id != rule.rule_id]

    _update_rules(workspace, remove)


def clear_permission_rules(workspace: Path) -> None:
    _update_rules(workspace, lambda rules: [])


def clear_tool_grants(workspace: Path) -> None:
    """Compatibility alias that clears every rule for one workspace."""

    clear_permission_rules(workspace)
