from __future__ import annotations

import errno
import hashlib
import importlib.util
import io
import json
import os
import queue
import signal
import subprocess
import sys
import threading
from argparse import Namespace
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "devtools" / "ash_explorer.py"
_spec = importlib.util.spec_from_file_location("ash_explorer_under_test", RUNNER)
assert _spec is not None and _spec.loader is not None
explorer = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = explorer
_spec.loader.exec_module(explorer)

ExpectedOutcome = explorer.ExpectedOutcome
ExplorerError = explorer.ExplorerError
StepRequest = explorer.StepRequest
SearchQuery = explorer.SearchQuery
SearchSnapshot = explorer.SearchSnapshot
_search_snapshot = explorer._search_snapshot
_verify_search = explorer._verify_search
_classify = explorer._classify
_handle_termination = explorer._handle_termination
_extract_tool_outcome = explorer._extract_tool_outcome
_read_live_line = explorer._read_live_line
_target_path = explorer._target_path
_tool_outcome = explorer._tool_outcome


def test_live_reader_returns_successive_jsonl_actions_without_dropping_lines() -> None:
    first = b'{"action":"step","label":"one"}\n'
    second = b'{"action":"finish"}\n'
    stream = io.BytesIO(first + second)

    assert _read_live_line(stream) == first
    assert _read_live_line(stream) == second
    assert _read_live_line(stream) is None


def test_nonzero_ash_exit_is_not_classified_as_observed() -> None:
    request = StepRequest(
        label="probe",
        prompt="Inspect the behavior.",
        file_path="probe.txt",
        content="probe\n",
        overwrite=False,
        expectation=ExpectedOutcome(kind="observed"),
    )

    assert _classify(
        request,
        {"exit_code": 7},
        None,
        None,
        None,
        [],
        {},
    ) == ("unexpected_failure", "Ash exited with 7")


def test_truncated_ash_output_is_not_classified_as_observed() -> None:
    request = StepRequest(
        label="probe",
        prompt="Inspect the behavior.",
        file_path="probe.txt",
        content="probe\n",
        overwrite=False,
        expectation=ExpectedOutcome(kind="observed"),
    )

    assert _classify(
        request,
        {"exit_code": 0, "stdout_truncated": True},
        None,
        None,
        None,
        [],
        {},
    ) == ("driver_failure", "Ash process output was truncated")


def test_tool_error_without_success_field_is_a_refusal_outcome() -> None:
    event = {
        "type": "tool.error",
        "tool": "write_file",
        "error": "Access denied: outside project scope",
        "dispatched": True,
        "ambiguous": True,
    }
    outcome = _tool_outcome([event], {"tool_outcome": {"tool_call_id": "call-1"}})

    assert outcome is not None
    assert outcome["success"] is False
    assert outcome["ambiguous"] is True
    assert outcome["error"] == event["error"]


def test_pre_dispatch_policy_denial_is_not_an_ambiguous_tool_outcome() -> None:
    event = {
        "type": "tool.denied",
        "tool": "write_file",
        "reason": "Access denied: path '../outside.txt' is outside project scope.",
    }
    outcome = _tool_outcome([event], {})

    assert outcome is not None
    assert outcome["success"] is False
    assert outcome["dispatched"] is False
    assert outcome["ambiguous"] is False
    assert outcome["error"] == event["reason"]


@pytest.mark.parametrize("expectation_kind", ["observed", "expected_refusal"])
def test_ambiguous_tool_outcome_precedes_observed_and_refusal_matching(
    expectation_kind: str,
) -> None:
    expectation = (
        ExpectedOutcome("expected_refusal", "outside project scope")
        if expectation_kind == "expected_refusal"
        else ExpectedOutcome("observed")
    )
    request = StepRequest(
        label="ambiguous refusal",
        prompt="Read the requested path.",
        file_path="probe.txt",
        content="",
        overwrite=False,
        expectation=expectation,
        tool_name="read_file",
    )

    assert _classify(
        request,
        {"exit_code": 0, "session_id_in_result": True},
        {
            "success": False,
            "ambiguous": True,
            "error": "Access denied: outside project scope",
        },
        None,
        None,
        [],
        {},
    ) == ("ambiguous_outcome", "tool dispatch outcome is ambiguous")


def test_tool_result_extracted_from_actual_ash_response_wrapper() -> None:
    content = (
        '<tool_response name="write_file" call_id="call-1">\n'
        '{"success":false,"error":"Access denied: outside project scope"}\n'
        "</tool_response>"
    )
    request = {
        "messages": [{"role": "tool", "tool_call_id": "call-1", "content": content}]
    }

    outcome = _extract_tool_outcome(request, "call-1")

    assert outcome is not None
    assert outcome["success"] is False
    assert outcome["error"] == "Access denied: outside project scope"
    assert outcome["content"] == content


