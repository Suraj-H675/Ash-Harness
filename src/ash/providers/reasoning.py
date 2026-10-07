"""Verified model-specific reasoning-effort support."""

from __future__ import annotations

from dataclasses import dataclass


ReasoningEffort = str


@dataclass(frozen=True)
class ReasoningEffortSpec:
    supported: tuple[ReasoningEffort, ...]
    default: ReasoningEffort | None = None

    def __post_init__(self) -> None:
        if not self.supported or len(set(self.supported)) != len(self.supported):
            raise ValueError("reasoning effort levels must be nonempty and unique")
        if self.default is not None and self.default not in self.supported:
            raise ValueError("default reasoning effort must be supported")


_OPENAI: dict[str, ReasoningEffortSpec] = {
    **{
        model: ReasoningEffortSpec(
            ("none", "low", "medium", "high", "xhigh", "max"),
            "medium",
        )
        for model in (
            "gpt-5.6",
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-5.6-luna",
            "gpt-6-sol",
            "gpt-6-luna",
        )
    },
    "gpt-6-astra": ReasoningEffortSpec(
        ("low", "medium", "high", "xhigh", "max"),
        "medium",
    ),
    "gpt-6.1-sol": ReasoningEffortSpec(
        ("low", "medium", "high", "xhigh", "max"),
        "medium",
    ),
}

_ANTHROPIC: dict[str, ReasoningEffortSpec] = {
    "claude-opus-5-5": ReasoningEffortSpec(
        ("low", "medium", "high", "xhigh", "max"),
        "medium",
    ),
    "claude-sonnet-5-5": ReasoningEffortSpec(
        ("low", "medium", "high", "xhigh", "max"),
        "high",
    ),
    "claude-fable-5-1": ReasoningEffortSpec(
        ("low", "medium", "high", "xhigh", "max"),
        "high",
    ),
    "claude-opus-4-7": ReasoningEffortSpec(
        ("low", "medium", "high", "xhigh", "max"),
        "high",
    ),
    "claude-sonnet-4-6": ReasoningEffortSpec(
        ("low", "medium", "high", "max"),
        "high",
    ),
}

_GOOGLE: dict[str, ReasoningEffortSpec] = {
    "gemini-3.8-flash": ReasoningEffortSpec(("low", "medium", "high"), "medium"),
    "gemini-3.7-flash": ReasoningEffortSpec(("low", "medium", "high"), "medium"),
    "gemini-3.6-flash": ReasoningEffortSpec(
        ("minimal", "low", "medium", "high"), "medium"
    ),
    "gemini-3.5-flash-lite": ReasoningEffortSpec(
        ("minimal", "low", "medium", "high"), "minimal"
    ),
    "gemini-3.1-pro-preview": ReasoningEffortSpec(
        ("low", "medium", "high"), "high"
    ),
    "gemini-3.1-flash-lite": ReasoningEffortSpec(
        ("minimal", "low", "medium", "high"), "minimal"
    ),
    "gemini-3.5-flash": ReasoningEffortSpec(
        ("minimal", "low", "medium", "high"), "medium"
    ),
    "gemini-2.5-pro": ReasoningEffortSpec(("low", "medium", "high")),
    "gemini-2.5-flash": ReasoningEffortSpec(
        ("none", "low", "medium", "high")
    ),
    "gemini-2.5-flash-lite": ReasoningEffortSpec(
        ("none", "low", "medium", "high"), "none"
    ),
}


def reasoning_effort_spec(
    provider_family: str,
    model_name: str,
) -> ReasoningEffortSpec | None:
    """Return only effort semantics we can map to a provider request confidently."""

    family = provider_family.strip().casefold()
    model = model_name.strip().casefold()
    if family == "openai":
        return _OPENAI.get(model)
    if family == "anthropic":
        return _ANTHROPIC.get(model)
    if family == "google":
        return _GOOGLE.get(model)
    if family == "vertex" and model.startswith("google/"):
        return ReasoningEffortSpec(("low", "medium", "high"))
    return None
