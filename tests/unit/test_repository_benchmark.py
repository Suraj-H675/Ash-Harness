"""Offline checks for sealed upstream-repository coding evaluations."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import re
import signal
import stat
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from ash.core.session import ToolCallRecord


_devtools = Path(__file__).resolve().parents[2] / "devtools"
if str(_devtools) not in sys.path:
    sys.path.insert(0, str(_devtools))
_path = _devtools / "repository_benchmark.py"
_spec = importlib.util.spec_from_file_location("repository_benchmark_under_test", _path)
assert _spec is not None and _spec.loader is not None
bench = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = bench
_spec.loader.exec_module(bench)


def test_interrupted_repository_trial_reaps_isolated_child_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals: list[int] = []

    class FakeChild:
        pid = 32111
        returncode = -9
        waits = 0

        def poll(self) -> None:
            return None

        def wait(self, *, timeout: int) -> int:
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired("ash", timeout)
            return -9

    child = FakeChild()
    monkeypatch.setattr(bench.os, "killpg", lambda pid, sig: signals.append(sig))
    assert bench._stop_evaluation_child(child) == -9
    assert signals == [signal.SIGTERM, signal.SIGKILL]


def test_finished_repository_child_is_never_signalled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Finished:
        returncode = 0

        def poll(self) -> int:
            return 0

    def unexpected_signal(pid: int, signum: int) -> None:
        pytest.fail("completed trial must not receive process-group signals")

    monkeypatch.setattr(bench.os, "killpg", unexpected_signal)
    assert bench._stop_evaluation_child(Finished()) == 0


def test_coding_receipt_aggregates_verified_restarted_session_lineage(
    tmp_path: Path,
) -> None:
    from ash.core.checkpoints import recover_interrupted_turns
    from ash.core.session import SessionStore
    from ash.safety.guard import SafetyGuard

    store = SessionStore(tmp_path / "paired.db")
    parent = store.create_session(str(tmp_path), model="openai/gpt-6-luna")
    store.start_turn(parent.session_id, "original", "fix two modules")
    store.save_tool_call(
        parent.session_id,
        ToolCallRecord(
            call_id="edited", tool_name="replace_file_content",
            arguments={"file_path": "module.py"}, approved=True,
            dispatched=True, executed=True, success=True,
            result="edited", error=None,
            timestamp=datetime.now(timezone.utc),
        ),
        turn_id="original",
    )
    store.save_turn_usage_snapshot(
        "original", {"prompt_tokens": 100, "completion_tokens": 10}
    )
    store.interrupt_turn("original")
    recover_interrupted_turns(
        store, SafetyGuard(tmp_path), parent.session_id,
        include_quiescent_interrupted=True,
    )
    child = store.restart_context_session(
        parent.session_id, expected_project_path=tmp_path,
        user_handoff="Continue the two-file repair", automatic=True,
    )
    store.start_turn(child.session_id, "continuation", "verify existing edit")
    store.save_tool_call(
        child.session_id,
        ToolCallRecord(
            call_id="tested", tool_name="run_command",
            arguments={"command_line": "python qa_visible.py"}, approved=True,
            dispatched=True, executed=True, success=True,
            result="passed", error=None,
            timestamp=datetime.now(timezone.utc),
        ),
        turn_id="continuation",
    )
    store.save_turn_usage_snapshot(
        "continuation", {"prompt_tokens": 60, "completion_tokens": 15}
    )
    store.complete_turn("continuation")
    summary = bench.collect_coding_session_receipts(store, tmp_path)
    assert summary["audit_ok"] is True
    assert summary["lineage_ok"] is True
    assert summary["context_continuations"] == 1
    assert summary["session_count"] == 2
    assert [entry["tool"] for entry in summary["calls"]] == [
        "replace_file_content", "run_command"
    ]
    assert summary["usage"] == {"prompt_tokens": 160, "completion_tokens": 25}


def test_size_telemetry_excludes_tool_payload_and_private_file_path() -> None:
    record = ToolCallRecord(
        call_id="read-1",
        tool_name="read_file",
        arguments={
            "file_path": "/private/SECRET-SOURCE-PATH",
            "start_line": 40,
            "max_chars": 32768,
            "expected_sha256": "PRIVATE-HASH",
        },
        approved=True,
        executed=True,
        dispatched=True,
        result="SECRET-FILE-CONTENT" * 150,
        timestamp=datetime.now(timezone.utc),
    )
    metrics = bench.tool_call_size_metrics(record)
    assert metrics == {
        "result_chars": len(record.result),
        "explicit_max_chars": True,
        "max_chars": 32768,
        "start_line": 40,
    }
    assert "SECRET" not in repr(metrics)
    assert "PRIVATE" not in repr(metrics)
    assert not bench.tool_call_size_metrics(
        record.model_copy(
            update={"arguments": {"file_path": "SECRET", "max_chars": "SECRET"}}
        )
    ).get("max_chars")


def test_headless_error_category_uses_only_stable_values_without_error_payload(
    tmp_path: Path,
) -> None:
    stdout = tmp_path / "ash.stdout"
    stdout.write_text(
        json.dumps(
            {
                "type": "error",
                "error": {
                    "category": "context",
                    "message": "PRIVATE-REASONING-DATA",
                    "edited_files": ["PRIVATE-SOURCE-PATH"],
                },
            }
        )
        + "\n"
    )
    result = bench.headless_error_category(stdout)
    assert result == "context"
    assert "PRIVATE" not in repr(result)
    stdout.write_text(json.dumps({"type": "turn.completed"}) + "\n")
    assert bench.headless_error_category(stdout) is None


@pytest.mark.parametrize(
    "payload",
    [
        '{"type":"error","error":{"category":"PRIVATE-SECRET"}}\n',
        '{"type":"error","error":{"category":123}}\n',
        '{"type":"error","error":{"category":"tool"}}\nnot json\n',
        '{"type":"error","error":null}\n',
        "[1,2,3]\n",
        "x" * (256 * 1024 + 1),
    ],
)
def test_headless_error_category_refuses_untrusted_or_large_output(
    tmp_path: Path, payload: str
) -> None:
    stdout = tmp_path / "ash.stdout"
    stdout.write_text(payload)
    assert bench.headless_error_category(stdout) is None


@pytest.mark.parametrize(
    ("task_id", "upstream", "editable"),
    [
        (
            "click-clirunner-context-settings",
            "https://github.com/pallets/click.git",
            {"src/click/testing.py"},
        ),
        (
            "toolz-partition-invalid-length",
            "https://github.com/pytoolz/toolz.git",
            {"toolz/itertoolz.py"},
        ),
        (
            "dateutil-strict-numeric-fields",
            "https://github.com/dateutil/dateutil.git",
            {"src/dateutil/parser/isoparser.py", "src/dateutil/tz/tz.py"},
        ),
        (
            "click-resource-exception",
            "https://github.com/pallets/click.git",
            {"src/click/core.py"},
        ),
        (
            "h11-chunk-footer",
            "https://github.com/python-hyper/h11.git",
            {"h11/_readers.py"},
        ),
        (
            "urllib3-header-sanitization",
            "https://github.com/urllib3/urllib3.git",
            {"src/urllib3/connection.py"},
        ),
        (
            "urllib3-proxy-redirect",
            "https://github.com/urllib3/urllib3.git",
            {"src/urllib3/connectionpool.py", "src/urllib3/poolmanager.py"},
        ),
        (
            "urllib3-url-auth-feature",
            "https://github.com/urllib3/urllib3.git",
            {"src/urllib3/util/url.py"},
        ),
        (
            "attrs-generator-setattr-hooks",
            "https://github.com/python-attrs/attrs.git",
            {"src/attr/_compat.py", "src/attr/_make.py"},
        ),
        (
            "packaging-exception-pickle",
            "https://github.com/pypa/packaging.git",
            {"src/packaging/metadata.py", "src/packaging/dependency_groups.py"},
        ),
        (
            "tomlkit-comment-control-validation",
            "https://github.com/python-poetry/tomlkit.git",
            {"tomlkit/_utils.py", "tomlkit/api.py", "tomlkit/items.py"},
        ),
        (
            "pluggy-role-module-split",
            "https://github.com/pytest-dev/pluggy.git",
            {
                "src/pluggy/_caller.py",
                "src/pluggy/_callers.py",
                "src/pluggy/_config.py",
                "src/pluggy/_decorators.py",
                "src/pluggy/_execution.py",
                "src/pluggy/_hooks.py",
                "src/pluggy/_impl.py",
            },
        ),
        (
            "boltons-bulk-ordered-collections",
            "https://github.com/mahmoud/boltons.git",
            {"boltons/listutils.py", "boltons/setutils.py"},
        ),
        (
            "pyflakes-functional-typing-fields",
            "https://github.com/PyCQA/pyflakes.git",
            {"pyflakes/checker.py"},
        ),
        (
            "pytest-mixed-indirect-fixture-precedence",
            "https://github.com/pytest-dev/pytest.git",
            {"src/_pytest/fixtures.py", "src/_pytest/python.py"},
        ),
    ],
)
def test_upstream_tasks_are_pinned_and_oracles_sealed(
    task_id: str, upstream: str, editable: set[str]
) -> None:
    task = bench.TASKS[task_id]
    assert task.upstream == upstream
    assert task.base_sha != task.reference_fix_sha
    assert re.fullmatch("[a-f0-9]{40}", task.base_sha)
    assert re.fullmatch("[a-f0-9]{40}", task.reference_fix_sha)
    assert task.editable == editable
    assert task.visible_script == "qa_visible.py"
    assert task.visible_script not in task.editable
    assert task.command_grant == "python3 -B qa_visible.py"
    assert task.hidden_source != task.visible_source
    assert task.hidden_source not in task.prompt
    compile(task.visible_source, task.visible_script, "exec")
    compile(task.hidden_source, "<sealed-oracle>", "exec")


def test_pyflakes_r16_pins_and_model_inputs() -> None:
    task_id = "pyflakes-functional-typing-fields"
    assert [name for name in bench.TASKS if name.startswith("pyflakes-")] == [task_id]
    task = bench.TASKS[task_id]
    assert task.source == "PyCQA/pyflakes"
    assert task.base_sha == "9f0a7f9c8feb4d086a918a708bb778b38ce7a248"
    assert task.reference_fix_sha == "5f2af952d81a8bead32d3ab91a60f7efbc6d8aea"

    for model_input in (
        task.prompt,
        bench.shared_user_prompt(task),
        task.visible_source,
    ):
        assert task.hidden_source not in model_input
        assert "PYFLAKES_TYPING_FIELDS_HIDDEN_OK" not in model_input
        assert task.reference_fix_sha not in model_input


def test_pytest_r17_provenance_scope_and_runtime_contract() -> None:
    task = bench.TASKS["pytest-mixed-indirect-fixture-precedence"]
    assert task.source == "pytest-dev/pytest"
    assert task.base_sha == "fd47511afeb024d2fbad9257d1b44310672c6a0f"
    assert task.reference_fix_sha == "6b6502e94b78b41951b70265a84187a419e8ce8c"
    assert task.upstream_pr == "https://github.com/pytest-dev/pytest/pull/13976"
    assert task.license == "MIT"
    assert task.require_all_editable_changed is False
    assert task.expected_reference_paths == frozenset(
        {
            "src/_pytest/fixtures.py",
            "src/_pytest/python.py",
            "testing/python/collect.py",
            "testing/python/metafunc.py",
        }
    )
    assert task.runtime is bench.PYTEST_R17_RUNTIME
    assert hashlib.sha256(task.visible_source.encode()).hexdigest() == (
        "1594f93a769f5201c3295b657f6e193191bbb8a835db08fe68c33882f32419c6"
    )
    assert hashlib.sha256(task.hidden_source.encode()).hexdigest() == (
        "92a099461feb5b73c7a0dde779482f78914213d3e152038055bb6766c9b7481a"
    )
    assert bench.hidden_oracle_sha256(task) == (
        "a5081d5881cda1de372a6476e07203ff7672b11cf71a1e67e27001bb59b7d895"
    )
    assert bench.hidden_oracle_classification(task) == (
        "arm-sealed-authored-parent-checker; not an independent holdout"
    )
    assert task.hidden_expected_json is not None
    assert "R17_PROBE_RESULT" not in task.hidden_expected_json
    assert [
        (
            package.distribution,
            package.package,
            package.version,
            package.trusted_source_sha256,
        )
        for package in task.runtime.packages
    ] == [
        (
            "pluggy", "pluggy", "1.6.0",
            "730093250a37d7d717dd7e7f1437bbedac53b1b378c79ecd27ae255b19ef2304",
        ),
        (
            "iniconfig", "iniconfig", "2.3.0",
            "fbb6f8782db1b0c49aafda68456e08ffe2051c2b6b69b61fde9f04313cf949de",
        ),
        (
            "Pygments", "pygments", "2.21.0",
            "eef8783697c9205496d5c357e9be17a75e0d3895d02e7cd9e4b4b5a6f250210b",
        ),
        (
            "packaging", "packaging", "26.3",
            "eb4f1d0f37a10fe5ac03972ecba52dd94f2cd0f06bf66dcc61dd0c10af736764",
        ),
    ]
    assert task.runtime.generated_files[0][0] == "src/_pytest/_version.py"
    assert task.runtime.trusted_overlay_sha256 == (
        "06f72d92dfa1f1bc0b9b1863ec21b5deee01cdc5f721f6ae148ae225a4bc0db7"
    )
    assert "sys.version_info < (3, 10)" in task.visible_source
    assert "sys.version_info < (3, 10)" in task.hidden_source
    assert "(3, 14)" not in task.visible_source + task.hidden_source
    assert "indirect" in task.prompt.lower()
    assert "duplicate parametrization" not in task.prompt.lower()
    for model_input in (
        task.prompt,
        bench.shared_user_prompt(task),
        task.visible_source,
    ):
        assert task.hidden_source not in model_input
        assert task.hidden_expected_json not in model_input
        assert task.reference_fix_sha not in model_input
        assert "pytest-dev/pytest/pull/13976" not in model_input
    compile(task.visible_source, task.visible_script, "exec")
    compile(task.hidden_source, "<r17-sealed-oracle>", "exec")


@pytest.mark.parametrize("task_id", tuple(bench.TASKS))
def test_fair_user_prompt_is_model_neutral_and_preserves_core_scope(
    task_id: str,
) -> None:
    task = bench.TASKS[task_id]
    prompt = bench.shared_user_prompt(task)
    assert "run_command" not in prompt
    assert "Use poteto-mode" not in prompt
    assert task.command_grant in prompt
    for path in task.editable:
        assert path in prompt


def test_repository_snapshot_and_protected_edits_refuse_unexpected_changes(
    tmp_path: Path,
) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("private transport metadata")
    (tmp_path / "h11").mkdir()
    source = tmp_path / "h11/_readers.py"
    source.write_text("def decode(): return False\n")
    (tmp_path / "qa_visible.py").write_text("assert True\n")
    baseline = bench.snapshot_files(tmp_path)
    assert ".git/config" not in baseline
    assert set(baseline) == {"h11/_readers.py", "qa_visible.py"}
    assert bench.check_edits(baseline, baseline, frozenset({"h11/_readers.py"})) == (
        True,
        False,
    )

    source.write_text("def decode(): return True\n")
    updated = bench.snapshot_files(tmp_path)
    assert bench.check_edits(baseline, updated, frozenset({"h11/_readers.py"})) == (
        True,
        True,
    )
    (tmp_path / "qa_visible.py").write_text("assert False\n")
    assert bench.check_edits(
        baseline, bench.snapshot_files(tmp_path), frozenset({"h11/_readers.py"})
    ) == (False, True)
    (tmp_path / "qa_visible.py").write_text("assert True\n")
    (tmp_path / "unexpected.txt").write_text("extra")
    assert bench.check_edits(
        baseline, bench.snapshot_files(tmp_path), frozenset({"h11/_readers.py"})
    ) == (False, False)
    (tmp_path / "unexpected.txt").unlink()
    source.unlink()
    source.symlink_to(tmp_path / "qa_visible.py")
    assert bench.check_edits(
        baseline, bench.snapshot_files(tmp_path), frozenset({"h11/_readers.py"})
    ) == (False, False)


def test_repository_scoring_refuses_protected_file_mode_change(tmp_path: Path) -> None:
    source = tmp_path / "module.py"
    qa = tmp_path / "qa_visible.py"
    source.write_text("VALUE = 0\n", encoding="utf-8")
    qa.write_text("assert True\n", encoding="utf-8")
    original = bench.snapshot_files(tmp_path)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    assert bench.check_edits(
        original, bench.snapshot_files(tmp_path), frozenset({"module.py"})
    ) == (True, True)

    original_mode = stat.S_IMODE(qa.stat().st_mode)
    qa.chmod(original_mode ^ stat.S_IXUSR)
    assert qa.read_bytes() == b"assert True\n"
    assert bench.check_edits(
        original, bench.snapshot_files(tmp_path), frozenset({"module.py"})
    ) == (False, True)


def test_repository_required_source_edits_cannot_be_mode_only(tmp_path: Path) -> None:
    source = tmp_path / "module.py"
    source.write_text("VALUE = 0\n", encoding="utf-8")
    original = bench.snapshot_files(tmp_path)

    source.chmod(stat.S_IMODE(source.stat().st_mode) ^ stat.S_IXUSR)
    assert bench.check_edits(
        original, bench.snapshot_files(tmp_path), frozenset({"module.py"})
    ) == (True, False)


def test_repository_preflight_refuses_reference_mode_only_source_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = bench.TASKS["h11-chunk-footer"]

    def fake_fetch(destination: Path, _task: object, sha: str) -> None:
        destination.mkdir(parents=True)
        source = destination / "h11/_readers.py"
        source.parent.mkdir()
        source.write_text("same source\n", encoding="utf-8")
        if sha == task.reference_fix_sha:
            source.chmod(stat.S_IMODE(source.stat().st_mode) ^ stat.S_IXUSR)

    def fake_oracle(workspace: Path, _task: object) -> dict[str, object]:
        passed = workspace.name == "reference"
        return {
            "correct": passed,
            "visible_oracle_exit": 0 if passed else 1,
            "hidden_oracle_exit": 0 if passed else 1,
        }

    monkeypatch.setattr(bench, "fetch_pinned", fake_fetch)
    monkeypatch.setattr(bench, "check_oracles", fake_oracle)
    with pytest.raises(RuntimeError, match="reference fix does not modify"):
        bench.preflight(task, tmp_path)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO creation unavailable")
def test_repository_scoring_refuses_special_files_even_if_source_is_correct(
    tmp_path: Path,
) -> None:
    source = tmp_path / "module.py"
    qa = tmp_path / "qa_visible.py"
    source.write_text("VALUE = 0\n", encoding="utf-8")
    qa.write_text("assert True\n", encoding="utf-8")
    original = bench.snapshot_files(tmp_path)
    source.write_text("VALUE = 1\n", encoding="utf-8")

    os.mkfifo(tmp_path / "unexpected.pipe")
    updated = bench.snapshot_files(tmp_path)
    assert "unexpected.pipe" in updated
    assert bench.check_edits(original, updated, frozenset({"module.py"})) == (
        False, False,
    )

    (tmp_path / "unexpected.pipe").unlink()
    source.unlink()
    os.mkfifo(source)
    assert bench.check_edits(
        original, bench.snapshot_files(tmp_path), frozenset({"module.py"})
    ) == (False, False)


def test_repository_scoring_accepts_added_and_removed_editable_modules() -> None:
    original = {
        "src/pluggy/_hooks.py": "old-shim-source",
        "src/pluggy/_callers.py": "old-caller-source",
        "src/pluggy/_legacy.py": "to-be-removed",
        "qa_visible.py": "sealed-visible-test",
    }
    updated = {
        "src/pluggy/_hooks.py": "compatibility-shim",
        "src/pluggy/_callers.py": "compatibility-shim",
        "src/pluggy/_caller.py": "new-caller-module",
        "src/pluggy/_config.py": "new-config-module",
        "qa_visible.py": "sealed-visible-test",
    }
    editable = frozenset(
        {
            "src/pluggy/_hooks.py",
            "src/pluggy/_callers.py",
            "src/pluggy/_caller.py",
            "src/pluggy/_config.py",
            "src/pluggy/_legacy.py",
        }
    )
    assert bench.check_edits(original, updated, editable) == (True, True)

    protected_deletion = {**updated}
    protected_deletion.pop("qa_visible.py")
    assert bench.check_edits(original, protected_deletion, editable) == (False, False)


def test_editable_path_validation_requires_reference_proof_for_new_modules() -> None:
    original = {"src/pkg/_hooks.py": "sha-old"}
    editable = frozenset({"src/pkg/_hooks.py", "src/pkg/_caller.py"})
    bench.validate_editable_paths(
        original,
        editable,
        {"reference_added_paths": ["src/pkg/_caller.py"]},
    )
    with pytest.raises(RuntimeError, match="absent from both pinned revisions"):
        bench.validate_editable_paths(original, editable, {"reference_added_paths": []})
    with pytest.raises(RuntimeError, match="absent from both pinned revisions"):
        bench.validate_editable_paths(
            original,
            editable,
            {"reference_added_paths": ["src/pkg/_other.py"]},
        )
    with pytest.raises(RuntimeError, match="outside the editable allowlist"):
        bench.validate_editable_paths(
            original,
            editable,
            {
                "reference_added_paths": [
                    "src/pkg/_caller.py",
                    "src/pkg/_unapproved.py",
                ]
            },
        )


def test_two_module_scoring_rejects_partial_repair_and_modified_qa() -> None:
    required = bench.TASKS["urllib3-proxy-redirect"].editable
    pool = "src/urllib3/connectionpool.py"
    manager = "src/urllib3/poolmanager.py"
    original = {
        pool: "sha-original-pool",
        manager: "sha-original-manager",
        "qa_visible.py": "sha-test",
    }
    partially_fixed = {**original, pool: "sha-fixed-pool"}
    assert bench.check_edits(original, partially_fixed, required) == (True, False)
    both_fixed = {**partially_fixed, manager: "sha-fixed-manager"}
    assert bench.check_edits(original, both_fixed, required) == (True, True)
    altered_test = {**both_fixed, "qa_visible.py": "sha-altered-test"}
    assert bench.check_edits(original, altered_test, required) == (False, True)


def test_single_file_repair_can_pass_when_task_allows_it() -> None:
    task = bench.TASKS["pytest-mixed-indirect-fixture-precedence"]
    fixtures = "src/_pytest/fixtures.py"
    original = {path: f"original:{path}" for path in task.editable}
    one_file_fixed = {**original, fixtures: "fixed fixtures"}
    assert bench.check_edits(original, one_file_fixed, task.editable) == (
        True,
        False,
    )
    assert bench.check_edits(
        original,
        one_file_fixed,
        task.editable,
        require_all_editable_changed=task.require_all_editable_changed,
    ) == (True, True)
    two_file_fixed = {
        **one_file_fixed,
        "src/_pytest/python.py": "fixed python module",
    }
    assert bench.check_edits(
        original,
        two_file_fixed,
        task.editable,
        require_all_editable_changed=task.require_all_editable_changed,
    ) == (True, True)
    assert bench.check_edits(
        original,
        original,
        task.editable,
        require_all_editable_changed=task.require_all_editable_changed,
    ) == (True, False)


def test_r17_upstream_test_and_runtime_paths_remain_protected() -> None:
    task = bench.TASKS["pytest-mixed-indirect-fixture-precedence"]
    source = "src/_pytest/fixtures.py"
    original = {
        source: "original source",
        "src/_pytest/python.py": "other source",
        "testing/python/collect.py": "sealed upstream tests",
        "src/_pytest/_version.py": "generated metadata",
        ".benchmark_runtime/site/pluggy/__init__.py": "runtime overlay",
    }
    changed = {
        **original,
        source: "repaired source",
        "testing/python/collect.py": "attempted test edit",
    }
    assert bench.check_edits(
        original,
        changed,
        task.editable,
        require_all_editable_changed=task.require_all_editable_changed,
    ) == (False, True)


def test_r17_parent_checker_rejects_boolean_number_substitution() -> None:
    """JSON booleans must never compare equal to numeric fixture observations."""
    task = bench.TASKS["pytest-mixed-indirect-fixture-precedence"]
    expected = json.loads(task.hidden_expected_json)
    valid_result = {
        "valid_exit": expected["valid_exit"],
        "observations": expected["observations"],
        "unknown_exit": expected["unknown_exit"],
        "unknown_output": expected["unknown_error"],
    }

    def result_for(payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            [], 0, "R17_PROBE_RESULT:" + json.dumps(payload), ""
        )

    assert bench._r17_hidden_probe_matches(task, result_for(valid_result))
    corrupted = json.loads(json.dumps(valid_result))
    assert ["ordinary", 0] in corrupted["observations"]
    corrupted["observations"] = [
        ["ordinary", False] if item == ["ordinary", 0] else item
        for item in corrupted["observations"]
    ]
    assert not bench._r17_hidden_probe_matches(task, result_for(corrupted))


def test_r17_hidden_oracle_is_run_in_memory_and_never_written(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = bench.TASKS["pytest-mixed-indirect-fixture-precedence"]
    visible = tmp_path / task.visible_script
    visible.write_text(task.visible_source, encoding="utf-8")
    calls: list[tuple[str | None, str | None]] = []

    def fake_oracle(
        workspace: Path, script: str | None = None, *, code: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        assert workspace == tmp_path
        calls.append((script, code))
        assert not (workspace / "hidden_oracle.py").exists()
        if code is not None:
            assert task.hidden_expected_json not in code
            payload = json.loads(task.hidden_expected_json)
            probe_result = {
                "valid_exit": payload["valid_exit"],
                "observations": payload["observations"],
                "unknown_exit": payload["unknown_exit"],
                "unknown_output": payload["unknown_error"],
            }
            return subprocess.CompletedProcess(
                [], 0, "R17_PROBE_RESULT:" + json.dumps(probe_result), ""
            )
        return subprocess.CompletedProcess([], 0, "passed", "")

    monkeypatch.setattr(bench, "oracle_python", fake_oracle)
    result = bench.check_oracles(tmp_path, task)
    assert result["correct"] is True
    assert calls == [(task.visible_script, None), (None, task.hidden_source)]


def test_r17_parent_expectations_are_absent_from_candidate_process_surfaces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = bench.TASKS["pytest-mixed-indirect-fixture-precedence"]
    sentinel = "R17_PARENT_ONLY_EXPECTATION_SENTINEL_7cb4a6"
    task = replace(task, hidden_expected_json=json.dumps({"secret": sentinel}))
    case_file = tmp_path / "hidden_cases.py"
    case_file.write_text(task.hidden_source, encoding="utf-8")
    scanner = r"""from pathlib import Path
