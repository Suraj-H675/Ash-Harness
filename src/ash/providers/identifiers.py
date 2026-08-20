"""Canonical provider and model identifier validation."""

from __future__ import annotations

import re


PROVIDER_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def parse_model_string(model: str) -> tuple[str, str]:
    """Split and validate a canonical ``provider/model`` identifier."""

    provider, separator, model_name = model.strip().partition("/")
    provider = provider.casefold()
    if not separator or not PROVIDER_NAME.fullmatch(provider) or not model_name.strip():
        raise ValueError(
            f"Model string must be in 'provider/model' format, got: {model!r}"
        )
    return provider, model_name.strip()
