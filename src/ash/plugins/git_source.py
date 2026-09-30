"""Canonical validation for plugin Git source URIs."""

from __future__ import annotations

import re
import urllib.parse

MAX_PLUGIN_GIT_SOURCE_CHARS = 2048
GIT_DIGEST_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


def validate_plugin_git_source(source: object) -> urllib.parse.SplitResult:
    """Validate one HTTPS or local-file plugin Git source and return its parsed URI."""

    if (
        not isinstance(source, str)
        or not source
        or len(source) > MAX_PLUGIN_GIT_SOURCE_CHARS
        or any(ord(character) < 32 or ord(character) == 127 for character in source)
        or any(character.isspace() for character in source)
    ):
        raise ValueError("plugin Git source URL is invalid")
    try:
        parsed = urllib.parse.urlsplit(source)
        scheme = parsed.scheme.casefold()
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("plugin Git source URL is invalid") from exc
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("plugin Git source URL cannot contain embedded credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("plugin Git source URL cannot contain a query or fragment")
    if scheme == "https":
        if not hostname:
            raise ValueError("plugin Git HTTPS source requires a hostname")
        if port == 0:
            raise ValueError("plugin Git HTTPS source port is invalid")
    elif scheme == "file":
        if hostname not in {None, "", "localhost"}:
            raise ValueError("plugin file source must be local")
        if port is not None:
            raise ValueError("plugin file source cannot specify a port")
    else:
        raise ValueError("plugin Git source must use HTTPS or a local file URI")
    return parsed
