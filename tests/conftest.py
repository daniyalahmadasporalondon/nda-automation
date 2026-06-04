from __future__ import annotations

import os
import tempfile

import pytest

_TEST_DATA_DIR = tempfile.mkdtemp(prefix="nda-automation-tests-")

os.environ["NDA_DATA_DIR"] = _TEST_DATA_DIR
os.environ["NDA_AI_REVIEW_ENABLED"] = ""
# The production default active engine is ai_first + fail_closed (see
# nda_automation/review_engine.py). With AI disabled in tests, that default would
# make every review path raise ActiveReviewEngineError. Pin the deterministic
# engine as the suite-wide baseline so review-path tests exercise the rules
# engine. Tests that specifically verify engine selection/defaults override this
# with their own patch.dict(os.environ, ...), so this does not mask them.
os.environ["NDA_ACTIVE_REVIEW_ENGINE"] = "deterministic"


@pytest.fixture
def in_memory_matters():
    """A fresh, isolated in-memory matter repository per test.

    Matter-dependent code that accepts a MatterRepository can be exercised
    against this without touching the shared on-disk tempdir.
    """
    from nda_automation.matter_repository import InMemoryMatterRepository

    return InMemoryMatterRepository()