def test_search_tool_result_unescapes_only_the_xml_wrapper_body() -> None:
    payload = {
        "success": True,
        "output": "notes.txt:1:<tag> & literal &amp; value",
        "error": None,
        "truncated": False,
    }
    content = (
        '<tool_response name="search_text" call_id="call-1">\n'
        f"{xml_escape(json.dumps(payload, ensure_ascii=False, indent=2))}\n"
        "</tool_response>"
    )
    request = {
        "messages": [{"role": "tool", "tool_call_id": "call-1", "content": content}]
    }

    outcome = _extract_tool_outcome(request, "call-1", "search_text")

    assert outcome is not None
    assert outcome["success"] is True
    assert outcome["truncated"] is False
    assert outcome["output"] == payload["output"]


def test_search_fixture_xml_expansion_has_a_bounded_allowance() -> None:
    output = "&" * explorer.MAX_SEARCH_OUTPUT_BYTES
    payload = {
        "success": True,
        "output": output,
        "error": None,
        "truncated": False,
    }
    content = (
        '<tool_response name="search_text" call_id="call-1">\n'
        f"{xml_escape(json.dumps(payload, ensure_ascii=False))}\n"
        "</tool_response>"
    )
    request = {
        "messages": [{"role": "tool", "tool_call_id": "call-1", "content": content}]
    }

    outcome = _extract_tool_outcome(request, "call-1", "search_text")

    assert outcome is not None
    assert outcome["output"] == output


def test_search_discovery_requires_successfully_offered_exact_schema() -> None:
    from ash.tools.search import SearchTextArgs

    schema = SearchTextArgs.model_json_schema()
    request = {
        "tools": [
            {
                "function": {
                    "name": "search_text",
                    "parameters": schema,
                }
            }
        ]
    }
    discovery = json.dumps(
        {
            "tools": [
                {
                    "name": "search_text",
                    "input_schema": schema,
                    "schema_truncated": False,
                }
            ]
        },
        separators=(",", ":"),
    )

    assert explorer._search_discovery_matches_offered_schema(discovery, request)
    assert not explorer._search_discovery_matches_offered_schema(
        discovery.replace('"schema_truncated":false', '"schema_truncated":true'),
        request,
    )
    changed_offer = {
        "tools": [
            {
                "function": {
                    "name": "search_text",
                    "parameters": {**schema, "description": "changed"},
                }
            }
        ]
    }
    assert not explorer._search_discovery_matches_offered_schema(
        discovery, changed_offer
    )


def test_read_action_rejects_unlisted_tools_and_arguments() -> None:
    def action(name: str, arguments: dict[str, object]) -> bytes:
        return json.dumps(
            {
                "action": "step",
                "label": "probe",
                "prompt": "read",
                "tool_call": {"name": name, "arguments": arguments},
                "expect": {"kind": "observed"},
            }
        ).encode()

    _, request = explorer._parse_action(action("read_file", {"file_path": "probe.txt"}))
    assert request is not None
    assert request.tool_name == "read_file"
    assert request.tool_arguments == {"file_path": "probe.txt"}
    with pytest.raises(ExplorerError, match="read_file arguments"):
        explorer._parse_action(
            action("read_file", {"file_path": "probe.txt", "content": "unexpected"})
        )
    with pytest.raises(
        ExplorerError,
        match="only write_file, read_file, list_dir, glob_files, and search_text",
    ):
        explorer._parse_action(action("execute_shell", {"command": "pwd"}))


def test_search_text_action_requires_the_exact_bounded_literal_query() -> None:
    def parse(arguments: dict[str, object]) -> StepRequest:
        action = {
            "action": "step",
            "label": "search",
            "prompt": "Search the disposable project.",
            "tool_call": {"name": "search_text", "arguments": arguments},
            "expect": {"kind": "expected_success"},
        }
        _, request = explorer._parse_action(json.dumps(action).encode("utf-8"))
        assert request is not None
        return request

    valid = {
        "pattern": "literal [needle]",
        "directory_path": ".",
        "glob": None,
        "fixed_strings": True,
        "case_sensitive": True,
        "max_results": 64,
    }
    request = parse(valid)
    assert request.tool_name == "search_text"
    assert request.search_query == SearchQuery("literal [needle]")
    assert request.tool_arguments == valid
    for invalid in (
        {**valid, "pattern": ""},
        {**valid, "pattern": "é" * 129},
        {**valid, "pattern": "needle\n"},
        {**valid, "directory_path": "./"},
        {**valid, "glob": "../*.txt"},
        {**valid, "glob": "bad\n*.txt"},
        {**valid, "glob": "é" * 65},
        {**valid, "fixed_strings": 1},
        {**valid, "case_sensitive": False},
        {**valid, "max_results": True},
        {**valid, "max_results": 32},
        {key: value for key, value in valid.items() if key != "glob"},
        {**valid, "extra": "value"},
    ):
        with pytest.raises(ExplorerError):
            parse(invalid)