import os
import sys

test_file = Path(sys.argv[3])
visible = [repr(sys.argv), Path('/proc/self/cmdline').read_bytes().decode('utf-8', 'ignore')]
visible.append(repr(dict(os.environ)))
visible.extend(str(path) for path in Path.cwd().rglob('*'))
visible.extend(path.read_text(encoding='utf-8', errors='ignore') for path in Path.cwd().rglob('*') if path.is_file())
visible.append(test_file.read_text(encoding='utf-8'))
print('\n'.join(visible))
"""
    candidate_view = subprocess.run(
        [sys.executable, "-B", "-c", scanner, "source", "overlay", str(case_file)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert sentinel not in candidate_view
    assert str(case_file) in candidate_view
    assert task.hidden_source in candidate_view

    def fake_oracle(
        workspace: Path, script: str | None = None, *, code: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        if code is not None:
            assert sentinel not in code
            return subprocess.CompletedProcess([], 0, "R17_PROBE_RESULT:{}", "")
        return subprocess.CompletedProcess([], 0, "visible", "")

    monkeypatch.setattr(bench, "oracle_python", fake_oracle)
    result = bench.check_oracles(tmp_path, task)
    assert result["hidden_oracle_exit"] == 1


def _git_ignored_pytest_tree(root: Path) -> None:
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / ".gitignore").write_text("/src/_pytest/_version.py\n", encoding="utf-8")
    (root / "src/_pytest").mkdir(parents=True)
    (root / "src/_pytest/__init__.py").write_text("", encoding="utf-8")


def test_r17_runtime_overlay_is_lock_verified_deterministic_and_ignored_neutral(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = bench.TASKS["pytest-mixed-indirect-fixture-precedence"]
    baseline = tmp_path / "baseline"
    reference = tmp_path / "reference"
    _git_ignored_pytest_tree(baseline)
    _git_ignored_pytest_tree(reference)

    git_calls: list[list[str]] = []
    original_command = bench.command

    def local_git_only(
        argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int
    ) -> subprocess.CompletedProcess[str]:
        assert argv == ["git", "check-ignore", "-q", "--", "src/_pytest/_version.py"]
        assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert not {
            "OPENAI_API_KEY",
            "OPENROUTER_API_KEY",
            "ANTHROPIC_API_KEY",
        }.intersection(env)
        git_calls.append(argv)
        return original_command(argv, cwd=cwd, env=env, timeout=timeout)

    monkeypatch.setattr(bench, "command", local_git_only)

    baseline_receipt = bench.materialize_task_runtime(baseline, task.runtime)
    reference_receipt = bench.materialize_task_runtime(reference, task.runtime)
    assert baseline_receipt == reference_receipt
    assert baseline_receipt["source"] == (
        "active Ash virtualenv; uv.lock and installed RECORD checked; "
        "source bytes matched chief-observed fingerprints, not PyPI wheel hashes"
    )
    assert set(baseline_receipt["dependencies"]) == {
        "pluggy",
        "iniconfig",
        "Pygments",
        "packaging",
    }
    assert baseline_receipt["overlay_file_count"] > 300
    assert baseline_receipt["overlay_sha256"] == (
        "06f72d92dfa1f1bc0b9b1863ec21b5deee01cdc5f721f6ae148ae225a4bc0db7"
    )
    assert baseline_receipt["oracle_interpreter"] == (
        "Linux /usr/bin/python3, Python 3.10 or newer, shared by task and grader"
    )
    assert baseline_receipt["lock_sha256"]
    assert len(git_calls) == 2
    assert set(baseline_receipt["generated_files"]) == {"src/_pytest/_version.py"}

    for tree in (baseline, reference):
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", "--", "src/_pytest/_version.py"],
            cwd=tree,
            check=False,
        )
        assert ignored.returncode == 0
        files = bench.snapshot_files(tree)
        assert "src/_pytest/_version.py" in files
        assert ".benchmark_runtime/site/pluggy/__init__.py" in files
        assert not any("__pycache__" in path or path.endswith(".pyc") for path in files)
        assert bench.snapshot_files(tree) == files
        bench.verify_task_runtime(tree, task.runtime, baseline_receipt)
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=tree,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert "src/_pytest/_version.py" not in status

    baseline_runtime = {
        path.relative_to(baseline).as_posix(): path.read_bytes()
        for path in (baseline / ".benchmark_runtime").rglob("*")
        if path.is_file()
    }
    reference_runtime = {
        path.relative_to(reference).as_posix(): path.read_bytes()
        for path in (reference / ".benchmark_runtime").rglob("*")
        if path.is_file()
    }
    assert baseline_runtime == reference_runtime

    tampered = baseline / ".benchmark_runtime/site/iniconfig/__init__.py"
    tampered.write_text("# changed\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="bytes differ"):
        bench.verify_task_runtime(baseline, task.runtime, baseline_receipt)
    tampered.unlink()
    (baseline / ".benchmark_runtime/site/stray.so").write_bytes(b"not executable")
    with pytest.raises(RuntimeError, match="unexpected file set"):
        bench.verify_task_runtime(baseline, task.runtime, baseline_receipt)


def test_r17_runtime_refuses_package_symlinks_and_special_files(
    tmp_path: Path,
) -> None:
    task = bench.TASKS["pytest-mixed-indirect-fixture-precedence"]
    _, purelib, metadata = bench._locked_runtime_versions(task.runtime)
    package = next(
        item for item in task.runtime.packages if item.package == "iniconfig"
    )
    recorded = metadata[package.distribution.casefold()][1]
    source_root = purelib / package.package
    symlink_root = tmp_path / "symlink-purelib" / package.package
    symlink_root.parent.mkdir()
    import shutil

    shutil.copytree(source_root, symlink_root)
    source_file = next(path for path in symlink_root.rglob("*.py") if path.is_file())
    external = tmp_path / "external-source.py"
    external.write_text("must not be read", encoding="utf-8")
    source_file.unlink()
    source_file.symlink_to(external)
    with pytest.raises(RuntimeError, match="symlink"):
        bench._package_source_files(symlink_root.parent, package, recorded)

    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO creation unavailable")
    special_root = tmp_path / "special-purelib" / package.package
    special_root.parent.mkdir()
    shutil.copytree(source_root, special_root)
    os.mkfifo(special_root / "unexpected.py")
    with pytest.raises(RuntimeError, match="special file"):
        bench._package_source_files(special_root.parent, package, recorded)


def test_r17_runtime_rejects_modified_source_even_with_matching_record(
    tmp_path: Path,
) -> None:
    task = bench.TASKS["pytest-mixed-indirect-fixture-precedence"]
    _, purelib, metadata = bench._locked_runtime_versions(task.runtime)
    package = next(item for item in task.runtime.packages if item.package == "iniconfig")
    recorded = dict(metadata[package.distribution.casefold()][1])
    modified_root = tmp_path / "modified-purelib" / package.package
    modified_root.parent.mkdir()
    import shutil

    shutil.copytree(purelib / package.package, modified_root)
    changed = next(path for path in modified_root.rglob("*.py") if path.is_file())
    data = changed.read_bytes() + b"# attacker-controlled source\n"
    changed.write_bytes(data)
    relative = (Path(package.package) / changed.relative_to(modified_root)).as_posix()
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest())
    recorded[relative] = (
        "sha256=" + digest.rstrip(b"=").decode("ascii"),
        str(len(data)),
    )

    with pytest.raises(RuntimeError, match="trusted runtime source fingerprint mismatch"):
        bench._package_source_files(modified_root.parent, package, recorded)


def test_r17_runtime_rejects_hardlinked_source_even_with_forged_record(
    tmp_path: Path,
) -> None:
    task = bench.TASKS["pytest-mixed-indirect-fixture-precedence"]
    _, purelib, metadata = bench._locked_runtime_versions(task.runtime)
    package = next(item for item in task.runtime.packages if item.package == "iniconfig")
    recorded = dict(metadata[package.distribution.casefold()][1])
    linked_root = tmp_path / "linked-purelib" / package.package
    linked_root.parent.mkdir()
    import shutil

    shutil.copytree(purelib / package.package, linked_root)
    linked = next(path for path in linked_root.rglob("*.py") if path.is_file())
    external = tmp_path / "host-only-source.py"
    external.write_text("synthetic host-only bytes", encoding="utf-8")
    linked.unlink()
    os.link(external, linked)
    relative = (Path(package.package) / linked.relative_to(linked_root)).as_posix()
    data = external.read_bytes()
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest())
    recorded[relative] = (
        "sha256=" + digest.rstrip(b"=").decode("ascii"),
        str(len(data)),
    )

    with pytest.raises(RuntimeError, match="hardlinked"):
        bench._package_source_files(linked_root.parent, package, recorded)
    assert external.read_text(encoding="utf-8") == "synthetic host-only bytes"


def test_r17_runtime_source_requires_locked_active_venv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = bench.TASKS["pytest-mixed-indirect-fixture-precedence"]
    outside = tmp_path / "external-site"
    outside.mkdir()
    original_paths = bench.sysconfig.get_paths
    monkeypatch.setattr(
        bench.sysconfig,
        "get_paths",
        lambda: {**original_paths(), "purelib": str(outside)},
    )
    with pytest.raises(RuntimeError, match="outside the active Ash virtualenv"):
        bench._locked_runtime_versions(task.runtime)
    monkeypatch.setattr(bench.sysconfig, "get_paths", original_paths)

    lock_root = tmp_path / "wrong-lock"
    lock_root.mkdir()
    (lock_root / "uv.lock").write_text(
        '[[package]]\nname = "pluggy"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(bench, "REPO", lock_root)
    with pytest.raises(RuntimeError, match="does not uniquely pin pluggy"):
        bench._locked_runtime_versions(task.runtime)

    untrusted = tmp_path / "untrusted-python"
    untrusted.mkdir()
    monkeypatch.setattr(bench.sys, "prefix", str(untrusted))
    with pytest.raises(RuntimeError, match="active Ash virtualenv"):
        bench._locked_runtime_versions(task.runtime)


def test_r17_task_command_and_grader_interpreters_must_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # This unit checks OS-specific logic without requiring macOS to run Linux.
    monkeypatch.setattr(bench.sys, "platform", "linux")
    monkeypatch.setattr(
        bench.shutil,
        "which",
        lambda _name, *, path: "/usr/bin/python3",
    )
    bench._verify_r17_python_command_parity()

    monkeypatch.setattr(
        bench.shutil,
        "which",
        lambda _name, *, path: (
            "/usr/local/bin/python3" if path.startswith("/usr/local")
            else "/usr/bin/python3"
        ),
    )
    with pytest.raises(RuntimeError, match="resolve to /usr/bin/python3"):
        bench._verify_r17_python_command_parity()

    monkeypatch.setattr(bench.sys, "platform", "darwin")
    with pytest.raises(RuntimeError, match="Linux host"):
        bench._verify_r17_python_command_parity()


def test_legacy_preflight_accepts_baseline_with_only_hidden_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preserve R1-R16 acceptance when visible misses a real original defect."""
    task = bench.TASKS["h11-chunk-footer"]

    def fake_fetch(destination: Path, _task: object, sha: str) -> None:
        path = destination / "h11/_readers.py"
        path.parent.mkdir(parents=True)
        path.write_text(
            "reference code\n" if sha == task.reference_fix_sha else "broken code\n",
            encoding="utf-8",
        )

    def fake_grade(workspace: Path, _task: object) -> dict[str, object]:
        if workspace.name == "baseline":
            return {
                "correct": False,
                "visible_oracle_exit": 0,
                "hidden_oracle_exit": 1,
            }
        return {
            "correct": True,
            "visible_oracle_exit": 0,
            "hidden_oracle_exit": 0,
        }

    monkeypatch.setattr(bench, "fetch_pinned", fake_fetch)
    monkeypatch.setattr(bench, "check_oracles", fake_grade)
    _, proof = bench.preflight(task, tmp_path)
    assert proof["base_visible_exit"] == 0
    assert proof["base_hidden_exit"] == 1
    assert proof["reference_visible_exit"] == 0
    assert proof["reference_hidden_exit"] == 0


