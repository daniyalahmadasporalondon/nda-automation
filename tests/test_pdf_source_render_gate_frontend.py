from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_pdf_source_render_gate_attempts_render_without_repository_markers() -> None:
    if shutil.which("node") is None:
        pytest.skip("node is not installed; skipping frontend PDF render-gate checks")
    subprocess.run(
        ["node", str(ROOT / "tests" / "frontend" / "pdf-source-render-gate.mjs")],
        check=True,
        cwd=ROOT,
    )
