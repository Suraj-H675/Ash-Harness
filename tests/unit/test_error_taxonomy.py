import io
import json
from types import SimpleNamespace

import pytest

from ash.cli import _bootstrap_and_headless, _bootstrap_and_repl
from config import AshConfig
from core.planner import PlannerError
from core.session import SessionStorageError
from exceptions import ErrorCategory, classify_exception, format_error
from safety.guard import SafetyViolation
from sandbox import SandboxBackendUnavailable
from ui.headless import HeadlessUI


class _ProviderFailure(RuntimeError):
    pass


_ProviderFailure.__module__ = "providers.fake"


def test_classify_config_validation_error() -> None:
    with pytest.raises(Exception) as caught:
        AshConfig(model="missing-provider-prefix")

    error = classify_exception(caught.value)

    assert error.category == ErrorCategory.CONFIG
    assert "model" in error.message
    assert error.exit_code == 2


def test_classify_policy_storage_sandbox_and_context_errors() -> None:
    assert classify_exception(SafetyViolation("blocked")).category == ErrorCategory.POLICY
    assert (
        classify_exception(SessionStorageError("database corrupt")).category
        == ErrorCategory.STORAGE
    )
    sandbox = classify_exception(SandboxBackendUnavailable("bwrap missing"))
    assert sandbox.category == ErrorCategory.SANDBOX
    assert sandbox.retriable is True
    assert classify_exception(PlannerError("bad plan")).category == ErrorCategory.CONTEXT


def test_classify_provider_error_is_retriable_for_transient_failures() -> None:
    error = classify_exception(_ProviderFailure("OpenAI API error: timeout"))

    assert error.category == ErrorCategory.PROVIDER
    assert error.retriable is True
    assert "API key" in error.remedy


def test_format_error_includes_category_and_remedy() -> None:
    rendered = format_error(classify_exception(SafetyViolation("denied")))

    assert "Error [policy]: denied" in rendered
    assert "Remedy:" in rendered


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
