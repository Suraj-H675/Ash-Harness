"""Pure, bounded literal-search evidence for owner-granted Crew investigations."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from ash.coworker_evidence import RepositoryEvidence

MAX_INSPECTION_MATCHES = 24
MAX_INSPECTION_SNIPPET_CHARS = 220


def literal_inspection(
    evidence: Sequence[RepositoryEvidence],
    *,
    query: str,
) -> str:
    """Return bounded, source-attributed matches without executing source or commands."""

    needle = query.casefold()
    matches: list[dict[str, Any]] = []
    total_matches = 0
    for item in evidence:
        for line_number, line in enumerate(item.text.splitlines(), 1):
            offset = line.casefold().find(needle)
            if offset < 0:
                continue
            total_matches += 1
            if len(matches) >= MAX_INSPECTION_MATCHES:
                continue
            start = max(0, offset - 70)
            end = min(len(line), start + MAX_INSPECTION_SNIPPET_CHARS)
            excerpt = line[start:end]
            matches.append({
                "path": item.relative_path,
                "sha256": item.sha256,
                "line": line_number,
                "excerpt": excerpt,
                "truncated_left": start > 0,
                "truncated_right": end < len(line),
            })
    return json.dumps(
        {
            "query": query,
            "files": [
                {"path": item.relative_path, "sha256": item.sha256}
                for item in evidence
            ],
            "matches": matches,
            "total_matching_lines": total_matches,
            "matches_omitted": max(0, total_matches - len(matches)),
        },
        ensure_ascii=False, separators=(",", ":"),
    )
