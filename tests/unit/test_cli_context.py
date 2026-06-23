from ash.cli import _render_context_budget
from context.history import ContextBudgetReport, ContextBudgetSlice


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
        )
    )

    assert "system: ~12/20" in rendered
    assert "memory: ~10/10 truncated" in rendered


def test_render_context_budget_without_report_is_empty() -> None:
    assert _render_context_budget(None) == ""
