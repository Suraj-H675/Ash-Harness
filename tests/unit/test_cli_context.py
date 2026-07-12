from ash.cli import _render_context_budget
from ash.context.history import (
    ContextBudgetReport,
    ContextBudgetSlice,
    ContextFragment,
    ContextFragmentKind,
    ContextTrust,
)


def test_render_context_budget_shows_bucket_usage() -> None:
    rendered = _render_context_budget(
        ContextBudgetReport(
            maximum=100,
            completion_reserve=10,
            input_limit=90,
            slices={
                "system": ContextBudgetSlice("system", limit=20, used=12),
                "memory": ContextBudgetSlice(
                    "memory", limit=10, used=10, truncated=True
                ),
            },
            fragments=(
                ContextFragment(
                    kind=ContextFragmentKind.SYSTEM,
                    source="assembled_system_prompt",
                    trust=ContextTrust.MIXED,
                    tokens=12,
                    limit=20,
                    truncated=False,
                    content_sha256="a" * 64,
                ),
            ),
        )
    )

    assert "system: ~12/20" in rendered
    assert "memory: ~10/10 truncated" in rendered
    assert "system: assembled_system_prompt [mixed] sha256=aaaaaaaaaaaa" in rendered


def test_render_context_budget_without_report_is_empty() -> None:
    assert _render_context_budget(None) == ""
