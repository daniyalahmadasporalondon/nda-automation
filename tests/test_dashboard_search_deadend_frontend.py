from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_dashboard_search_deadend_keyword_fallback() -> None:
    """FIX 1: an `unsupported` assistant intent (e.g. a bare counterparty name like
    "Moorwand") falls back to the deterministic keyword filter instead of dead-ending
    on the "Unsupported request" help card. Drives the real classic controller against
    a hand-rolled DOM (see tests/frontend/dashboard-search-deadend.cjs)."""
    if shutil.which("node") is None:
        pytest.skip("node is not installed; skipping frontend dashboard-search checks")
    subprocess.run(
        ["node", str(ROOT / "tests" / "frontend" / "dashboard-search-deadend.cjs")],
        check=True,
        cwd=ROOT,
    )
