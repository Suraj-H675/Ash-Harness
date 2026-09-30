"""Canonical provider and model identifier validation."""

from __future__ import annotations

import re


PROVIDER_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
MAX_MODEL_IDENTIFIER_BYTES = 512


def parse_model_string(model: str) -> tuple[str, str]:
    """Split and validate a canonical ``provider/model`` identifier."""

    if not isinstance(model, str):
        raise ValueError("Model string must be text in 'provider/model' format")
    normalized = model.strip()
    if len(normalized.encode("utf-8")) > MAX_MODEL_IDENTIFIER_BYTES:
        raise ValueError(
            "Model string in 'provider/model' format exceeds "
            f"{MAX_MODEL_IDENTIFIER_BYTES} bytes"
        )
    provider, separator, model_name = normalized.partition("/")
    provider = provider.casefold()
    if not separator or not PROVIDER_NAME.fullmatch(provider) or not model_name.strip():
        raise ValueError(
            f"Model string must be in 'provider/model' format, got: {model!r}"
        )
    return provider, model_name.strip()
