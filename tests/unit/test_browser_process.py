from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ash.safety.browser_process import (
    browser_subprocess_environment,
    run_browser_subprocess,
)


def test_browser_subprocess_environment_scrubs_unrelated_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-secret")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.test:8080")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/tmp/playwright-browsers")

    environment = browser_subprocess_environment()

    assert "OPENAI_API_KEY" not in environment
    assert environment["HTTPS_PROXY"] == "http://proxy.example.test:8080"
    assert environment["PLAYWRIGHT_BROWSERS_PATH"] == "/tmp/playwright-browsers"


@pytest.mark.skipif(os.name == "nt", reason="POSIX descendant lifecycle regression")
def test_browser_subprocess_timeout_terminates_descendants(tmp_path: Path) -> None:
    marker = tmp_path / "child-survived"
    child = (
        "import time; "
        "time.sleep(0.4); "
        f"open({str(marker)!r}, 'w').write('survived'); "
        "time.sleep(5)"
    )
    parent = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
        "time.sleep(30)"
    )

    with pytest.raises(subprocess.TimeoutExpired):
        run_browser_subprocess(
            [sys.executable, "-I", "-c", parent],
            timeout=0.1,
        )
    time.sleep(0.55)

    assert not marker.exists()