def test_search_snapshot_is_stable_and_hashes_all_glob_selected_files(
    tmp_path: Path,
) -> None:
    (tmp_path / "keep.txt").write_bytes(b"first needle\r\nsecond\rneedle again\n")
    (tmp_path / "also.txt").write_bytes(b"no match\n")
    (tmp_path / "ignored.md").write_bytes(b"needle\n")
    query = SearchQuery("needle", glob="*.txt")

    first = _search_snapshot(tmp_path, query)
    repeat = _search_snapshot(tmp_path, query)

    assert first.complete is True
    assert first.expected_matches == (
        "keep.txt:1:first needle",
        "keep.txt:3:needle again",
    )
    assert repeat.sha256 == first.sha256
    (tmp_path / "also.txt").write_bytes(b"changed but still no match\n")
    changed_selected_file = _search_snapshot(tmp_path, query)
    assert changed_selected_file.sha256 != first.sha256
    assert changed_selected_file.expected_matches == first.expected_matches
    (tmp_path / "ignored.md").write_bytes(b"changed outside the glob\n")
    changed_unselected_file = _search_snapshot(tmp_path, query)
    assert changed_unselected_file.sha256 == changed_selected_file.sha256


@pytest.mark.parametrize(
    ("kind", "detail"),
    [
        ("symlink", "stable workspace"),
        ("invalid_utf8", "stable workspace"),
        ("long_file", "file limit"),
        ("long_path", "safe path"),
    ],
)
def test_search_snapshot_rejects_unverifiable_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, detail: str
) -> None:
    if kind == "symlink":
        target = tmp_path / "target.txt"
        target.write_text("needle\n", encoding="utf-8")
        try:
            (tmp_path / "link.txt").symlink_to(target)
        except OSError as exc:
            pytest.skip(f"symlink unavailable: {exc}")
    elif kind == "invalid_utf8":
        (tmp_path / "bad.txt").write_bytes(b"\xff")
    elif kind == "long_file":
        monkeypatch.setattr(explorer, "MAX_SEARCH_FILE_BYTES", 2)
        (tmp_path / "large.txt").write_text("needle", encoding="utf-8")
    else:
        long_path = tmp_path
        for index in range(5):
            long_path = long_path / (f"level-{index}-" + "x" * 220)
        long_path.mkdir(parents=True)
        (long_path / "needle.txt").touch()

    snapshot = _search_snapshot(tmp_path, SearchQuery("needle"))
    assert snapshot.complete is False
    assert snapshot.sha256 is None
    assert snapshot.expected_matches is None
    assert detail in (snapshot.detail or "")


def test_search_snapshot_rejects_entry_depth_and_aggregate_caps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "one.txt").write_text("needle", encoding="utf-8")
    (tmp_path / "two.txt").write_text("needle", encoding="utf-8")
    monkeypatch.setattr(explorer, "MAX_SEARCH_TOTAL_BYTES", 8)
    assert "aggregate limit" in (
        _search_snapshot(tmp_path, SearchQuery("needle")).detail or ""
    )

    monkeypatch.setattr(explorer, "MAX_SEARCH_TOTAL_BYTES", 128 * 1024)
    monkeypatch.setattr(explorer, "MAX_SEARCH_SCAN_ENTRIES", 1)
    assert "entry limit" in (
        _search_snapshot(tmp_path, SearchQuery("needle")).detail or ""
    )

    monkeypatch.setattr(explorer, "MAX_SEARCH_SCAN_ENTRIES", 128)
    monkeypatch.setattr(explorer, "MAX_SEARCH_DEPTH", 2)
    deep = tmp_path / "deep" / "nested"
    deep.mkdir(parents=True)
    assert "depth limit" in (
        _search_snapshot(tmp_path, SearchQuery("needle")).detail or ""
    )


