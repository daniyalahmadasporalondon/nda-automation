from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_redline_render_resilience_one_bad_redline_does_not_blank_document() -> None:
    if shutil.which("node") is None:
        pytest.skip("node is not installed; skipping frontend redline-render resilience checks")
    subprocess.run(
        ["node", str(ROOT / "tests" / "frontend" / "redline-render-resilience.mjs")],
        check=True,
        cwd=ROOT,
    )
