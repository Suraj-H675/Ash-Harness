import pytest

from core.redaction import redact_text, redact_value
from core.secret_middleware import SecretRedactionMiddleware
from tools.base import ToolResult


def test_redaction_handles_common_secret_shapes() -> None:
    assert "supersecretvalue" not in redact_text("token=supersecretvalue")
    assert redact_value({"api_key": "sk-example-secret-12345"}) == {
        "api_key": "[REDACTED]"
    }


@pytest.mark.asyncio
async def test_tool_result_redaction() -> None:
    result = ToolResult(
        success=True,
        output="Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
        error="password=hunter2-secret",
    )
    await SecretRedactionMiddleware().after_tool("run_command", {}, result)
    assert "abcdefghijklmnopqrstuvwxyz" not in result.output
    assert "hunter2-secret" not in (result.error or "")