def test_r17_preflight_records_exact_reference_paths_and_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = bench.TASKS["pytest-mixed-indirect-fixture-precedence"]
    # Real Linux-only host validation is covered by the separate parity test.
    monkeypatch.setattr(bench, "_verify_r17_python_command_parity", lambda: None)
    monkeypatch.setattr(bench, "_attest_r17_system_python_version", lambda: "3.14.10")

    def fake_fetch(destination: Path, _task: object, sha: str) -> None:
        destination.mkdir(parents=True)
        source = destination / "src/_pytest"
        source.mkdir(parents=True)
        (source / "fixtures.py").write_text(
            "fixed" if sha == task.reference_fix_sha else "base",
            encoding="utf-8",
        )
        (source / "python.py").write_text(
            "reference" if sha == task.reference_fix_sha else "same",
            encoding="utf-8",
        )
        upstream_tests = destination / "testing/python"
        upstream_tests.mkdir(parents=True)
        test_version = "reference" if sha == task.reference_fix_sha else "base"
        (upstream_tests / "collect.py").write_text(test_version, encoding="utf-8")
        (upstream_tests / "metafunc.py").write_text(test_version, encoding="utf-8")

    runtime_receipt = {"overlay_sha256": "same-overlay"}

    def fake_runtime(root: Path, _runtime: object) -> dict[str, object]:
        overlay = root / ".benchmark_runtime/site/pluggy"
        overlay.mkdir(parents=True)
        (overlay / "__init__.py").write_text("trusted", encoding="utf-8")
        (root / "src/_pytest/_version.py").write_text(
            "__version__ = '9.1.0.dev0'\n", encoding="utf-8"
        )
        return runtime_receipt

    def fake_grade(workspace: Path, _task: object) -> dict[str, object]:
        assert (workspace / ".benchmark_runtime/site/pluggy/__init__.py").is_file()
        assert (workspace / "src/_pytest/_version.py").is_file()
        assert (workspace / "qa_visible.py").is_file()
        good = workspace.name == "reference"
        return {
            "correct": good,
            "visible_oracle_exit": 0 if good else 2,
            "hidden_oracle_exit": 0 if good else 2,
        }

    monkeypatch.setattr(bench, "fetch_pinned", fake_fetch)
    monkeypatch.setattr(bench, "materialize_task_runtime", fake_runtime)
    monkeypatch.setattr(bench, "check_oracles", fake_grade)
    _, proof = bench.preflight(task, tmp_path)
    assert proof["base_visible_exit"] == 2
    assert proof["reference_visible_exit"] == 0
    assert proof["reference_modified_paths"] == [
        "src/_pytest/fixtures.py",
        "src/_pytest/python.py",
    ]
    assert proof["upstream_changed_paths"] == sorted(task.expected_reference_paths)
    assert set(proof["base_editable_sha256"]) == task.editable
    assert set(proof["reference_editable_sha256"]) == task.editable
    assert proof["base_sha"] == task.base_sha
    assert proof["reference_fix_sha"] == task.reference_fix_sha
    assert proof["upstream_pr"] == task.upstream_pr
    assert proof["license"] == "MIT"
    assert proof["runtime_overlay"] == runtime_receipt
    assert proof["hidden_oracle_sha256"] == bench.hidden_oracle_sha256(task)
    assert proof["hidden_probe_sha256"] == hashlib.sha256(
        task.hidden_source.encode()
    ).hexdigest()
    assert proof["hidden_oracle_classification"] == (
        "arm-sealed-authored-parent-checker; not an independent holdout"
    )
    assert proof["system_python_version"]
    assert proof["system_python_executable"] == "/usr/bin/python3"

    def extra_reference_path(destination: Path, selected: object, sha: str) -> None:
        fake_fetch(destination, selected, sha)
        if sha == task.reference_fix_sha:
            (destination / "testing/python/unexpected.py").write_text(
                "outside the pinned PR path set", encoding="utf-8"
            )

    monkeypatch.setattr(bench, "fetch_pinned", extra_reference_path)
    with pytest.raises(
        RuntimeError, match="source paths differ from the task manifest"
    ):
        bench.preflight(task, tmp_path / "extra-path")

    def one_base_oracle_green(workspace: Path, _task: object) -> dict[str, object]:
        if workspace.name == "reference":
            return {
                "correct": True,
                "visible_oracle_exit": 0,
                "hidden_oracle_exit": 0,
            }
        return {
            "correct": False,
            "visible_oracle_exit": 2,
            "hidden_oracle_exit": 0,
        }

    monkeypatch.setattr(bench, "fetch_pinned", fake_fetch)
    monkeypatch.setattr(bench, "check_oracles", one_base_oracle_green)
    with pytest.raises(RuntimeError, match="not independently reproducible"):
        bench.preflight(task, tmp_path / "partial-red")