def test_search_verification_requires_complete_consistent_nontruncated_evidence(
    tmp_path: Path,
) -> None:
    before = SearchSnapshot("a" * 64, ("probe.txt:1:needle",), True)
    after = SearchSnapshot("a" * 64, ("probe.txt:1:needle",), True)
    output = "probe.txt:1:needle"
    outcome: dict[str, object] = {
        "event_type": "tool.completed",
        "success": True,
        "error": None,
        "output": output,
        "truncated": False,
        "fixture_truncated": False,
        "ambiguous": False,
        "fixture_result": {
            "success": True,
            "output": output,
            "error": None,
            "truncated": False,
        },
    }
    request = StepRequest(
        label="search",
        prompt="Search for the literal.",
        file_path=".",
        content="",
        overwrite=False,
        expectation=ExpectedOutcome("expected_success"),
        tool_name="search_text",
        search_query=SearchQuery("needle"),
    )
    status = {"exit_code": 0, "session_id_in_result": True}

    verified = _verify_search(before, after, outcome, 1)
    assert verified.verified is True
    assert _classify(
        request,
        status,
        outcome,
        None,
        None,
        [],
        {},
        search_verification=verified,
    ) == ("expected_success", None)

    for changed in (
        {**outcome, "output": "probe.txt:1:wrong"},
        {
            **outcome,
            "fixture_result": {
                **outcome["fixture_result"],
                "output": "probe.txt:1:wrong",
            },
        },
        {**outcome, "truncated": True, "fixture_truncated": True},
        {**outcome, "truncated": None, "fixture_truncated": None},
    ):
        verification = _verify_search(before, after, changed, 1)
        assert verification.verified is False
    missing_truncation = _verify_search(
        before,
        after,
        {**outcome, "truncated": None, "fixture_truncated": None},
        1,
    )
    assert (
        _classify(
            request,
            status,
            {**outcome, "truncated": None, "fixture_truncated": None},
            None,
            None,
            [],
            {},
            search_verification=missing_truncation,
        )[0]
        == "observed"
    )

    changed_workspace = _verify_search(
        before, SearchSnapshot("b" * 64, before.expected_matches, True), outcome, 1
    )
    assert changed_workspace.verified is False
    assert changed_workspace.evidence_complete is True
    ambiguous = _verify_search(before, after, {**outcome, "ambiguous": True}, 1)
    assert _classify(
        request,
        {"exit_code": 7},
        {**outcome, "ambiguous": True},
        None,
        None,
        [],
        {},
        search_verification=ambiguous,
    ) == ("ambiguous_outcome", "tool dispatch outcome is ambiguous")


def test_declared_search_refusal_uses_stable_snapshots_without_truncation_fields() -> (
    None
):
    before = SearchSnapshot("a" * 64, (), True)
    after = SearchSnapshot("a" * 64, (), True)
    outcome: dict[str, object] = {
        "event_type": "tool.denied",
        "success": False,
        "error": "search denied by policy",
        "ambiguous": False,
    }
    request = StepRequest(
        label="search refusal",
        prompt="Search for a restricted query.",
        file_path=".",
        content="",
        overwrite=False,
        expectation=ExpectedOutcome("expected_refusal", "denied by policy"),
        tool_name="search_text",
        search_query=SearchQuery("needle"),
    )
    verification = _verify_search(before, after, outcome, 1)

    assert verification.verified is False
    assert verification.before_sha256 == verification.after_sha256
    assert _classify(
        request,
        {"exit_code": 0, "session_id_in_result": True},
        outcome,
        before.sha256,
        after.sha256,
        [],
        {},
        search_verification=verification,
    ) == ("expected_refusal", None)
    assert (
        _classify(
            request,
            {"exit_code": 0, "session_id_in_result": True},
            {**outcome, "ambiguous": None},
            before.sha256,
            after.sha256,
            [],
            {},
            search_verification=verification,
        )[0]
        == "unexpected_failure"
    )
    changed = _verify_search(before, SearchSnapshot("b" * 64, (), True), outcome, 1)
    assert (
        _classify(
            request,
            {"exit_code": 0, "session_id_in_result": True},
            outcome,
            before.sha256,
            "b" * 64,
            [],
            {},
            search_verification=changed,
        )[0]
        == "unexpected_failure"
    )


def test_list_directory_action_is_read_only_and_bounded(tmp_path: Path) -> None:
    def action(arguments: dict[str, object]) -> bytes:
        return json.dumps(
            {
                "action": "step",
                "label": "list",
                "prompt": "Inspect the disposable project.",
                "tool_call": {"name": "list_dir", "arguments": arguments},
                "expect": {"kind": "expected_success"},
            }
        ).encode()

    valid = {"directory_path": ".", "recursive": False, "max_results": 64}
    _, request = explorer._parse_action(action(valid))
    assert request is not None and request.tool_name == "list_dir"
    assert request.tool_arguments == valid
    for changed in (
        {**valid, "recursive": True},
        {**valid, "max_results": 200},
        {**valid, "max_results": True},
        {**valid, "extra": "unsafe"},
        {**valid, "directory_path": "/tmp"},
    ):
        with pytest.raises(ExplorerError):
            explorer._parse_action(action(changed))

    directory = tmp_path / "project"
    directory.mkdir()
    (directory / "alpha.txt").write_text("alpha")
    (directory / "subdir").mkdir()
    digest, listing = explorer._directory_snapshot(directory)
    assert listing == "alpha.txt\nsubdir/"
    assert digest is not None
    outcome = {"success": True, "ambiguous": False, "output": listing}
    status = {"exit_code": 0, "session_id_in_result": True}
    assert _classify(
        request, status, outcome, digest, digest, [], {}, directory_listing=listing
    ) == ("expected_success", None)
    wrong = {**outcome, "output": "alpha.txt"}
    assert (
        _classify(
            request, status, wrong, digest, digest, [], {}, directory_listing=listing
        )[0]
        == "unexpected_failure"
    )
    assert (
        _classify(
            request,
            status,
            outcome,
            digest,
            "f" * 64,
            [],
            {},
            directory_listing=listing,
        )[0]
        == "unexpected_failure"
    )


