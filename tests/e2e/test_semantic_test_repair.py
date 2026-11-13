"""A failed real test must give a coding agent actionable repair locations."""

from __future__ import annotations

import io
import shlex
import sys
from pathlib import Path

import pytest

from ash.core.loop import AshLoop
from ash.core.session import SessionStore
from ash.providers.base import ProviderABC, StreamChunk
from ash.providers.capabilities import ProviderCapabilities
from ash.safety.guard import SafetyGuard
from ash.tools.command import RunCommandTool
from ash.tools.filesystem import ReplaceFileContentTool
from ash.ui.headless import HeadlessUI


@pytest.mark.asyncio
@pytest.mark.parametrize("omit_initial_indentation", [False, True])
@pytest.mark.parametrize("noisy_command_output", [False, True])
async def test_real_pytest_failures_guide_two_module_repair_and_recheck(
    tmp_path: Path, omit_initial_indentation: bool, noisy_command_output: bool,
) -> None:
    """Exercise native model->command->structured errors->edits->recheck."""

    (tmp_path / "pricing.py").write_text(
        "def discounted_subtotal(price):\n    return price - 5\n",
        encoding="utf-8",
    )
    (tmp_path / "taxes.py").write_text(
        "def final_price(subtotal):\n    return subtotal + 11\n",
        encoding="utf-8",
    )
    (tmp_path / "test_checkout.py").write_text(
        "from pricing import discounted_subtotal\n"
        "from taxes import final_price\n\n"
        "def test_discount():\n"
        "    assert discounted_subtotal(100) == 90\n\n"
        "def test_tax():\n"
        "    assert final_price(90) == 99\n",
        encoding="utf-8",
    )
    command = (
        f"{shlex.quote(sys.executable)} -m pytest -q --no-header "
        "--tb=short test_checkout.py"
    )
    if noisy_command_output:
        # A real subprocess produces the pytest evidence after thousands of
        # unrelated stderr lines. This must survive model-visible extraction.
        (tmp_path / "noisy_runner.py").write_text(
            "import subprocess, sys\n"
            "sys.stderr.write('unrelated warning\\n' * 2400)\n"
            "sys.stderr.write('token=supersecretvalue\\n')\n"
            "result = subprocess.run([sys.executable, '-m', 'pytest', '-q', "
            "'--no-header', '--tb=short', 'test_checkout.py'], "
            "capture_output=True, text=True, check=False)\n"
            "sys.stdout.write(result.stdout)\n"
            "sys.stderr.write(result.stderr)\n"
            "sys.exit(result.returncode)\n",
            encoding="utf-8",
        )
        command = f"{shlex.quote(sys.executable)} -B noisy_runner.py"

    class TestRepairProvider(ProviderABC):
        model_name = "semantic-repair-scripted"
        _ash_declared_capabilities = ProviderCapabilities(native_tools=True)

        def __init__(self) -> None:
            self.requests = 0

        def count_tokens(self, text: str) -> int:
            return max(1, len(str(text)) // 4)

        async def stream_chat(self, messages, temperature=0.0, tools=None):
            self.requests += 1
            if self.requests == 1:
                tool_calls = [{
                    "id": "check-initial", "name": "run_command",
                    "arguments": {"command_line": command},
                }]
            elif self.requests == 2 or (
                omit_initial_indentation and self.requests == 3
            ):
                history = repr(messages)
                if self.requests == 2:
                    assert '"pytest_failed": 2' in history
                    assert '"line": 5' in history
                    assert '"line": 8' in history
                    assert '"path": "test_checkout.py"' in history
                    assert '"code": "AssertionError"' in history
                    if noisy_command_output:
                        assert "supersecretvalue" not in history
                        assert "[REDACTED]" in history
                else:
                    assert "Possible leading indentation mismatch at line 2" in history
                    assert "no edit was made" in history
                    assert (tmp_path / "pricing.py").read_text().endswith(
                        "    return price - 5\n"
                    )
                    assert (tmp_path / "taxes.py").read_text().endswith(
                        "    return subtotal + 11\n"
                    )
                omit_indent = omit_initial_indentation and self.requests == 2
                tool_calls = [{
                    "id": f"fix-pricing-{self.requests}",
                    "name": "replace_file_content",
                    "arguments": {
                        "file_path": "pricing.py",
                        "target_content": (
                            "return price - 5" if omit_indent else "    return price - 5"
                        ),
                        "replacement_content": "    return price - 10",
                    },
                }, {
                    "id": f"fix-taxes-{self.requests}",
                    "name": "replace_file_content",
                    "arguments": {
                        "file_path": "taxes.py",
                        "target_content": (
                            "return subtotal + 11"
                            if omit_indent else "    return subtotal + 11"
                        ),
                        "replacement_content": "    return subtotal + 9",
                    },
                }]
            elif self.requests == (4 if omit_initial_indentation else 3):
                tool_calls = [{
                    "id": "recheck", "name": "run_command",
                    "arguments": {"command_line": command},
                }]
            else:
                assert '"pytest_passed": 2' in repr(messages)
                yield StreamChunk(
                    content="Both modules repaired and the checks passed.",
                    is_done=True,
                )
                return
            yield StreamChunk(is_done=True, native_tool_calls=tool_calls)

    async def approve(tool_name: str, arguments: dict[str, object]) -> bool:
        if tool_name == "run_command":
            assert arguments["command_line"] == command
        return True

    guard = SafetyGuard(tmp_path)
    store = SessionStore(tmp_path / "semantic-repair.db")
    provider = TestRepairProvider()
    loop = AshLoop(
        store, provider, guard,
        HeadlessUI(output_format="stream-json", stream=io.StringIO()),
        tmp_path,
        tools={
            "run_command": RunCommandTool(guard),
            "replace_file_content": ReplaceFileContentTool(guard),
        },
        on_tool_approval=approve,
    )
    try:
        session = await loop.start_session()
        response = await loop.run_turn(
            "Correct pricing and taxes, then run the authorized pytest check."
        )
        assert response == "Both modules repaired and the checks passed."
        assert provider.requests == (5 if omit_initial_indentation else 4)
        assert "return price - 10" in (tmp_path / "pricing.py").read_text()
        assert "return subtotal + 9" in (tmp_path / "taxes.py").read_text()
        calls = store.load_session(session.session_id).tool_calls
        expected_tools = [
            "run_command", "replace_file_content", "replace_file_content",
        ]
        expected_success = [False, not omit_initial_indentation, not omit_initial_indentation]
        if omit_initial_indentation:
            expected_tools.extend(("replace_file_content", "replace_file_content"))
            expected_success.extend((True, True))
        expected_tools.append("run_command")
        expected_success.append(True)
        assert [call.tool_name for call in calls] == expected_tools
        assert [call.success for call in calls] == expected_success
        assert [calls[0].exit_code, calls[-1].exit_code] == [1, 0]
        assert store.verify_audit_log(session.session_id) == []
    finally:
        await loop.aclose()
