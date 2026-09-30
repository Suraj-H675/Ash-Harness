import io
import json
from types import SimpleNamespace

import pytest

from ash.cli import _bootstrap_and_headless, _bootstrap_and_repl, validate_structured_output
from ash.config import AshConfig
from ash.core.planner import PlannerError
from ash.core.session import SessionResolutionError, SessionStorageError
from ash.exceptions import ErrorCategory, classify_exception, format_error
from ash.safety.guard import SafetyViolation
from ash.sandbox import SandboxBackendUnavailable
from ash.ui.headless import HeadlessUI


class _ProviderFailure(RuntimeError):
    pass


_ProviderFailure.__module__ = "ash.providers.fake"


def test_classify_config_validation_error() -> None:
    with pytest.raises(Exception) as caught:
        AshConfig(model="missing-provider-prefix")

    error = classify_exception(caught.value)

    assert error.category == ErrorCategory.CONFIG
    assert "model" in error.message
    assert error.exit_code == 2


def test_classify_policy_storage_sandbox_and_context_errors() -> None:
    assert (
        classify_exception(SafetyViolation("blocked")).category == ErrorCategory.POLICY
    )
    assert (
        classify_exception(SessionStorageError("database corrupt")).category
        == ErrorCategory.STORAGE
    )
    sandbox = classify_exception(SandboxBackendUnavailable("bwrap missing"))
    assert sandbox.category == ErrorCategory.SANDBOX
    assert sandbox.retriable is True
    assert (
        classify_exception(PlannerError("bad plan")).category == ErrorCategory.CONTEXT
    )


def test_session_classification_does_not_match_unrelated_value_errors() -> None:
    assert (
        classify_exception(SessionResolutionError("session reference must not be empty")).category
        == ErrorCategory.SESSION
    )
    assert (
        classify_exception(
            ValueError("--resume without a session requires an interactive terminal")
        ).category
        == ErrorCategory.SESSION
    )
    assert (
        classify_exception(ValueError("session belongs to a different workspace")).category
        == ErrorCategory.SESSION
    )
    assert (
        classify_exception(ValueError("session_retention_days cannot be negative")).category
        != ErrorCategory.SESSION
    )


def test_classify_provider_error_is_retriable_for_transient_failures() -> None:
    error = classify_exception(_ProviderFailure("OpenAI API error: timeout"))

    assert error.category == ErrorCategory.PROVIDER
    assert error.retriable is True
    assert "API key" in error.remedy


def test_classify_structured_output_failure_as_output() -> None:
    schema = {"type": "object", "required": ["ok"]}
    with pytest.raises(ValueError) as caught:
        validate_structured_output("not-json", schema)

    error = classify_exception(caught.value)

    assert error.category == ErrorCategory.OUTPUT
    assert "regenerate" in error.remedy


def test_format_error_includes_category_and_remedy() -> None:
    rendered = format_error(classify_exception(SafetyViolation("denied")))

    assert "Error [policy]: denied" in rendered
    assert "Remedy:" in rendered


