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


def test_admin_drive_folder_picker_controls() -> None:
    """Drive the "Browse Drive" folder picker (admin-drive.js) end to end.

    The Node harness mounts the real controller against a minimal fake DOM and a
    stubbed fetch, asserting the modal open + root listing, breadcrumb drill-in,
    select-fills-both-fields, the Drive-disconnected (409) error path, and that
    the manual paste-an-ID + Save flow is untouched.
    """
    if shutil.which("node") is None:
        pytest.skip("node is not installed; skipping admin-drive frontend checks")
    subprocess.run(
        ["node", str(ROOT / "tests" / "frontend" / "admin-drive-picker.cjs")],
        check=True,
        cwd=ROOT,
    )


def test_admin_access_panel_controls() -> None:
    """Drive the Admin Access panel (admin-access.js) end to end.

    The Node harness mounts the real controller against a minimal fake DOM and a
    stubbed fetch, asserting the load probe (env roots immutable + persisted with
    Remove), the 403 admin-only state, add POST + re-render, delegated Remove
    DELETE + re-render, the 409 lockout/immutable error surfaced inline, and that
    interpolated emails are HTML-escaped.
    """
    if shutil.which("node") is None:
        pytest.skip("node is not installed; skipping admin-access frontend checks")
    subprocess.run(
        ["node", str(ROOT / "tests" / "frontend" / "admin-access.cjs")],
        check=True,
        cwd=ROOT,
    )


def test_personalisation_self_serve_form() -> None:
    """Drive the self-serve "My signature" form (admin-personalisation.js).

    The Node harness mounts the REAL controller against a minimal fake DOM and a
    stubbed fetch, asserting that a NON-admin's form loads and saves through
    GET/POST /api/me/personalisation-settings (never the admin endpoint, no 403
    dead-end), and that the admin-only global-default panel self-hides on 403
    rather than nagging "Administrator access is required".
    """
    if shutil.which("node") is None:
        pytest.skip("node is not installed; skipping personalisation frontend checks")
    subprocess.run(
        ["node", str(ROOT / "tests" / "frontend" / "personalisation-self-serve.cjs")],
        check=True,
        cwd=ROOT,
    )