def test_list_directory_snapshot_rejects_symlinks_and_excess_entries(
    tmp_path: Path,
) -> None:
    (tmp_path / "safe.txt").write_text("ok")
    link = tmp_path / "link"
    try:
        link.symlink_to(tmp_path / "safe.txt")
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")
    with pytest.raises(ExplorerError, match="symlinks"):
        explorer._directory_snapshot(tmp_path)
    link.unlink()
    for i in range(explorer.MAX_DIRECTORY_ENTRIES + 1):
        (tmp_path / f"entry-{i:03}").touch()
    with pytest.raises(ExplorerError, match="entry limit"):
        explorer._directory_snapshot(tmp_path)


def test_glob_files_requires_bounded_workspace_relative_pattern(tmp_path: Path) -> None:
    def parse(arguments: dict[str, object]) -> StepRequest:
        action = {
            "action": "step",
            "label": "find files",
            "prompt": "Discover matching filenames.",
            "tool_call": {"name": "glob_files", "arguments": arguments},
            "expect": {"kind": "expected_success"},
        }
        _, parsed = explorer._parse_action(json.dumps(action).encode())
        assert parsed is not None
        return parsed

    arguments = {"pattern": "*.txt", "directory_path": ".", "max_results": 64}
    request = parse(arguments)
    assert request.tool_name == "glob_files"
    assert request.tool_arguments == arguments
    for invalid in (
        {**arguments, "directory_path": "../"},
        {**arguments, "max_results": 65},
        {**arguments, "max_results": True},
        {**arguments, "pattern": "../*.txt"},
        {**arguments, "pattern": "/tmp/*.txt"},
        {**arguments, "pattern": "a\nb"},
        {**arguments, "extra": "value"},
    ):
        with pytest.raises(ExplorerError):
            parse(invalid)

    (tmp_path / "alpha.txt").write_text("text", encoding="utf-8")
    (tmp_path / "skip.md").write_text("text", encoding="utf-8")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "beta.txt").write_text("text", encoding="utf-8")
    digest, names = explorer._glob_snapshot(tmp_path, "*.txt")
    assert names == "alpha.txt\nnested/beta.txt"
    assert digest is not None
    outcome = {"success": True, "ambiguous": False, "output": names}
    status = {"exit_code": 0, "session_id_in_result": True}
    assert _classify(
        request, status, outcome, digest, digest, [], {}, directory_listing=names
    ) == (
        "expected_success",
        None,
    )
    assert (
        _classify(
            request,
            status,
            {**outcome, "output": "alpha.txt"},
            digest,
            digest,
            [],
            {},
            directory_listing=names,
        )[0]
        == "unexpected_failure"
    )
    assert (
        _classify(
            request,
            status,
            outcome,
            digest,
            "b" * 64,
            [],
            {},
            directory_listing=names,
        )[0]
        == "unexpected_failure"
    )


def test_glob_snapshot_rejects_symlinks_and_unbounded_scans(tmp_path: Path) -> None:
    (tmp_path / "safe.txt").write_text("ok", encoding="utf-8")
    link = tmp_path / "link"
    try:
        link.symlink_to(tmp_path / "safe.txt")
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")
    with pytest.raises(ExplorerError, match="symlinks"):
        explorer._glob_snapshot(tmp_path, "*.txt")
    link.unlink()
    for i in range(explorer.MAX_GLOB_SCAN_ENTRIES):
        (tmp_path / f"other-{i:03}.md").touch()
    with pytest.raises(ExplorerError, match="scan limit"):
        explorer._glob_snapshot(tmp_path, "*.txt")


def test_sigterm_enters_the_runner_cleanup_path() -> None:
    with pytest.raises(KeyboardInterrupt, match="received signal"):
        _handle_termination(signal.SIGTERM, None)


def test_target_path_never_leaves_the_owned_scratch(tmp_path: Path) -> None:
    scratch = tmp_path / "run"
    project = scratch / "project"
    project.mkdir(parents=True)

    assert _target_path(scratch, project, "../outside-marker.txt") == (
        scratch / "outside-marker.txt"
    )
    with pytest.raises(ExplorerError, match="disposable scratch"):
        _target_path(scratch, project, "../../outside.txt")


