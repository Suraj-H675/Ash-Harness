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


@pytest.mark.parametrize(
    ("assignment", "marker"),
    [
        ('password="synthetic quoted marker"', "synthetic quoted marker"),
        ("password='synthetic quoted marker'", "synthetic quoted marker"),
        ("password=synthetic-unquoted-marker", "synthetic-unquoted-marker"),
    ],
)
def test_redaction_removes_complete_assigned_secret_values(
    assignment: str, marker: str
) -> None:
    rendered = redact_text(f"before {assignment} after")

    assert marker not in rendered
    assert rendered.endswith(" after")
    assert "[REDACTED]" in rendered


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


def test_streaming_redactor_never_emits_chunk_split_quoted_secret() -> None:
    marker = "synthetic quoted marker"

    for quote in ('"', "'"):
        value = f"prefix password={quote}{marker}{quote} suffix\n"
        for split in range(len(value) + 1):
            redactor = StreamingRedactor()
            emitted = (
                redactor.feed(value[:split])
                + redactor.feed(value[split:])
                + redactor.finish()
            )

            assert marker not in emitted
            assert f"password={quote}[REDACTED]{quote}" in emitted
            assert emitted.endswith(" suffix\n")


def test_redaction_handles_nested_escaped_and_multiline_assignments() -> None:
    escaped = r'payload={\"password\": \"synthetic escaped marker\"}'
    multiline = 'payload={"password": "line one\nline two synthetic marker"}'

    assert "synthetic escaped marker" not in redact_text(escaped)
    assert "line two synthetic marker" not in redact_text(multiline)


def test_redaction_handles_escaped_quotes_without_consuming_following_fields() -> None:
    value = (
        r'payload={\"password\": \"secret with \\\"quote\\\" '
        r'synthetic escaped marker\", \"other\": \"safe\"}'
    )

    rendered = redact_text(value)

    assert "synthetic escaped marker" not in rendered
    assert r'\"password\": \"[REDACTED]\"' in rendered
    assert r'\"other\": \"safe\"' in rendered


@pytest.mark.parametrize(
    "value",
    [
        r'prefix payload={\"password\": \"synthetic escaped marker\"} suffix\n',
        (
            r'prefix payload={\"password\": \"secret with \\\"quote\\\" '
            r'synthetic escaped marker\", \"other\": \"safe\"} suffix\n'
        ),
        'prefix payload={"password": "line one\nline two synthetic marker"} suffix\n',
    ],
)
def test_streaming_redactor_never_emits_nested_or_multiline_secret(
    value: str,
) -> None:
    marker = "synthetic escaped marker" if "escaped" in value else "synthetic marker"

    for split in range(len(value) + 1):
        redactor = StreamingRedactor()
        emitted = (
            redactor.feed(value[:split])
            + redactor.feed(value[split:])
            + redactor.finish()
        )

        assert marker not in emitted


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
