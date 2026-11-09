"""Protect the external coding benchmark's scoring and oracle isolation."""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

from ash.core.session import StoredRuntimeEvent


_benchmark_path = Path(__file__).resolve().parents[2] / "devtools/coding_benchmark.py"
_spec = importlib.util.spec_from_file_location(
    "ash_coding_benchmark_under_test", _benchmark_path
)
assert _spec is not None and _spec.loader is not None
_benchmark = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _benchmark
_spec.loader.exec_module(_benchmark)

CALC_TEST = _benchmark.CALC_TEST
CHECKOUT = _benchmark.CHECKOUT
CHECKOUT_TEST = _benchmark.CHECKOUT_TEST
PRICING = _benchmark.PRICING
grade = _benchmark.grade
oracle_python = _benchmark.oracle_python
summarize_operational_events = _benchmark.summarize_operational_events


def test_coding_timeline_records_request_and_tool_timings_without_content() -> None:
    base = "2026-11-09T03:00:"

    def event(
        sequence: int, seconds: int, kind: str, **fields: object
    ) -> StoredRuntimeEvent:
        return StoredRuntimeEvent(
            sequence=sequence,
            event={
                "type": kind,
                "timestamp": f"{base}{seconds:02d}+00:00",
                **fields,
            },
        )

    evidence = summarize_operational_events(
        [
            event(1, 0, "turn.started", prompt="do not retain secret"),
            event(2, 1, "model.request.started", operation_id="r1", tool_count=10),
            event(
                3,
                4,
                "model.request.completed",
                operation_id="r1",
                prompt_tokens=4000,
                completion_tokens=50,
                output="SECRET-OUTPUT",
            ),
            event(
                4,
                5,
                "tool.completed",
                tool="replace_file_content",
                success=True,
                arguments={"file_path": "private.txt"},
            ),
            event(
                5,
                7,
                "tool.completed",
                tool="run_command",
                success=True,
                error="PRIVATE-ERROR",
            ),
            event(6, 8, "model.request.started", operation_id="r2", tool_count=10),
            event(
                7,
                10,
                "model.request.completed",
                operation_id="r2",
                prompt_tokens=4100,
                completion_tokens=30,
            ),
            event(8, 11, "turn.completed", stop_reason="completed", iterations=2),
        ]
    )

    assert evidence["model_requests"] == 2
    assert evidence["model_requests_completed"] == 2
    assert evidence["completed_model_seconds"] == 5
    assert evidence["last_successful_edit_seconds"] == 5
    assert evidence["last_successful_command_seconds"] == 7
    assert evidence["timeline_truncated"] is False
    assert evidence["timeline"][1]["tool_count"] == 10
    assert evidence["timeline"][-1]["at_seconds"] == 11
    serialized = str(evidence)
    assert "SECRET-OUTPUT" not in serialized
    assert "PRIVATE-ERROR" not in serialized
    assert "private.txt" not in serialized
    assert "do not retain secret" not in serialized


def test_coding_timeline_bounded_and_rejects_untrusted_metadata() -> None:
    records = [
        StoredRuntimeEvent(
            sequence=i + 1,
            event={
                "type": "tool.completed",
                "timestamp": "2026-11-09T03:00:00+00:00",
                "tool": "read_file" if i != 1 else "PRIVATE NAME with spaces",
                "success": True,
                "output": "DO_NOT_KEEP",
            },
        )
        for i in range(302)
    ]

    evidence = summarize_operational_events(records)

    assert len(evidence["timeline"]) == 300
    assert evidence["event_counts"]["tool.completed"] == 302
    assert evidence["timeline_truncated"] is True
    assert "tool" not in evidence["timeline"][1]
    assert "DO_NOT_KEEP" not in str(evidence)


def test_coding_timeline_redacts_sensitive_identifier_fields() -> None:
    events = [
        StoredRuntimeEvent(
            sequence=1,
            event={
                "type": "tool.completed",
                "timestamp": "2026-11-09T03:00:00+00:00",
                "tool": "sk-proj-abcdefghijklmnopqrstuvwxyz123456",
                "success": False,
            },
        ),
        StoredRuntimeEvent(
            sequence=2,
            event={
                "type": "model.request.completed",
                "timestamp": "T" * 100_000,
                "prompt_tokens": 42,
            },
        ),
    ]

    evidence = summarize_operational_events(events)

    assert evidence["timeline"] == [
        {
            "type": "tool.completed",
            "at_seconds": 0.0,
            "tool": "[REDACTED]",
            "success": False,
        }
    ]
    assert "sk-proj" not in str(evidence)
    assert evidence["model_requests_completed"] == 0


def test_marker_scoring_rejects_a_symlink_to_outside_the_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("ASH_FREE_MODEL_OK")
    (workspace / "marker.txt").symlink_to(outside)

    assert grade("marker", workspace) == {
        "correct": False,
        "oracle_error": "output is a symlink",
    }


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or shutil.which("bwrap") is None,
    reason="Linux Bubblewrap runtime required",
)
def test_coding_oracle_cannot_read_host_files_outside_fixture(tmp_path: Path) -> None:
    outside = tmp_path / "host-only.txt"
    outside.write_text("HOST_ONLY_SECRET")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "probe.py").write_text(
        "from pathlib import Path\n"
        f"outside = Path({str(outside)!r})\n"
        'print("ISOLATED" if not outside.exists() else outside.read_text())\n'
    )

    result = oracle_python(workspace, "probe.py")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ISOLATED"
    assert outside.read_text() == "HOST_ONLY_SECRET"


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or shutil.which("bwrap") is None,
    reason="Linux Bubblewrap runtime required",
)
def test_repair_oracle_rejects_constant_answer_that_passes_visible_test(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "calc.py").write_text("def add(a, b):\n    return 5\n")
    (workspace / "test_calc.py").write_bytes(CALC_TEST)

    outcome = grade("repair-test", workspace)

    assert outcome["correct"] is False


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or shutil.which("bwrap") is None,
    reason="Linux Bubblewrap runtime required",
)
def test_multifile_oracle_rejects_visible_test_only_hack(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "inventory").mkdir(parents=True)
    (workspace / "inventory/__init__.py").write_bytes(b"")
    (workspace / "inventory/pricing.py").write_bytes(PRICING)
    (workspace / "inventory/checkout.py").write_bytes(CHECKOUT)
    (workspace / "test_checkout.py").write_bytes(CHECKOUT_TEST)
    (workspace / "inventory/pricing.py").write_text(
        "from decimal import Decimal\n"
        "def discounted_total(prices: list[Decimal], discount_percent: Decimal) -> Decimal:\n"
        "    return Decimal('9.00')\n"
    )

    assert grade("multi-file", workspace)["correct"] is False

    (workspace / "inventory/pricing.py").write_text(
        "from decimal import Decimal\n"
        "def discounted_total(prices: list[Decimal], discount_percent: Decimal) -> Decimal:\n"
        "    subtotal = sum(prices, start=Decimal('0'))\n"
        "    return subtotal * (Decimal('100') - discount_percent) / Decimal('100')\n"
    )

    assert grade("multi-file", workspace)["correct"] is True
