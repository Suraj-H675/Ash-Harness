"""Protect the external coding benchmark's scoring and oracle isolation."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

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
TASKS = _benchmark.TASKS
check_workspace_artifacts = _benchmark.check_workspace_artifacts
grade = _benchmark.grade
oracle_python = _benchmark.oracle_python
summarize_operational_events = _benchmark.summarize_operational_events


def _free_model_catalog() -> dict[str, object]:
    return {
        "data": [
            {
                "id": "cohere/north-mini-code:free",
                "pricing": {
                    "prompt": "0",
                    "completion": "0.000",
                    "request": "0",
                    "internal_reasoning": "0",
                },
                "supported_parameters": ["tools", "max_tokens"],
                "context_length": 256_000,
            }
        ]
    }


def test_free_openrouter_catalog_accepts_only_an_exact_zero_cost_tool_model() -> None:
    _benchmark.validate_free_openrouter_catalog(
        "cohere/north-mini-code:free", _free_model_catalog(), context_tokens=32_768
    )


@pytest.mark.parametrize(
    ("change", "error"),
    [
        (lambda item: item["pricing"].update(prompt="0.0001"), "zero-cost"),
        (lambda item: item["pricing"].update(completion="0.01"), "zero-cost"),
        (lambda item: item["pricing"].update(request="0.2"), "extra charge"),
        (lambda item: item["pricing"].update(internal_reasoning="0.2"), "extra charge"),
        (lambda item: item["pricing"].update(overrides=[{}]), "overrides"),
        (lambda item: item.update(supported_parameters=[]), "tool-call"),
        (lambda item: item.update(context_length=1000), "context window"),
        (lambda item: item.update(pricing={}), "pricing"),
    ],
)
def test_free_openrouter_catalog_rejects_unpriced_or_incompatible_variants(
    change: Callable[[dict[str, Any]], None], error: str
) -> None:
    catalog = _free_model_catalog()
    records = catalog["data"]
    assert isinstance(records, list) and isinstance(records[0], dict)
    change(records[0])
    with pytest.raises(ValueError, match=error):
        _benchmark.validate_free_openrouter_catalog(
            "cohere/north-mini-code:free", catalog, context_tokens=16_384
        )


@pytest.mark.parametrize(
    "model", ["cohere/north-mini-code", "openrouter/free", "../free:free"]
)
def test_free_openrouter_catalog_refuses_non_exact_model_ids(model: str) -> None:
    with pytest.raises(ValueError, match="exact vendor/model:free"):
        _benchmark.validate_free_openrouter_model(model)


def test_free_openrouter_catalog_refuses_unknown_or_duplicate_free_models() -> None:
    with pytest.raises(ValueError, match="not uniquely found"):
        _benchmark.validate_free_openrouter_catalog(
            "recursive/flint-v1.0:free", _free_model_catalog(), context_tokens=8192
        )
    catalog = _free_model_catalog()
    records = catalog["data"]
    assert isinstance(records, list)
    catalog["data"] = records + records
    with pytest.raises(ValueError, match="not uniquely found"):
        _benchmark.validate_free_openrouter_catalog(
            "cohere/north-mini-code:free", catalog, context_tokens=8192
        )


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (
            ["--openrouter-free-model", "cohere/north-mini-code"],
            "exact vendor/model:free",
        ),
        (
            ["--openrouter-free-model", "cohere/north-mini-code:free"],
            "OPENROUTER_API_KEY",
        ),
        (
            ["--openrouter-free-model", "cohere/north-mini-code:free", "--repeat", "3"],
            "1-2 explicitly selected trials",
        ),
        (
            [
                "--openrouter-free-model",
                "cohere/north-mini-code:free",
                "--endpoint",
                "http://127.0.0.1:9999",
            ],
            "cannot use --endpoint",
        ),
        (
            [
                "--openrouter-free-model",
                "cohere/north-mini-code:free",
                "--completion-tokens",
                "0",
            ],
            "--completion-tokens must be between",
        ),
    ],
)
def test_free_openrouter_cli_refuses_unapproved_requests_before_network_or_files(
    tmp_path: Path, arguments: list[str], message: str
) -> None:
    env = os.environ.copy()
    env.pop("OPENROUTER_API_KEY", None)
    target = tmp_path / "never-created"
    result = subprocess.run(
        [
            sys.executable,
            str(_benchmark_path),
            "--evidence-dir",
            str(target),
            "--task",
            "event-log",
            *arguments,
        ],
        cwd=_benchmark.REPO,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )
    assert result.returncode == 2, result.stderr
    assert message in result.stderr
    assert not target.exists()


def test_free_openrouter_runtime_requires_bubblewrap_before_catalog_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    evidence = tmp_path / "not-created"
    monkeypatch.setenv("OPENROUTER_API_KEY", "FAKE-ONLY-NO-INFERENCE")
    monkeypatch.setattr(_benchmark.shutil, "which", lambda name: None)

    def unexpected_catalog(*args: object, **kwargs: object) -> None:
        pytest.fail("No catalog or inference traffic before sandbox preflight")

    monkeypatch.setattr(_benchmark.urllib.request, "urlopen", unexpected_catalog)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "coding_benchmark.py",
            "--task",
            "event-log",
            "--openrouter-free-model",
            "cohere/north-mini-code:free",
            "--evidence-dir",
            str(evidence),
        ],
    )
    with pytest.raises(SystemExit) as raised:
        _benchmark.main()
    assert raised.value.code == 2
    assert "Bubblewrap is required" in capsys.readouterr().err
    assert not evidence.exists()


def test_free_openrouter_trial_keeps_key_in_child_environment_not_config_or_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "FAKE-ONLY-DO-NOT-LOG"

    def successful_setup(
        *args: object, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        setup_env = kwargs["env"]
        assert isinstance(setup_env, dict)
        assert "OPENROUTER_API_KEY" not in setup_env
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")

    def inspect_launch(argv: list[str], **kwargs: object) -> None:
        env = kwargs["env"]
        assert isinstance(env, dict)
        assert env["ASH_MODEL"] == "openrouter/cohere/north-mini-code:free"
        assert env["OPENROUTER_API_KEY"] == secret
        assert env["ASH_MAX_TURN_ITERATIONS"] == "16"
        assert env["ASH_MAX_COMPLETION_TOKENS"] == "4096"
        assert secret not in " ".join(argv)
        config = (Path(env["HOME"]) / ".ash/ash.toml").read_text()
        assert config == 'sandbox_backend = "native"\n'
        assert secret not in config
        raise RuntimeError("captured isolated free-model invocation")

    monkeypatch.setattr(_benchmark, "command", successful_setup)
    monkeypatch.setattr(_benchmark.subprocess, "Popen", inspect_launch)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    with pytest.raises(RuntimeError, match="captured isolated"):
        _benchmark.run_trial(
            "event-log",
            evidence,
            "https://openrouter.ai/api/v1",
            120,
            None,
            remote_model_id="cohere/north-mini-code:free",
            remote_api_key=secret,
            completion_tokens=4096,
        )
    assert all(
        secret.encode() not in path.read_bytes()
        for path in evidence.rglob("*")
        if path.is_file()
    )


def test_free_openrouter_cli_preflight_is_public_zero_cost_and_secret_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    key = "SYNTHETIC-DO-NOT-LOG-OPENROUTER-KEY"
    evidence = tmp_path / "trial"
    catalog_urls: list[str] = []

    def public_catalog(url: str, *, timeout: int) -> io.BytesIO:
        assert timeout == 10
        catalog_urls.append(url)
        return io.BytesIO(json.dumps(_free_model_catalog()).encode())

    def git_stub(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if argv[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(argv, 0, stdout="test-head\n", stderr="")
        if argv[:2] == ["git", "diff"]:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        raise AssertionError("Unexpected benchmark setup command")

    def trial_stub(
        task: str,
        path: Path,
        endpoint: str,
        timeout: int,
        model_hash: str | None,
        *args: object,
        **kwargs: object,
    ) -> dict[str, object]:
        assert task == "event-log"
        assert path == evidence
        assert endpoint == "https://openrouter.ai/api/v1"
        assert timeout == 285
        assert model_hash is None
        assert kwargs["remote_api_key"] == key
        assert kwargs["remote_model_id"] == "cohere/north-mini-code:free"
        assert kwargs["completion_tokens"] == 4_096
        return {
            "task": task,
            "passed": True,
            "task_correct": True,
            "required_tools_succeeded": True,
            "elapsed_seconds": 0.01,
        }

    monkeypatch.setattr(_benchmark.urllib.request, "urlopen", public_catalog)
    monkeypatch.setattr(_benchmark, "command", git_stub)
    monkeypatch.setattr(_benchmark, "run_trial", trial_stub)
    monkeypatch.setattr(_benchmark.shutil, "which", lambda name: "/usr/bin/bwrap")
    monkeypatch.setenv("OPENROUTER_API_KEY", key)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "coding_benchmark.py",
            "--task",
            "event-log",
            "--openrouter-free-model",
            "cohere/north-mini-code:free",
            "--context-tokens",
            "32768",
            "--evidence-dir",
            str(evidence),
        ],
    )
    assert _benchmark.main() == 0
    assert catalog_urls == [_benchmark.OPENROUTER_CATALOG_URL]
    summary = (evidence / "summary.json").read_text()
    assert (
        json.loads(summary)["provider_model"]
        == "openrouter/cohere/north-mini-code:free"
    )
    assert json.loads(summary)["context_tokens"] == 32_768
    assert key not in summary + capsys.readouterr().out


def test_coding_benchmark_default_and_custom_turn_budget_reach_isolated_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[tuple[str, str]] = []

    def successful_setup(
        *args: object, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")

    def capture_launch(*args: object, **kwargs: object) -> None:
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        captured.append(
            (
                environment["ASH_MAX_TURN_TOTAL_TOKENS"],
                environment["ASH_MAX_CONTEXT_TOKENS"],
            )
        )
        raise RuntimeError("captured disposable evaluation environment")

    monkeypatch.setattr(_benchmark, "command", successful_setup)
    monkeypatch.setattr(_benchmark.subprocess, "Popen", capture_launch)
    for index, (budget, context) in enumerate(
        ((40_000, 8_192), (80_000, 16_384)), start=1
    ):
        evidence = tmp_path / f"evidence-{index}"
        evidence.mkdir()
        with pytest.raises(RuntimeError, match="captured disposable"):
            _benchmark.run_trial(
                "marker",
                evidence,
                "http://127.0.0.1:18105",
                90,
                None,
                turn_token_budget=budget,
                context_tokens=context,
            )
    assert captured == [("40000", "8192"), ("80000", "16384")]


@pytest.mark.parametrize("budget", ["1999", "250001"])
def test_coding_benchmark_rejects_out_of_range_turn_budget_before_connection(
    tmp_path: Path, budget: str
) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(_benchmark_path),
            "--evidence-dir",
            str(tmp_path / "never-created"),
            "--turn-token-budget",
            budget,
        ],
        cwd=_benchmark.REPO,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 2
    assert "--turn-token-budget must be between" in result.stderr
    assert not (tmp_path / "never-created").exists()


@pytest.mark.parametrize("context", ["4095", "16385"])
def test_coding_benchmark_rejects_out_of_range_context_before_connection(
    tmp_path: Path, context: str
) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(_benchmark_path),
            "--evidence-dir",
            str(tmp_path / "never-created"),
            "--context-tokens",
            context,
        ],
        cwd=_benchmark.REPO,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 2
    assert "--context-tokens must be between" in result.stderr
    assert not (tmp_path / "never-created").exists()


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


def test_coding_timeline_measures_only_successful_terminal_requests_and_tools() -> None:
    def event(sequence: int, kind: str, **fields: object) -> StoredRuntimeEvent:
        return StoredRuntimeEvent(
            sequence=sequence,
            event={
                "type": kind,
                "timestamp": f"2026-11-09T03:00:{sequence:02d}+00:00",
                **fields,
            },
        )

    evidence = summarize_operational_events(
        [
            event(1, "model.request.started", operation_id="failed"),
            event(3, "model.request.error", operation_id="failed", retriable=True),
            event(4, "model.request.started", operation_id="success"),
            event(8, "model.request.completed", operation_id="success"),
            event(9, "tool.started", operation_id="edited", tool="write_file"),
            event(
                11,
                "tool.completed",
                operation_id="edited",
                tool="write_file",
                success=True,
            ),
            event(12, "tool.started", operation_id="blocked", tool="run_command"),
            event(
                15,
                "tool.error",
                operation_id="blocked",
                tool="run_command",
                success=False,
            ),
        ]
    )

    assert evidence["model_requests"] == 2
    assert evidence["model_requests_completed"] == 1
    assert evidence["model_requests_still_open"] == 0
    assert evidence["completed_model_seconds"] == 4
    assert evidence["unsuccessful_model_seconds"] == 2
    assert evidence["completed_tool_seconds"] == 2
    assert evidence["tool_calls_still_open"] == 0
    assert evidence["last_successful_edit_seconds"] == 10
    assert evidence["last_successful_command_seconds"] is None
    assert [item.get("duration_seconds") for item in evidence["timeline"]] == [
        None,
        2,
        None,
        4,
        None,
        2,
        None,
        3,
    ]


def test_coding_timeline_splits_first_output_wait_and_remaining_generation() -> None:
    def event(sequence: int, kind: str, **fields: object) -> StoredRuntimeEvent:
        return StoredRuntimeEvent(
            sequence=sequence,
            event={
                "type": kind,
                "timestamp": f"2026-11-09T03:00:{sequence:02d}+00:00",
                **fields,
            },
        )

    evidence = summarize_operational_events(
        [
            event(1, "model.request.started", operation_id="a"),
            event(
                4,
                "model.request.first_output",
                operation_id="a",
                content="PRIVATE-OUTPUT",
            ),
            event(5, "model.request.first_output", operation_id="a"),
            event(9, "model.request.completed", operation_id="a"),
            event(10, "model.request.started", operation_id="without-output"),
            event(13, "model.request.completed", operation_id="without-output"),
            event(14, "model.request.started", operation_id="failed"),
            event(15, "model.request.first_output", operation_id="failed"),
            event(17, "model.request.error", operation_id="failed"),
        ]
    )

    assert evidence["model_requests_completed"] == 2
    assert evidence["completed_model_seconds"] == 11
    assert evidence["completed_requests_with_first_output"] == 1
    assert evidence["completed_model_first_output_seconds"] == 3
    assert evidence["completed_model_after_first_output_seconds"] == 5
    assert evidence["unsuccessful_model_seconds"] == 3
    assert evidence["timeline"][1]["since_start_seconds"] == 3
    assert "since_start_seconds" not in evidence["timeline"][2]
    assert evidence["timeline"][3]["first_output_seconds"] == 3
    assert evidence["timeline"][3]["after_first_output_seconds"] == 5
    assert "first_output_seconds" not in evidence["timeline"][5]
    assert "PRIVATE-OUTPUT" not in str(evidence)


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


def test_two_file_scoring_requires_both_original_sources_to_change(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "keep.txt").write_bytes(_benchmark.PRESERVED)
    task = TASKS["two-file"]
    assert task.editable_files == frozenset({"billing/tax.py", "billing/receipt.py"})
    for filename, content in task.initial_files.items():
        target = workspace / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    protected, changed, files = check_workspace_artifacts("two-file", workspace)
    assert protected is True
    assert changed is False
    assert set(files) == {"keep.txt", *task.initial_files}

    tax_path = workspace / "billing/tax.py"
    tax_path.write_bytes(
        _benchmark.BILLING_TAX.replace(
            b"return subtotal + subtotal * percent / Decimal('100')",
            b"return subtotal * percent / Decimal('100')",
        )
    )
    assert check_workspace_artifacts("two-file", workspace)[:2] == (True, False)

    receipt_path = workspace / "billing/receipt.py"
    receipt_path.write_bytes(_benchmark.BILLING_RECEIPT.replace(b":.1f}", b":.2f}"))
    assert check_workspace_artifacts("two-file", workspace)[:2] == (True, True)

    (workspace / "test_billing.py").write_text("pass\n", encoding="utf-8")
    assert check_workspace_artifacts("two-file", workspace)[:2] == (False, True)


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or shutil.which("bwrap") is None,
    reason="Linux Bubblewrap runtime required",
)
def test_two_file_oracle_rejects_partial_repairs_and_accepts_complete_fix(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for filename, content in TASKS["two-file"].initial_files.items():
        target = workspace / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    assert grade("two-file", workspace)["correct"] is False
    tax_path = workspace / "billing/tax.py"
    tax_path.write_bytes(
        _benchmark.BILLING_TAX.replace(
            b"return subtotal + subtotal * percent / Decimal('100')",
            b"return subtotal * percent / Decimal('100')",
        )
    )
    assert grade("two-file", workspace)["correct"] is False
    receipt_path = workspace / "billing/receipt.py"
    receipt_path.write_bytes(_benchmark.BILLING_RECEIPT.replace(b":.1f}", b":.2f}"))
    assert grade("two-file", workspace)["correct"] is True


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or shutil.which("bwrap") is None,
    reason="Linux Bubblewrap runtime required",
)
def test_event_log_oracle_rejects_partial_repairs_and_accepts_three_module_fix(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task = TASKS["event-log"]
    assert task.editable_files == frozenset(
        {"eventlog/parser.py", "eventlog/aggregate.py", "eventlog/render.py"}
    )
    (workspace / "keep.txt").write_bytes(_benchmark.PRESERVED)
    for filename, data in task.initial_files.items():
        path = workspace / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    assert grade("event-log", workspace)["correct"] is False
    assert check_workspace_artifacts("event-log", workspace)[:2] == (True, False)

    parser = workspace / "eventlog/parser.py"
    parser.write_bytes(
        _benchmark.EVENTLOG_PARSER.replace(
            b"        event = json.loads(line)  # BUG: one bad record aborts the stream\n"
            b"        if event.get('request_id'):\n",
            b"        try:\n"
            b"            event = json.loads(line)\n"
            b"        except json.JSONDecodeError:\n"
            b"            continue\n"
            b"        if (isinstance(event, dict) and "
            b"isinstance(event.get('request_id'), str) "
            b"and event['request_id']):\n",
        )
    )
    assert grade("event-log", workspace)["correct"] is False
    assert check_workspace_artifacts("event-log", workspace)[:2] == (True, False)

    aggregate = workspace / "eventlog/aggregate.py"
    aggregate.write_bytes(
        _benchmark.EVENTLOG_AGGREGATE.replace(
            b"not isinstance(ms, int)", b"type(ms) is not int"
        ).replace(b"ms or previous", b"ms")
    )
    assert grade("event-log", workspace)["correct"] is False
    assert check_workspace_artifacts("event-log", workspace)[:2] == (True, False)

    renderer = workspace / "eventlog/render.py"
    renderer.write_bytes(_benchmark.EVENTLOG_RENDER.replace(b", reverse=True", b""))
    final = grade("event-log", workspace)
    assert final["correct"] is True, final
    assert final["visible_oracle_exit"] == 0
    assert final["hidden_oracle_exit"] == 0
    protected, sources_changed, files = check_workspace_artifacts(
        "event-log", workspace
    )
    assert protected is True
    assert sources_changed is True
    assert set(files) == {"keep.txt", *task.initial_files}

    # A visible-example-specific shortcut must not satisfy the independent
    # oracle, even if the exposed integration test remains green.
    correct_parser = parser.read_bytes()
    parser.write_bytes(
        correct_parser.replace(
            b"and event['request_id']):",
            b"and event['request_id'] in ('a', 'z')):",
        )
    )
    assert oracle_python(workspace, "test_eventlog.py").returncode == 0
    assert grade("event-log", workspace)["correct"] is False
    parser.write_bytes(correct_parser)

    # The real Qwen3.5-4B trial on 2026-11-10 fixed malformed JSON and null
    # values, but incorrectly assumed every other JSON value was an object.
    # Passing the visible test must not certify that near-correct repair.
    parser.write_bytes(
        correct_parser.replace(
            b"if (isinstance(event, dict) and "
            b"isinstance(event.get('request_id'), str) "
            b"and event['request_id']):",
            b"if event and event.get('request_id'):",
        )
    )
    assert oracle_python(workspace, "test_eventlog.py").returncode == 0
    partial = grade("event-log", workspace)
    assert partial["visible_oracle_exit"] == 0
    assert partial["hidden_oracle_exit"] != 0
    assert partial["correct"] is False
    parser.write_bytes(correct_parser)

    # The external hidden oracle is not a substitute for the protected-file
    # contract: changing tests must still invalidate the scored workspace.
    (workspace / "test_eventlog.py").write_bytes(b"pass\n")
    assert check_workspace_artifacts("event-log", workspace)[:2] == (False, True)


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
