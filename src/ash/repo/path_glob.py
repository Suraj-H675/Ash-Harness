"""Path-segment-aware globs for code discovery and symbol lookup."""

from __future__ import annotations

import fnmatch


def matches_relative_glob(relative_path: str, pattern: str) -> bool:
    """Match full relative paths; only an entire ``**`` segment crosses directories."""

    parts = relative_path.split("/")
    states = [False] * (len(parts) + 1)
    states[0] = True
    for segment in pattern.split("/"):
        if segment == ".":
            # A leading './' or interior '/./' is an explicit current-dir
            # component, not a filename segment that must be consumed.
            continue
        if segment == "**":
            # Globstar may match zero or more complete directory segments.
            reached = False
            for index in range(len(states)):
                reached = reached or states[index]
                states[index] = reached
        else:
            states = [False] + [
                states[index] and fnmatch.fnmatchcase(part, segment)
                for index, part in enumerate(parts)
            ]
        if not any(states):
            return False
    return states[-1]
