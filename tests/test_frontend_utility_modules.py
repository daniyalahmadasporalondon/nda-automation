from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_frontend_utility_modules_import_and_match_expected_behavior() -> None:
    subprocess.run(
        ["node", str(ROOT / "tests" / "frontend" / "utility-modules.mjs")],
        check=True,
        cwd=ROOT,
    )