def test_r17_ash_fake_grade_accepts_one_semantic_source_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_name = "pytest-mixed-indirect-fixture-precedence"
    task = bench.TASKS[task_name]
    scratch_roots: list[Path] = []

    def preflight(_task: object, scratch: Path):
        scratch_roots.append(scratch)
        baseline = scratch / "baseline"
        source = baseline / "src/_pytest"
        source.mkdir(parents=True)
        (source / "fixtures.py").write_text("baseline", encoding="utf-8")
        (source / "python.py").write_text("unchanged", encoding="utf-8")
        (source / "_version.py").write_text("generated", encoding="utf-8")
        overlay = baseline / ".benchmark_runtime/site/pluggy"
        overlay.mkdir(parents=True)
        (overlay / "__init__.py").write_text("locked source", encoding="utf-8")
        (baseline / task.visible_script).write_text(
            task.visible_source, encoding="utf-8"
        )
        (scratch / "reference").mkdir()
        return baseline, {
            "base_visible_exit": 2,
            "base_hidden_exit": 2,
            "reference_visible_exit": 0,
            "reference_hidden_exit": 0,
            "reference_added_paths": [],
            "runtime_overlay": {"overlay_sha256": "fixture"},
        }

    def fake_command(
        argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int
    ) -> subprocess.CompletedProcess[str]:
        assert cwd.exists()
        assert timeout > 0
        assert not {
            "OPENAI_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY"
        }.intersection(env)
        setup_commands.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    class FakeChild:
        def wait(self, *, timeout: int) -> int:
            assert timeout == 30
            return 0

    def fake_popen(argv: list[str], **_kwargs: object) -> FakeChild:
        assert task.prompt in argv
        cwd = _kwargs["cwd"]
        assert isinstance(cwd, Path)
        (cwd / "src/_pytest/fixtures.py").write_text("repaired", encoding="utf-8")
        return FakeChild()

    tool_calls = [
        {
            "tool": next(iter(bench.EDIT_TOOL_NAMES)),
            "success": True,
            "executed": True,
        },
        {
            "tool": "run_command",
            "success": True,
            "executed": True,
            "command": task.command_grant,
        },
    ]

    setup_commands: list[list[str]] = []
    monkeypatch.setattr(bench, "preflight", preflight)
    monkeypatch.setattr(bench, "verify_task_runtime", lambda *_args: None)
    monkeypatch.setattr(bench, "_chatgpt_tmpfs_root", lambda: tmp_path / "memory")
    (tmp_path / "memory").mkdir()
    def fake_stage_access(home: Path, *_args: object, **_kwargs: object) -> None:
        (home / ".ash").mkdir()

    monkeypatch.setattr(bench, "_stage_chatgpt_access_only", fake_stage_access)
    monkeypatch.setattr(bench, "command", fake_command)
    monkeypatch.setattr(bench.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        bench.subprocess,
        "check_output",
        lambda *_args, **_kwargs: task.base_sha,
    )
    monkeypatch.setattr(
        bench,
        "collect_coding_session_receipts",
        lambda *_args: {
            "calls": tool_calls,
            "audit_ok": True,
            "lineage_ok": True,
            "events": {},
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            "terminals": ["turn.completed"],
            "context_continuations": 0,
            "session_count": 1,
        },
    )
    monkeypatch.setattr(
        bench,
        "check_oracles",
        lambda *_args: {
            "correct": True,
            "visible_oracle_exit": 0,
            "hidden_oracle_exit": 0,
        },
    )

    evidence = tmp_path / "ash-evidence"
    evidence.mkdir()
    result = bench.run_task(
        task_name,
        model="gpt-6-luna",
        account=object(),
        evidence=evidence,
        timeout=30,
        context_tokens=32768,
        turn_token_budget=0,
    )
    assert result["passed"] is True, result
    assert result["protected_files_unchanged"] is True
    assert result["required_sources_changed"] is True
    assert result["required_tools_succeeded"] is True
    assert result["oracle"]["correct"] is True
    assert result["scratch_removed"] is True
    assert any(task.command_grant in " ".join(argv) for argv in setup_commands)
    assert not scratch_roots[0].exists()


