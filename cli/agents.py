"""Top-level persisted subagent status and report inspection."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from agents.shared_state import SharedState


def list_agent_statuses(db_path: str | Path) -> list[dict[str, Any]]:
    state = SharedState(db_path)
    try:
        return [
            {
                **asdict(status),
                "last_heartbeat": status.last_heartbeat.isoformat(),
            }
            for status in state.list_agents()
        ]
    finally:
        state.close()


def list_agent_reports(db_path: str | Path, *, limit: int = 20) -> list[dict[str, Any]]:
    if limit < 1:
        raise ValueError("limit must be positive")
    state = SharedState(db_path)
    try:
        return [
            {
                "message_id": message.message_id,
                "sender_id": message.sender_id,
                "timestamp": message.timestamp.isoformat(),
                **message.content,
            }
            for message in state.fetch_messages(
                "lead",
                undelivered_only=False,
                limit=limit,
            )
            if message.message_type == "agent_report"
        ]
    finally:
        state.close()


def list_agent_messages(
    db_path: str | Path,
    *,
    recipient_id: str = "lead",
    undelivered_only: bool = True,
    limit: int = 50,
) -> list[dict[str, Any]]:
    if limit < 1:
        raise ValueError("limit must be positive")
    state = SharedState(db_path)
    try:
        return [
            {
                "message_id": message.message_id,
                "sender_id": message.sender_id,
                "recipient_id": message.recipient_id,
                "message_type": message.message_type,
                "content": message.content,
                "delivered": message.delivered,
                "timestamp": message.timestamp.isoformat(),
            }
            for message in state.fetch_messages(
                recipient_id,
                undelivered_only=undelivered_only,
                limit=limit,
            )
        ]
    finally:
        state.close()


def render_agent_statuses(
    statuses: list[dict[str, Any]],
    *,
    json_output: bool = False,
) -> str:
    if json_output:
        return json.dumps({"agents": statuses}, sort_keys=True)
    if not statuses:
        return "No subagents recorded."
    return "\n".join(
        f"{item['agent_id']} [{item['role']}] {item['status']}: "
        f"{item['current_task']}"
        for item in statuses
    )


def render_agent_reports(
    reports: list[dict[str, Any]],
    *,
    json_output: bool = False,
) -> str:
    if json_output:
        return json.dumps({"reports": reports}, sort_keys=True)
    if not reports:
        return "No subagent reports recorded."
    return "\n".join(
        f"{item.get('agent_id', item['sender_id'])} "
        f"[{item.get('role', 'unknown')}] "
        f"{'ok' if item.get('success') else 'failed'}: "
        f"{item.get('summary', '')}"
        for item in reports
    )


def render_agent_messages(
    messages: list[dict[str, Any]],
    *,
    json_output: bool = False,
) -> str:
    if json_output:
        return json.dumps({"messages": messages}, sort_keys=True)
    if not messages:
        return "No subagent messages recorded."
    return "\n".join(
        f"{item['message_id']} {item['sender_id']} -> {item['recipient_id']} "
        f"{item['message_type']} "
        f"{'delivered' if item['delivered'] else 'pending'}: "
        f"{_summarize_content(item['content'])}"
        for item in messages
    )


def _summarize_content(content: Any) -> str:
    if isinstance(content, dict):
        summary = content.get("summary") or content.get("content")
        if summary is not None:
            return str(summary)
    return json.dumps(content, ensure_ascii=False, sort_keys=True)
