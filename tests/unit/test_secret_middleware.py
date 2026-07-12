import pytest

from ash.core.redaction import (
    StreamingRedactor,
    find_secret_candidates,
    redact_text,
    redact_value,
)
from ash.core.secret_middleware import SecretRedactionMiddleware
from ash.tools.base import ToolResult


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


def test_secret_candidate_scanner_reports_kinds_without_values() -> None:
    private_key = "-----BEGIN PRIVATE KEY-----"
    provider_key = "sk-proj-abcdefghijklmnopqrstuvwxyz"
    findings = find_secret_candidates(
        f"{private_key}\nOPENAI_API_KEY={provider_key}\npassword=hunter2-secret"
    )

    assert [(finding.kind, finding.line_number) for finding in findings] == [
        ("private key", 1),
        ("provider API key", 2),
        ("secret assignment", 2),
        ("secret assignment", 3),
    ]
    assert provider_key not in repr(findings)


def test_secret_candidate_scanner_ignores_placeholders() -> None:
    assert (
        find_secret_candidates(
            'api_key="your_api_key_here"\n'
            'password="EXAMPLE_PASSWORD"\n'
            'auth_token="${AUTH_TOKEN}"'
        )
        == ()
    )