def test_fetch_pinned_checks_out_only_exact_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = bench.TASKS["h11-chunk-footer"]
    invocations: list[list[str]] = []

    def fake_command(
        argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int
    ) -> subprocess.CompletedProcess[str]:
        assert cwd == tmp_path / "source"
        assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert "OPENROUTER_API_KEY" not in env
        assert 0 < timeout <= 45
        invocations.append(argv)
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=task.base_sha + "\n" if argv[1:] == ["rev-parse", "HEAD"] else "",
        )

    monkeypatch.setattr(bench, "command", fake_command)
    bench.fetch_pinned(tmp_path / "source", task, task.base_sha)
    assert invocations == [
        ["git", "init", "-q"],
        ["git", "remote", "add", "origin", task.upstream],
        [
            "git",
            "-c",
            "protocol.version=2",
            "fetch",
            "--depth=1",
            "origin",
            task.base_sha,
        ],
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "checkout",
            "-q",
            "--detach",
            task.base_sha,
        ],
        ["git", "rev-parse", "HEAD"],
    ]


def test_preflight_requires_red_base_and_green_upstream_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = bench.TASKS["h11-chunk-footer"]

    def fake_fetch(destination: Path, _: object, sha: str) -> None:
        destination.mkdir()
        (destination / "h11").mkdir()
        (destination / "h11/_readers.py").write_text(sha)

    def fake_grade(workspace: Path, _: object) -> dict[str, object]:
        assert (workspace / "qa_visible.py").is_file()
        good = workspace.name == "reference"
        return {
            "correct": good,
            "visible_oracle_exit": 0 if good else 1,
            "hidden_oracle_exit": 0 if good else 1,
        }

    monkeypatch.setattr(bench, "fetch_pinned", fake_fetch)
    monkeypatch.setattr(bench, "check_oracles", fake_grade)
    baseline, proof = bench.preflight(task, tmp_path)
    assert baseline.name == "baseline"
    assert proof == {
        "base_visible_exit": 1,
        "base_hidden_exit": 1,
        "reference_visible_exit": 0,
        "reference_hidden_exit": 0,
        "reference_added_paths": [],
        "reference_removed_paths": [],
        "reference_modified_paths": ["h11/_readers.py"],
    }

    def broken_grade(workspace: Path, _: object) -> dict[str, object]:
        return {"correct": True, "visible_oracle_exit": 0, "hidden_oracle_exit": 0}

    monkeypatch.setattr(bench, "check_oracles", broken_grade)
    (tmp_path / "broken").mkdir()
    with pytest.raises(RuntimeError, match="not independently reproducible"):
        bench.preflight(task, tmp_path / "broken")

    def linked_source(destination: Path, selected: object, sha: str) -> None:
        fake_fetch(destination, selected, sha)
        (destination / "unsafe-link").symlink_to("/etc/passwd")

    monkeypatch.setattr(bench, "fetch_pinned", linked_source)
    (tmp_path / "linked").mkdir()
    with pytest.raises(RuntimeError, match="contains a symlink"):
        bench.preflight(task, tmp_path / "linked")


