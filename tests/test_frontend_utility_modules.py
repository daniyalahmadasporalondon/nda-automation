from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_frontend_utility_modules_import_and_match_expected_behavior() -> None:
    if shutil.which("node") is None:
        pytest.skip("node is not installed; skipping frontend utility module checks")
    subprocess.run(
        ["node", str(ROOT / "tests" / "frontend" / "utility-modules.mjs")],
        check=True,
        cwd=ROOT,
    )