def test_interrupted_step_is_recorded_as_unknown_and_finishes_as_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = tmp_path / "evidence"
    (evidence / "steps").mkdir(parents=True)
    scratch = tmp_path / "scratch"
    project = scratch / "project"
    project.mkdir(parents=True)
    action = {
        "action": "step",
        "label": "interrupted write",
        "prompt": "Create probe.txt.",
        "tool_call": {
            "name": "write_file",
            "arguments": {
                "file_path": "probe.txt",
                "content": "probe\n",
                "overwrite": False,
            },
        },
        "expect": {"kind": "observed"},
    }
    action_line = json.dumps(action, separators=(",", ":")).encode() + b"\n"

    class InterruptedRun:
        def __init__(self) -> None:
            self.evidence = evidence
            self.scratch = scratch
            self.project = project
            self.seed = 23
            self.step_index = 0
            self.action_accepted = False
            self.observations = []
            self.accepted = bytearray()
            self.manifest_updates: list[dict[str, object] | None] = []
            self.finish_reason = ""

        def append_accepted_line(self, raw_line: bytes) -> None:
            self.accepted.extend(raw_line)

        def write_manifest(self, *, in_flight: dict[str, object] | None = None) -> None:
            self.manifest_updates.append(in_flight)

        def run_step(self, _request: object, raw_line: bytes) -> None:
            self.append_accepted_line(raw_line)
            self.action_accepted = True
            raise KeyboardInterrupt("received signal 15")

        def close(self, *, reason: str) -> dict[str, str]:
            self.finish_reason = reason
            return {"finish_reason": reason}

    run = InterruptedRun()
    monkeypatch.setattr(explorer, "_create_run", lambda _path, _seed: run)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(action_line)))
    monkeypatch.setattr(sys, "stdout", io.StringIO())

    exit_code = explorer._run_live(Namespace(evidence=tmp_path / "unused", seed=23))

    assert exit_code == 130
    assert run.finish_reason == "interrupted"
    assert bytes(run.accepted) == action_line
    observation = json.loads(
        (evidence / "steps" / "01" / "observation.json").read_text()
    )
    assert observation["classification"] == "driver_failure"
    assert observation["process_status"]["outcome_unknown"] is True
    assert "KeyboardInterrupt" in observation["detail"]
    assert run.manifest_updates[-2] == {
        "step_index": 1,
        "label": "interrupted write",
        "dispatch_pending": False,
        "outcome_unknown": True,
    }