def test_main_json_prompt_reports_runtime_startup_failure_as_event(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from ash.cli import main
    import ash.runtime as runtime_module

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ASH_MODEL", "ollama/test")
    monkeypatch.setenv("ASH_DB_DIRECTORY", str(tmp_path / "db"))

    def fail_runtime(*args, **kwargs):
        del args, kwargs
        raise SandboxBackendUnavailable("sandbox unavailable")

    monkeypatch.setattr(runtime_module, "build_runtime", fail_runtime)

    code = main(["--prompt", "hello", "--output-format", "json"])

    assert code != 0
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["type"] == "error"
    assert payload["error"]["category"] == "sandbox"
    assert payload["error"]["message"] == "sandbox unavailable"


def test_main_json_prompt_reports_unsupported_platform_as_event(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from ash.cli import main
    import ash.platform_support as platform_module

    monkeypatch.setattr(
        platform_module,
        "platform_support_error",
        lambda: "native platform unsupported; use WSL2",
    )

    code = main(["--prompt", "hello", "--output-format", "json"])

    assert code == 2
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["type"] == "error"
    assert payload["error"]["category"] == "config"
    assert "native platform unsupported" in payload["error"]["message"]


def test_main_json_prompt_reports_unconfigured_provider_as_event(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from ash.cli import main
    import ash.commands.setup as setup_module

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ASH_MODEL", "ollama/test")
    monkeypatch.setenv("ASH_DB_DIRECTORY", str(tmp_path / "db"))
    monkeypatch.setattr(setup_module, "_has_provider_configured", lambda config: False)

    code = main(["--prompt", "hello", "--output-format", "json"])

    assert code != 0
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["type"] == "error"
    assert payload["error"]["category"] == "config"


def test_main_json_prompt_reports_permission_state_failure_as_event(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from ash.cli import main
    import ash.safety.grants as grants_module

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ASH_MODEL", "ollama/test")
    monkeypatch.setenv("ASH_DB_DIRECTORY", str(tmp_path / "db"))

    def fail_permissions(*args, **kwargs):
        del args, kwargs
        raise grants_module.PermissionGrantError("permission state invalid")

    monkeypatch.setattr(grants_module, "load_permission_rules", fail_permissions)

    code = main(["--prompt", "hello", "--output-format", "json"])

    assert code != 0
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["type"] == "error"
    assert payload["error"]["category"] == "policy"


def test_main_json_prompt_reports_missing_resume_session_as_event(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from ash.cli import main

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ASH_MODEL", "ollama/test")
    monkeypatch.setenv("ASH_DB_DIRECTORY", str(tmp_path / "db"))

    code = main(
        [
            "--prompt",
            "hello",
            "--output-format",
            "json",
            "--resume",
            "missing",
        ]
    )

    assert code != 0
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["type"] == "error"
    assert payload["error"]["category"] == "session"


@pytest.mark.asyncio
async def test_headless_bootstrap_emits_structured_error() -> None:
    class FailingLoop:
        async def start_session(self, session_id=None):
            return SimpleNamespace(session_id="s1")

        async def run_turn(self, prompt):
            raise _ProviderFailure("OpenAI API error: timeout")

        async def aclose(self):
            return None

    stream = io.StringIO()
    ui = HeadlessUI(output_format="stream-json", stream=stream)

    code = await _bootstrap_and_headless(
        FailingLoop(),
        SimpleNamespace(model="openai/gpt-5.2"),
        prompt="hello",
        session_id=None,
        ui=ui,
    )

    assert code == 1
    payload = json.loads(stream.getvalue())
    assert payload["type"] == "error"
    assert payload["error"]["category"] == "provider"
    assert payload["error"]["message"] == "OpenAI API error: timeout"
    assert payload["error"]["retriable"] is True


@pytest.mark.asyncio
async def test_headless_bootstrap_preserves_error_when_runtime_close_fails() -> None:
    class FailingLoop:
        async def start_session(self, session_id=None):
            return SimpleNamespace(session_id="s1")

        async def run_turn(self, prompt):
            raise _ProviderFailure("OpenAI API error: timeout")

        async def aclose(self):
            raise RuntimeError("runtime close failure")

    stream = io.StringIO()
    ui = HeadlessUI(output_format="stream-json", stream=stream)

    code = await _bootstrap_and_headless(
        FailingLoop(),
        SimpleNamespace(model="openai/gpt-5.2"),
        prompt="hello",
        session_id=None,
        ui=ui,
    )

    assert code == 1
    payload = json.loads(stream.getvalue())
    assert payload["type"] == "error"
    assert payload["error"]["category"] == "provider"
    assert payload["error"]["message"] == "OpenAI API error: timeout"


@pytest.mark.asyncio
async def test_repl_bootstrap_formats_missing_session_error(capsys) -> None:
    class MissingSessionLoop:
        closed = False

        async def start_session(self, session_id=None):
            raise KeyError(f"Session not found: {session_id}")

        async def aclose(self):
            self.closed = True

    loop = MissingSessionLoop()

    code = await _bootstrap_and_repl(
        loop,
        SimpleNamespace(),
        SimpleNamespace(),
        session_id="missing",
    )

    assert code == 1
    assert loop.closed is True
    stderr = capsys.readouterr().err
    assert "Error [session]: Session not found: missing" in stderr
    assert "List sessions" in stderr


@pytest.mark.asyncio
async def test_repl_bootstrap_preserves_error_when_runtime_close_fails(capsys) -> None:
    class MissingSessionLoop:
        recovery_summary = None
        recovered_turns = []

        async def start_session(self, session_id=None):
            raise KeyError(f"Session not found: {session_id}")

        async def aclose(self):
            raise RuntimeError("runtime close failure")

    code = await _bootstrap_and_repl(
        MissingSessionLoop(),
        SimpleNamespace(),
        SimpleNamespace(),
        session_id="missing",
    )

    assert code == 1
    stderr = capsys.readouterr().err
    assert "Error [session]: Session not found: missing" in stderr
    assert "List sessions" in stderr
