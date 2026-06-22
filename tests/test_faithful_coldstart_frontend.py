from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _run_frontend(script: str) -> None:
    if shutil.which("node") is None:
        pytest.skip("node is not installed; skipping frontend faithful-render checks")
    subprocess.run(
        ["node", str(ROOT / "tests" / "frontend" / script)],
        check=True,
        cwd=ROOT,
    )


def test_faithful_coldstart_engages_and_falls_back() -> None:
    # The cold page (no pre-injected window.docx): the faithful surface must engage
    # once the lazy-load resolves, and a load failure must degrade to the
    # reconstruction (never blank). Skips cleanly when jsdom is absent.
    _run_frontend("faithful-coldstart.mjs")


def test_faithful_redline_clean_upgrade_bugs() -> None:
    # #2 Clean renders accepted text (renderChanges:false), Redline keeps tracked
    # changes; #3 a dirty in-session edit is not overwritten by the persisted
    # faithful re-fetch. Skips cleanly when jsdom is absent.
    _run_frontend("faithful-redline-clean-upgrade.mjs")
