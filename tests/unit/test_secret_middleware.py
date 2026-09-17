import pytest

import ash.core.redaction as redaction_module
from ash.core.redaction import (
    LONG_TOKEN_WITHHELD_MARKER,
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
    "provider_key",
    [
        "sk-or-v1-" + "a" * 64,
        "xai-" + "a" * 80,
        "csk-" + "a" * 40,
        "csk_" + "b" * 40,
    ],
)
def test_redaction_handles_bare_provider_api_keys(provider_key: str) -> None:
    rendered = redact_text(f"provider echoed {provider_key}")

    assert provider_key not in rendered
    assert "[REDACTED]" in rendered
    findings = find_secret_candidates(f"provider echoed {provider_key}")
    assert [(item.kind, item.line_number) for item in findings] == [
        ("provider API key", 1)
    ]


@pytest.mark.parametrize(
    ("header", "safe_prefix"),
    [
        ("Authorization: Basic dXNlcjpwYXNz", "Authorization: Basic "),
        ("Authorization: Bearer bearer-secret-value", "Authorization: Bearer "),
        (
            "Proxy-Authorization: Basic proxy-secret-value",
            "Proxy-Authorization: Basic ",
        ),
        ("Cookie: session=secret-value; theme=dark", "Cookie: "),
        ("Set-Cookie: session=secret-value; Path=/", "Set-Cookie: "),
    ],
)
def test_redaction_handles_sensitive_headers(header: str, safe_prefix: str) -> None:
    rendered = redact_text(header)

    assert rendered.startswith(safe_prefix)
    assert "secret-value" not in rendered
    assert "dXNlcjpwYXNz" not in rendered
    assert "bearer-secret-value" not in rendered
    assert "proxy-secret-value" not in rendered
    assert rendered.endswith("[REDACTED]")


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


@pytest.mark.parametrize(
    "value",
    [
        "Authorization: Basic dXNlcjpwYXNz\nnext\n",
        "Authorization: Bearer bearer-secret-value\nnext\n",
        "Proxy-Authorization: Basic proxy-secret-value\nnext\n",
        "Cookie: session=secret-value; theme=dark\nnext\n",
        "Set-Cookie: session=secret-value; Path=/\nnext\n",
    ],
)
def test_streaming_redactor_never_emits_chunk_split_sensitive_headers(
    value: str,
) -> None:
    secrets = (
        "dXNlcjpwYXNz",
        "bearer-secret-value",
        "proxy-secret-value",
        "secret-value",
    )
    for split in range(len(value) + 1):
        redactor = StreamingRedactor()
        emitted = (
            redactor.feed(value[:split])
            + redactor.feed(value[split:])
            + redactor.finish()
        )

        assert all(secret not in emitted for secret in secrets)
        assert "[REDACTED]" in emitted
        assert emitted.endswith("next\n")


def test_structured_redaction_preserves_usage_types_and_redacts_credentials() -> None:
    value = {
        "prompt_tokens": 12,
        "completion_tokens": 4,
        "cache_read_tokens": 3,
        "total_tokens": 19,
        "max_tokens": 100,
        "nested": {
            "access_token": "access-secret",
            "password": "password-secret",
            "description": "api_key=description-secret-value",
        },
    }

    rendered = redact_value(value)

    assert rendered["prompt_tokens"] == 12
    assert rendered["completion_tokens"] == 4
    assert rendered["cache_read_tokens"] == 3
    assert rendered["total_tokens"] == 19
    assert rendered["max_tokens"] == 100
    assert rendered["nested"] == {
        "access_token": "[REDACTED]",
        "password": "[REDACTED]",
        "description": "api_key=[REDACTED]",
    }


@pytest.mark.parametrize(
    "field",
    [
        "apiToken",
        "accessToken",
        "privateKey",
        "credential",
        "github_token_value",
        "some_password_hint",
        "proxyAuthorization",
        "sessionCookie",
    ],
)
def test_structured_redaction_rejects_compound_secret_field_names(field: str) -> None:
    assert redact_value({field: "synthetic-secret"}) == {field: "[REDACTED]"}


def test_structured_redaction_only_preserves_numeric_usage_token_fields() -> None:
    assert redact_value({"reasoning_tokens": 7, "token_count": 3}) == {
        "reasoning_tokens": 7,
        "token_count": 3,
    }
    assert redact_value({"reasoning_tokens": "synthetic-secret"}) == {
        "reasoning_tokens": "[REDACTED]"
    }
    assert redact_value({"credential_configured": True}) == {
        "credential_configured": True
    }


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


def test_streaming_redactor_withholds_long_token_before_secret_scan(monkeypatch) -> None:
    def fail_if_scanned(_value: str) -> int | None:
        raise AssertionError("long unbroken token should be withheld before secret scanning")

    monkeypatch.setattr(
        redaction_module,
        "_incomplete_secret_assignment_start",
        fail_if_scanned,
    )
    redactor = StreamingRedactor(max_token_characters=256)

    emitted = redactor.feed("x" * 257)

    assert emitted == LONG_TOKEN_WITHHELD_MARKER


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
