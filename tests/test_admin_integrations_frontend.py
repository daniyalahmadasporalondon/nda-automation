from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_admin_integrations_gmail_polling_controls() -> None:
    """Drive the Gmail polling controls (admin-integrations.js) end to end.

    The Node harness mounts the real controller against a minimal fake DOM and a
    stubbed fetch, asserting the pause/resume-via-sync_enabled toggle (no
    disconnect), the "Polling on/off" copy, and the import-limit save + clamp.
    """
    if shutil.which("node") is None:
        pytest.skip("node is not installed; skipping admin-integrations frontend checks")
    subprocess.run(
        ["node", str(ROOT / "tests" / "frontend" / "admin-integrations.cjs")],
        check=True,
        cwd=ROOT,
    )
