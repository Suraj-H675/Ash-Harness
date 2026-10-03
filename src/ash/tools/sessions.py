"""Read-only retrieval from durable sessions in the current workspace."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

from ash.core.redaction import redact_text
from ash.core.session import SessionStore
from ash.safety.guard import SafetyGuard
from ash.tools.base import BaseTool, ToolResult, count_output_tokens
from ash.ui.safe_text import terminal_safe_text


class SearchSessionsArgs(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)
    limit: int = Field(8, ge=1, le=20)

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("session search query cannot be blank")
        return normalized


class SearchSessionsTool(BaseTool):
    name = "search_sessions"
    description = (
        "Search prior user and assistant conversation text from durable Ash "
        "sessions in the current workspace. Returns bounded redacted excerpts."
    )
    args_schema = SearchSessionsArgs

    def __init__(
        self,
        safety_guard: SafetyGuard,
        store: SessionStore,
        project_root: Path,
    ) -> None:
        super().__init__(safety_guard)
        self._store = store
        self._project_root = project_root

    async def run(self, **kwargs) -> ToolResult:
        args = SearchSessionsArgs(**kwargs)
        hits = self._store.search_session_messages(
            project_path=self._project_root,
            query=args.query,
            limit=args.limit,
        )
        payload = {
            "query": redact_text(args.query),
            "matches": [
                {
                    "session_id": hit.session_id,
                    "title": terminal_safe_text(
                        hit.title or "(untitled)", single_line=True
                    ),
                    "message_id": hit.message_id,
                    "role": hit.role,
                    "timestamp": hit.timestamp.isoformat(),
                    "excerpt": terminal_safe_text(
                        redact_text(hit.excerpt), single_line=True
                    ),
                }
                for hit in hits
            ],
        }
        output = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return ToolResult(
            success=True,
            output=output,
            token_count=count_output_tokens(output),
        )
