"""Isolated retention maintenance for the unattended automation worker."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from ash.automation.store import AutomationStore
from ash.core.session import SessionStore


def run_maintenance(request: dict[str, Any]) -> None:
    workspace = Path(_required_string(request, "workspace")).expanduser().resolve()
    database = Path(_required_string(request, "automation_db_path"))
    run_retention_days = _required_int(request, "run_retention_days", minimum=1)
    session_retention_days = _required_int(
        request, "session_retention_days", minimum=0
    )
    now = request.get("now")
    if not isinstance(now, (int, float)) or isinstance(now, bool):
        raise ValueError("now must be a numeric Unix timestamp")

    with AutomationStore(database, clock=lambda: float(now)) as store:
        store.prune_runs(
            workspace=workspace,
            older_than_days=run_retention_days,
        )

    session_store_path = request.get("session_store_path")
    if session_retention_days > 0 and session_store_path is not None:
        if not isinstance(session_store_path, str) or not session_store_path:
            raise ValueError("session_store_path must be a non-empty string or null")
        path = Path(session_store_path).expanduser().resolve()
        if path.exists():
            SessionStore(path).cleanup_sessions(
                session_retention_days,
                project_path=str(workspace),
            )


def _required_string(request: dict[str, Any], key: str) -> str:
    value = request.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _required_int(request: dict[str, Any], key: str, *, minimum: int) -> int:
    value = request.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{key} must be an integer greater than or equal to {minimum}")
    return value


def main() -> int:
    try:
        raw = json.load(sys.stdin)
        if not isinstance(raw, dict):
            raise ValueError("maintenance request must be a JSON object")
        run_maintenance(raw)
    except Exception as exc:  # noqa: BLE001 - process boundary reports one failure
        print(str(exc), file=sys.stderr)
        return 1
    print("ASH_AUTOMATION_MAINTENANCE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
