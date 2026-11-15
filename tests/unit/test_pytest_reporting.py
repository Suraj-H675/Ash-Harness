from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_oversized_input_cases_have_bounded_ci_test_names() -> None:
    """Verbose CI output must not echo megabytes of test parameters."""
    root = Path(__file__).resolve().parents[2]
    targets = (
        "tests/unit/test_openai_chatgpt_provider.py::"
        "test_native_compaction_rejects_unsafe_raw_http_responses",
        "tests/unit/test_repository_benchmark.py::"
        "test_headless_error_category_refuses_untrusted_or_large_output",
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *targets],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-500:]
    cases = [line for line in result.stdout.splitlines() if line.startswith("tests/unit/")]
    assert len(cases) == 12
    assert max(map(len, cases)) <= 300