def test_preflight_accepts_reference_added_source_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = replace(
        bench.TASKS["h11-chunk-footer"],
        editable=frozenset(
            {"h11/_readers.py", "h11/_new_reader.py", "h11/_legacy.py"}
        ),
    )

    def fake_fetch(destination: Path, _: object, sha: str) -> None:
        destination.mkdir()
        package = destination / "h11"
        package.mkdir()
        (package / "_readers.py").write_text(sha)
        if sha == task.reference_fix_sha:
            (package / "_new_reader.py").write_text("new role-specific module")
        else:
            (package / "_legacy.py").write_text("old module removed by refactor")

    def fake_grade(workspace: Path, _: object) -> dict[str, object]:
        good = workspace.name == "reference"
        return {
            "correct": good,
            "visible_oracle_exit": 0 if good else 1,
            "hidden_oracle_exit": 0 if good else 1,
        }

    monkeypatch.setattr(bench, "fetch_pinned", fake_fetch)
    monkeypatch.setattr(bench, "check_oracles", fake_grade)
    _, proof = bench.preflight(task, tmp_path)
    assert proof == {
        "base_visible_exit": 1,
        "base_hidden_exit": 1,
        "reference_visible_exit": 0,
        "reference_hidden_exit": 0,
        "reference_added_paths": ["h11/_new_reader.py"],
        "reference_removed_paths": ["h11/_legacy.py"],
        "reference_modified_paths": ["h11/_readers.py"],
    }


