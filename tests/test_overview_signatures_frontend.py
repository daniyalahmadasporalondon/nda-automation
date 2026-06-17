from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_overview_signatures_block() -> None:
    """Drive the Overview SIGNATURES renderer (overview/signatures.js) end to end.

    The Node harness mounts the real renderer against a minimal innerHTML-capturing
    container and asserts the four product cases: no envelope -> 0/2 "Not sent",
    one party signed -> 1/2 (and WHICH party), both signed -> 2/2 "Fully executed",
    and the Aspora vs counterparty role mapping.
    """
    if shutil.which("node") is None:
        pytest.skip("node is not installed; skipping overview-signatures frontend checks")
    subprocess.run(
        ["node", str(ROOT / "tests" / "frontend" / "signatures.cjs")],
        check=True,
        cwd=ROOT,
    )
