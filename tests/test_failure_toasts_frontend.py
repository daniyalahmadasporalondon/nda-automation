from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_failure_toasts_dedup_and_surface() -> None:
    """Drive the failure-notification toasts (notifications.js) end to end.

    The Node harness mounts the real controller and asserts: a new active failure
    event toasts exactly once, the same event on a later poll does not re-toast,
    resolved/dismissed events never toast, and the first observation seeds the
    seen-set silently (no flood on load).
    """
    if shutil.which("node") is None:
        pytest.skip("node is not installed; skipping failure-toasts frontend checks")
    subprocess.run(
        ["node", str(ROOT / "tests" / "frontend" / "failure-toasts.cjs")],
        check=True,
        cwd=ROOT,
    )
