"""Protect the external coding benchmark's scoring and oracle isolation."""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest


_benchmark_path = Path(__file__).resolve().parents[2] / "devtools/coding_benchmark.py"
_spec = importlib.util.spec_from_file_location(
    "ash_coding_benchmark_under_test", _benchmark_path
)
assert _spec is not None and _spec.loader is not None
_benchmark = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _benchmark
_spec.loader.exec_module(_benchmark)

CALC_TEST = _benchmark.CALC_TEST
CHECKOUT = _benchmark.CHECKOUT
CHECKOUT_TEST = _benchmark.CHECKOUT_TEST
PRICING = _benchmark.PRICING
grade = _benchmark.grade
oracle_python = _benchmark.oracle_python


def test_marker_scoring_rejects_a_symlink_to_outside_the_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("ASH_FREE_MODEL_OK")
    (workspace / "marker.txt").symlink_to(outside)

    assert grade("marker", workspace) == {
        "correct": False,
        "oracle_error": "output is a symlink",
    }


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or shutil.which("bwrap") is None,
    reason="Linux Bubblewrap runtime required",
)
def test_coding_oracle_cannot_read_host_files_outside_fixture(tmp_path: Path) -> None:
    outside = tmp_path / "host-only.txt"
    outside.write_text("HOST_ONLY_SECRET")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "probe.py").write_text(
        "from pathlib import Path\n"
        f"outside = Path({str(outside)!r})\n"
        'print("ISOLATED" if not outside.exists() else outside.read_text())\n'
    )

    result = oracle_python(workspace, "probe.py")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ISOLATED"
    assert outside.read_text() == "HOST_ONLY_SECRET"


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or shutil.which("bwrap") is None,
    reason="Linux Bubblewrap runtime required",
)
def test_repair_oracle_rejects_constant_answer_that_passes_visible_test(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "calc.py").write_text("def add(a, b):\n    return 5\n")
    (workspace / "test_calc.py").write_bytes(CALC_TEST)

    outcome = grade("repair-test", workspace)

    assert outcome["correct"] is False


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or shutil.which("bwrap") is None,
    reason="Linux Bubblewrap runtime required",
)
def test_multifile_oracle_rejects_visible_test_only_hack(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "inventory").mkdir(parents=True)
    (workspace / "inventory/__init__.py").write_bytes(b"")
    (workspace / "inventory/pricing.py").write_bytes(PRICING)
    (workspace / "inventory/checkout.py").write_bytes(CHECKOUT)
    (workspace / "test_checkout.py").write_bytes(CHECKOUT_TEST)
    (workspace / "inventory/pricing.py").write_text(
        "from decimal import Decimal\n"
        "def discounted_total(prices: list[Decimal], discount_percent: Decimal) -> Decimal:\n"
        "    return Decimal('9.00')\n"
    )

    assert grade("multi-file", workspace)["correct"] is False

    (workspace / "inventory/pricing.py").write_text(
        "from decimal import Decimal\n"
        "def discounted_total(prices: list[Decimal], discount_percent: Decimal) -> Decimal:\n"
        "    subtotal = sum(prices, start=Decimal('0'))\n"
        "    return subtotal * (Decimal('100') - discount_percent) / Decimal('100')\n"
    )

    assert grade("multi-file", workspace)["correct"] is True
