"""Validation primitives for unattended automation result delivery."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlparse

import httpx

from ash.automation.models import AutomationDeliveryLease
from ash.tools.web import (
    _PinnedPublicTransport,
    _resolve_public_addresses_with_timeout,
)


MAX_WEBHOOK_URL_BYTES = 2048
MAX_WEBHOOK_SECRET_ENV_BYTES = 256
MAX_WEBHOOK_BODY_BYTES = 256 * 1024
WEBHOOK_TIMEOUT_SECONDS = 10.0
WEBHOOK_DNS_TIMEOUT_SECONDS = 5.0
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")


class WebhookDeliveryError(RuntimeError):
    """Webhook configuration or transport could not complete safely."""


@dataclass(frozen=True)
class PreparedWebhook:
    url: str
    body: bytes
    headers: dict[str, str]
    resolved_addresses: tuple[str, ...] = ()


def normalize_webhook_settings(
    webhook_url: str | None,
    webhook_secret_env: str | None,
) -> tuple[str | None, str | None]:
    """Validate durable webhook configuration without performing network I/O."""

    raw_url = webhook_url if isinstance(webhook_url, str) else ""
    if _CONTROL_CHARACTER.search(raw_url):
        raise ValueError("webhook_url cannot contain control characters")
    normalized_url = raw_url.strip()
    normalized_secret = (
        webhook_secret_env.strip() if isinstance(webhook_secret_env, str) else ""
    )
    if not normalized_url:
        if normalized_secret:
            raise ValueError("webhook_secret_env requires webhook_url")
        return None, None
    if len(normalized_url.encode("utf-8")) > MAX_WEBHOOK_URL_BYTES:
        raise ValueError(f"webhook_url exceeds {MAX_WEBHOOK_URL_BYTES} bytes")
    try:
        httpx.URL(normalized_url)
    except (httpx.InvalidURL, ValueError) as exc:
        raise ValueError("webhook_url is not a valid HTTP URL") from exc
    parsed = urlparse(normalized_url)
    if parsed.scheme.casefold() != "https":
        raise ValueError("webhook_url must use https")
    if not parsed.hostname:
        raise ValueError("webhook_url must include a hostname")
    if parsed.username or parsed.password:
        raise ValueError("webhook_url cannot contain embedded credentials")
    if parsed.query:
        raise ValueError("webhook_url cannot contain a query string")
    if parsed.fragment:
        raise ValueError("webhook_url cannot contain a fragment")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("webhook_url contains an invalid port") from exc
    if normalized_secret:
        if len(normalized_secret.encode("utf-8")) > MAX_WEBHOOK_SECRET_ENV_BYTES:
            raise ValueError(
                f"webhook_secret_env exceeds {MAX_WEBHOOK_SECRET_ENV_BYTES} bytes"
            )
        if _ENV_NAME.fullmatch(normalized_secret) is None:
            raise ValueError(
                "webhook_secret_env must be a valid environment variable name"
            )
    return normalized_url, normalized_secret or None


def webhook_display_target(webhook_url: str) -> str:
    """Render a webhook origin without exposing credential-like path material."""

    parsed = urlparse(webhook_url)
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    port = parsed.port
    authority = host
    if port is not None and port != 443:
        authority = f"{authority}:{port}"
    suffix = "" if parsed.path in {"", "/"} else "/…"
    return f"https://{authority}{suffix}"


async def prepare_webhook(
    lease: AutomationDeliveryLease,
    *,
    environ: Mapping[str, str] | None = None,
) -> PreparedWebhook:
    """Build and validate a webhook request without dispatching the side effect."""

    url, secret_env = normalize_webhook_settings(
        lease.delivery.webhook_url,
        lease.delivery.webhook_secret_env,
    )
    assert url is not None
    hostname = urlparse(url).hostname
    assert hostname is not None
    try:
        resolved_addresses = await _resolve_public_addresses_with_timeout(
            hostname,
            timeout_seconds=WEBHOOK_DNS_TIMEOUT_SECONDS,
        )
    except ValueError as exc:
        raise WebhookDeliveryError(str(exc)) from exc
    payload = {
        "schema_version": 1,
        "event": "automation.run.finished",
        "delivery_id": lease.delivery.delivery_id,
        "job": {
            "job_id": lease.job.job_id,
            "name": lease.job.name,
        },
        "run": {
            "run_id": lease.run.run_id,
            "trigger": lease.run.trigger,
            "scheduled_for": lease.run.scheduled_for.isoformat(),
            "status": lease.run.status,
            "session_id": lease.run.session_id,
            "response": lease.run.response,
            "error": lease.run.error,
            "prompt_tokens": lease.run.prompt_tokens,
            "completion_tokens": lease.run.completion_tokens,
            "cache_read_tokens": lease.run.cache_read_tokens,
            "cache_write_tokens": lease.run.cache_write_tokens,
            "cost_usd": lease.run.cost_usd,
            "usage_source": lease.run.usage_source,
            "estimated_prompt_tokens": lease.run.estimated_prompt_tokens,
            "estimated_completion_tokens": lease.run.estimated_completion_tokens,
            "estimated_cost_usd": lease.run.estimated_cost_usd,
            "created_at": lease.run.created_at.isoformat(),
            "started_at": (
                lease.run.started_at.isoformat() if lease.run.started_at else None
            ),
            "finished_at": (
                lease.run.finished_at.isoformat() if lease.run.finished_at else None
            ),
        },
    }
    body = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(body) > MAX_WEBHOOK_BODY_BYTES:
        raise WebhookDeliveryError(
            f"automation webhook payload exceeds {MAX_WEBHOOK_BODY_BYTES} bytes"
        )
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Idempotency-Key": lease.delivery.delivery_id,
        "User-Agent": "ash-automation-webhook/0.1",
        "X-Ash-Delivery-Id": lease.delivery.delivery_id,
        "X-Ash-Event": "automation.run.finished",
    }
    if secret_env is not None:
        environment = os.environ if environ is None else environ
        secret = environment.get(secret_env, "")
        if not secret:
            raise WebhookDeliveryError(
                f"webhook signing environment variable {secret_env!r} is not configured"
            )
        digest = hmac.new(
            secret.encode("utf-8"),
            body,
            hashlib.sha256,
        ).hexdigest()
        headers["X-Ash-Signature"] = f"sha256={digest}"
    return PreparedWebhook(
        url=url,
        body=body,
        headers=headers,
        resolved_addresses=resolved_addresses,
    )


async def post_webhook(
    prepared: PreparedWebhook,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    """Dispatch one already-fenced webhook attempt and return its HTTP status."""

    effective_transport: httpx.AsyncBaseTransport
    if transport is None:
        if not prepared.resolved_addresses:
            raise WebhookDeliveryError(
                "automation webhook request is missing preflight DNS resolution"
            )
        effective_transport = _PinnedPublicTransport(
            pinned_addresses=prepared.resolved_addresses
        )
    else:
        effective_transport = transport
    try:
        async with httpx.AsyncClient(
            timeout=WEBHOOK_TIMEOUT_SECONDS,
            transport=effective_transport,
            follow_redirects=False,
        ) as client:
            async with client.stream(
                "POST",
                prepared.url,
                content=prepared.body,
                headers=prepared.headers,
            ) as response:
                return int(response.status_code)
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        raise WebhookDeliveryError(
            "automation webhook dispatch did not produce a provable HTTP response"
        ) from exc