def test_search_driver_failure_does_not_hash_the_project_directory(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "evidence"
    (evidence / "steps").mkdir(parents=True)
    scratch = tmp_path / "scratch"
    project = scratch / "project"
    project.mkdir(parents=True)
    raw_line = (
        json.dumps(
            {
                "action": "step",
                "label": "search failure",
                "prompt": "Search for the literal.",
                "tool_call": {
                    "name": "search_text",
                    "arguments": {
                        "pattern": "needle",
                        "directory_path": ".",
                        "glob": None,
                        "fixed_strings": True,
                        "case_sensitive": True,
                        "max_results": 64,
                    },
                },
                "expect": {"kind": "expected_success"},
            },
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )
    _, request = explorer._parse_action(raw_line)
    assert request is not None

    class FailingRun:
        def __init__(self) -> None:
            self.evidence = evidence
            self.scratch = scratch
            self.project = project
            self.step_index = 0
            self.action_accepted = False
            self.observations: list[object] = []
            self.accepted = bytearray()

        def append_accepted_line(self, line: bytes) -> None:
            self.accepted.extend(line)

        def write_manifest(self, *, in_flight=None) -> None:
            del in_flight

        def run_step(self, _request: object, _line: bytes) -> None:
            raise RuntimeError("fixture dispatch failed")

    run = FailingRun()
    observation, interrupted = explorer._execute_step(run, request, raw_line)

    assert interrupted is False
    assert observation.classification == "driver_failure"
    assert observation.target_before_sha256 is None
    assert observation.target_after_sha256 is None
    assert run.observations == [observation]
    assert (
        json.loads((evidence / "steps" / "01" / "observation.json").read_text())[
            "classification"
        ]
        == "driver_failure"
    )


def _child_loopback_bind_error(environment: dict[str, str]) -> str | None:
    probe_code = (
        "import json, socket, sys\n"
        "sock = None\n"
        "try:\n"
        "    sock = socket.socket()\n"
        "    sock.bind(('127.0.0.1', 0))\n"
        "except OSError as error:\n"
        "    print(json.dumps({'errno': error.errno, 'error': str(error)}))\n"
        "    sys.exit(1)\n"
        "else:\n"
        "    sock.close()\n"
        "    print(json.dumps({'available': True}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe_code],
        env=environment,
        capture_output=True,
        timeout=10,
        check=False,
    )
    if result.returncode == 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        pytest.fail(
            "child loopback capability probe failed without a readable errno: "
            f"{result.stderr.decode('utf-8', errors='replace')} ({exc})"
        )
    if payload.get("errno") in {errno.EPERM, errno.EACCES}:
        return str(payload.get("error", "permission denied"))
    pytest.fail(f"child loopback capability probe failed: {payload!r}")


def test_agent_directed_run_adapts_after_real_write_observation(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "evidence"
    environment = {
        "PATH": os.defpath,
        "HOME": str(tmp_path / "runner-home"),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    loopback_error = _child_loopback_bind_error(environment)
    if loopback_error is not None:
        pytest.skip(f"child-process loopback socket is unavailable: {loopback_error}")

    first_action = {
        "action": "step",
        "label": "ordinary write",
        "prompt": "Create probe.txt with the requested contents.",
        "tool_call": {
            "name": "write_file",
            "arguments": {
                "file_path": "probe.txt",
                "content": "probe-ok\n",
                "overwrite": False,
            },
        },
        "expect": {"kind": "expected_success"},
    }
    first_line = json.dumps(first_action, separators=(",", ":")).encode() + b"\n"

    process = subprocess.Popen(
        [
            sys.executable,
            str(RUNNER),
            "run",
            "--evidence",
            str(evidence),
            "--seed",
            "23",
        ],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    output_lines: queue.Queue[bytes | None] = queue.Queue()
    stderr_lines: list[bytes] = []

    def read_stdout() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            output_lines.put(line)
        output_lines.put(None)

    def read_stderr() -> None:
        assert process.stderr is not None
        stderr_lines.extend(process.stderr)

    stdout_thread = threading.Thread(target=read_stdout, daemon=True)
    stderr_thread = threading.Thread(target=read_stderr, daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    def receive() -> dict[str, object]:
        try:
            line = output_lines.get(timeout=55)
        except queue.Empty:
            pytest.fail(
                f"Ash Explorer produced no controller response; status={process.poll()}, "
                f"stderr={b''.join(stderr_lines).decode('utf-8', errors='replace')}"
            )
        if line is None:
            pytest.fail(
                f"Ash Explorer closed stdout; status={process.poll()}, "
                f"stderr={b''.join(stderr_lines).decode('utf-8', errors='replace')}"
            )
        return json.loads(line)

    try:
        assert process.stdin is not None
        started = receive()
        assert started["type"] == "started"

        process.stdin.write(first_line)
        process.stdin.flush()
        first_observation = receive()
        assert first_observation["type"] == "observation"
        assert first_observation["classification"] == "expected_success"
        assert (
            first_observation["target_after_sha256"]
            == hashlib.sha256(b"probe-ok\n").hexdigest()
        )

        second_action = {
            "action": "step",
            "label": "read back written file",
            "prompt": "Read the file that was just created.",
            "tool_call": {
                "name": "read_file",
                "arguments": {"file_path": "probe.txt"},
            },
            "expect": {"kind": "expected_success"},
        }
        second_line = json.dumps(second_action, separators=(",", ":")).encode() + b"\n"
        process.stdin.write(second_line)
        process.stdin.flush()
        second_observation = receive()
        assert second_observation["type"] == "observation"
        assert second_observation["classification"] == "expected_success"
        assert "1: probe-ok" in second_observation["tool_outcome"]["output"]

        third_action = {
            "action": "step",
            "label": "adjacent scratch read escape",
            "prompt": "Try the requested read exactly.",
            "tool_call": {
                "name": "read_file",
                "arguments": {"file_path": "../outside-marker.txt"},
            },
            "expect": {
                "kind": "expected_refusal",
                "error_contains": "outside project scope",
            },
        }
        third_line = json.dumps(third_action, separators=(",", ":")).encode() + b"\n"
        process.stdin.write(third_line)
        process.stdin.flush()
        third_observation = receive()
        assert third_observation["type"] == "observation"
        assert third_observation["classification"] == "expected_refusal"

        fourth_action = {
            "action": "step",
            "label": "adjacent scratch write escape",
            "prompt": "Attempt a prohibited write to the synthetic outside marker.",
            "tool_call": {
                "name": "write_file",
                "arguments": {
                    "file_path": "../outside-marker.txt",
                    "content": "must not replace marker\n",
                    "overwrite": True,
                },
            },
            "expect": {
                "kind": "expected_refusal",
                "error_contains": "outside project scope",
            },
        }
        fourth_line = json.dumps(fourth_action, separators=(",", ":")).encode() + b"\n"
        process.stdin.write(fourth_line)
        process.stdin.flush()
        fourth_observation = receive()
        assert fourth_observation["type"] == "observation"
        assert fourth_observation["classification"] == "expected_refusal"

        finish_line = b'{"action":"finish"}\n'
        process.stdin.write(finish_line)
        process.stdin.flush()
        finished = receive()
        assert finished["type"] == "finished"
        process.stdin.close()
        assert process.wait(timeout=10) == 0
    finally:
        if process.poll() is None:
            if process.stdin is not None and not process.stdin.closed:
                try:
                    process.stdin.write(b'{"action":"finish"}\n')
                    process.stdin.flush()
                    process.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
            try:
                process.wait(timeout=55)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        stdout_thread.join(timeout=2)
        stderr_thread.join(timeout=2)

    accepted_actions = [first_line, second_line, third_line, fourth_line, finish_line]
    assert (evidence / "actions.jsonl").read_bytes() == b"".join(accepted_actions)

    first = json.loads((evidence / "steps" / "01" / "observation.json").read_text())
    second = json.loads((evidence / "steps" / "02" / "observation.json").read_text())
    third = json.loads((evidence / "steps" / "03" / "observation.json").read_text())
    fourth = json.loads((evidence / "steps" / "04" / "observation.json").read_text())
    assert first["tool_outcome"]["success"] is True
    assert first["target_after_sha256"] == hashlib.sha256(b"probe-ok\n").hexdigest()
    assert second["tool_outcome"]["success"] is True
    assert second["target_before_sha256"] == first["target_after_sha256"]
    assert second["target_after_sha256"] == first["target_after_sha256"]
    assert third["tool_outcome"]["success"] is False
    assert third["tool_outcome"]["ambiguous"] is False
    assert "outside project scope" in third["tool_outcome"]["error"]
    marker_hash = hashlib.sha256(b"ASH_EXPLORER_OUTSIDE_MARKER\n").hexdigest()
    assert third["target_before_sha256"] == marker_hash
    assert third["target_after_sha256"] == marker_hash
    assert fourth["tool_outcome"]["success"] is False
    assert fourth["tool_outcome"]["ambiguous"] is False
    assert fourth["tool_outcome"]["dispatched"] is False
    assert "outside project scope" in fourth["tool_outcome"]["error"]
    assert fourth["target_before_sha256"] == marker_hash
    assert fourth["target_after_sha256"] == marker_hash
    fourth_events = [
        json.loads(line)
        for line in (evidence / "steps" / "04" / "stdout.bin").read_text().splitlines()
    ]
    assert any(event.get("type") == "tool.denied" for event in fourth_events)
    assert not any(event.get("type") == "tool.started" for event in fourth_events)

    provider = json.loads((evidence / "steps" / "03" / "provider.json").read_text())
    assert provider["tool_outcome"]["success"] is False
    assert provider["tool_outcome"]["tool_call_id"] == "ash-explorer-23-3"
    events = [
        json.loads(line)
        for line in (evidence / "steps" / "03" / "stdout.bin").read_text().splitlines()
    ]
    assert any(
        event.get("type") in {"tool.completed", "tool.error"}
        and event.get("tool") == "read_file"
        and (
            event.get("success") is False
            or (
                event.get("type") == "tool.error"
                and isinstance(event.get("error"), str)
            )
        )
        for event in events
    )

    manifest = json.loads((evidence / "manifest.json").read_text())
    revision = manifest["baseline_revision"]
    assert revision is None or len(revision) in {40, 64}
    assert manifest["ash_version"] == "0.4.6"
    assert len(manifest["runner_sha256"]) == 64
    assert manifest["seed"] == 23
    assert manifest["pending_cleanup"] is False
    assert not Path(manifest["scratch_path"]).exists()

    replay_evidence = tmp_path / "replay"
    replay = subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            "replay",
            "--steps",
            str(evidence / "actions.jsonl"),
            "--evidence",
            str(replay_evidence),
            "--seed",
            "23",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=55,
        check=False,
    )
    assert replay.returncode == 0, replay.stderr
    replay_events = [json.loads(line) for line in replay.stdout.splitlines()]
    assert [
        event["classification"]
        for event in replay_events
        if event["type"] == "observation"
    ] == [
        "expected_success",
        "expected_success",
        "expected_refusal",
        "expected_refusal",
    ]
    for index in ("01", "02", "03", "04"):
        original = json.loads(
            (evidence / "steps" / index / "observation.json").read_text()
        )
        repeated = json.loads(
            (replay_evidence / "steps" / index / "observation.json").read_text()
        )
        assert repeated["target_after_sha256"] == original["target_after_sha256"]
    replay_manifest = json.loads((replay_evidence / "manifest.json").read_text())
    assert replay_manifest["pending_cleanup"] is False
    assert not Path(replay_manifest["scratch_path"]).exists()
