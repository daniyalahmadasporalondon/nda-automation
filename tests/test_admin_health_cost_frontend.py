from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_admin_health_ai_cost_panel() -> None:
    """Drive the AI-spend (USD) cost panel (admin-health.js) end to end.

    The Node harness mounts the real controller against a minimal fake DOM and
    drives its payload renderer, asserting the USD headline total, the per-feature
    rows (name + "$" amount + token secondary), server-supplied feature ordering,
    sub-cent precision on small totals, the honest cumulative-since-restart caveat,
    and the empty / missing-cost-block paths.
    """
    if shutil.which("node") is None:
        pytest.skip("node is not installed; skipping admin-health cost frontend checks")
    subprocess.run(
        ["node", str(ROOT / "tests" / "frontend" / "admin-health-cost.cjs")],
        check=True,
        cwd=ROOT,
    )
