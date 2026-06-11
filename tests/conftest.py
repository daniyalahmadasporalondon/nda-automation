from __future__ import annotations

import os
import tempfile

import pytest

_TEST_DATA_DIR = tempfile.mkdtemp(prefix="nda-automation-tests-")

os.environ["NDA_DATA_DIR"] = _TEST_DATA_DIR
os.environ["NDA_AI_REVIEW_ENABLED"] = "true"
os.environ["NDA_AI_ASSESSMENT_STUB"] = "1"
os.environ["NDA_ACTIVE_REVIEW_ENGINE"] = "ai_first"


@pytest.fixture
def in_memory_matters():
    """A fresh, isolated in-memory matter repository per test.

    Matter-dependent code that accepts a MatterRepository can be exercised
    against this without touching the shared on-disk tempdir.
    """
    from nda_automation.matter_repository import InMemoryMatterRepository

    return InMemoryMatterRepository()
