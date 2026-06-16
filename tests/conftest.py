from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

_TEST_DATA_DIR = tempfile.mkdtemp(prefix="nda-automation-tests-")

# Force every test onto an isolated tmp data dir, overriding any NDA_DATA_DIR
# that a developer loaded from .env (e.g. `set -a; source .env`, which points it
# at the real ./data). Without this override a test that writes the user store
# would clobber the operator's real data/users.json — wiping their logged-in
# session and per-user OAuth tokens.
os.environ["NDA_DATA_DIR"] = _TEST_DATA_DIR
# The user store resolves its path from NDA_USERS_PATH *first*, falling back to
# NDA_DATA_DIR/users.json. matter_store.DATA_DIR is frozen at import time, so if
# nda_automation.matter_store happened to be imported before this conftest ran
# (plugin, -p, an unusual import chain), DATA_DIR would still point at the real
# ./data and the override above would be a no-op for the user store. Pinning
# NDA_USERS_PATH — which _users_path() reads at call time — guarantees the user
# store can never resolve to the real data/users.json regardless of import order.
os.environ["NDA_USERS_PATH"] = str(Path(_TEST_DATA_DIR) / "users.json")
os.environ["NDA_AI_REVIEW_ENABLED"] = "true"
os.environ["NDA_AI_ASSESSMENT_STUB"] = "1"
os.environ["NDA_ACTIVE_REVIEW_ENGINE"] = "ai_first"

# Belt-and-suspenders: if matter_store was already imported (so DATA_DIR froze to
# whatever NDA_DATA_DIR/./data was at that moment), re-point its module-level
# paths at the isolated tmp dir now. This keeps the matter store isolated even
# under an import-order inversion, mirroring the user-store protection above.
if "nda_automation.matter_store" in sys.modules:
    _matter_store = sys.modules["nda_automation.matter_store"]
    _root = Path(_TEST_DATA_DIR)
    _matter_store.DATA_DIR = _root
    _matter_store.MATTERS_PATH = _root / "matters.json"
    _matter_store.UPLOADS_DIR = _root / "uploads"


@pytest.fixture
def in_memory_matters():
    """A fresh, isolated in-memory matter repository per test.

    Matter-dependent code that accepts a MatterRepository can be exercised
    against this without touching the shared on-disk tempdir.
    """
    from nda_automation.matter_repository import InMemoryMatterRepository

    return InMemoryMatterRepository()