def test_reference_checkout_deleted_before_agent_access(tmp_path: Path) -> None:
    base = tmp_path / "baseline"
    ref = tmp_path / "reference"
    base.mkdir()
    ref.mkdir()
    (ref / "historical_fix.py").write_text("SECRET REFERENCE SOLUTION")
    bench.remove_reference_checkout(tmp_path)
    assert base.exists()
    assert not ref.exists()
    with pytest.raises(RuntimeError, match="missing or unsafe"):
        bench.remove_reference_checkout(tmp_path)


def test_reference_checkout_symlink_refused_without_deletion(tmp_path: Path) -> None:
    external = tmp_path / "protected"
    external.mkdir()
    (external / "source.py").write_text("KEEP")
    (tmp_path / "reference").symlink_to(external, target_is_directory=True)
    with pytest.raises(RuntimeError, match="missing or unsafe"):
        bench.remove_reference_checkout(tmp_path)
    assert (external / "source.py").read_text() == "KEEP"


@pytest.mark.parametrize(
    ("arguments", "error"),
    [
        (["--chatgpt-model", "!invalid"], "invalid exact ChatGPT model ID"),
        (["--chatgpt-model", "gpt-6-luna", "--timeout", "500"], "timeout must"),
        (["--chatgpt-model", "gpt-6-luna", "--context-tokens", "5"], "context budget"),
        (["--chatgpt-model", "gpt-6-luna", "--context-tokens", "131073"], "context budget"),
    ],
)
def test_real_repo_cli_rejects_invalid_options_without_evidence(
    tmp_path: Path, arguments: list[str], error: str
) -> None:
    evidence = tmp_path / "must-not-exist"
    child = subprocess.run(
        [
            sys.executable,
            str(_path),
            "--task",
            "h11-chunk-footer",
            "--evidence-dir",
            str(evidence),
            *arguments,
        ],
        cwd=_devtools.parent,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(_devtools.parent / "src")},
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert child.returncode == 2
    assert error in child.stderr
    assert not evidence.exists()
