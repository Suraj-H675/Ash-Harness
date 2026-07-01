import pytest

from core.redaction import StreamingRedactor, redact_text, redact_value
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


def test_streaming_redactor_retains_chunk_split_secrets() -> None:
    redactor = StreamingRedactor()

    assert redactor.feed("progress token=super") == "progress "
    emitted = redactor.feed("secretvalue\nnext")
    tail = redactor.finish()

    assert "supersecretvalue" not in emitted
    assert "[REDACTED]" in emitted
    assert tail == "next"


def test_streaming_redactor_withholds_unbounded_tokens() -> None:
    redactor = StreamingRedactor(max_token_characters=256)

    emitted = redactor.feed("x" * 257)
    assert emitted == "[long unbroken output token withheld]"
    assert redactor.feed("still-hidden ") == ""
    assert redactor.feed("safe\n") == "safe\n"
