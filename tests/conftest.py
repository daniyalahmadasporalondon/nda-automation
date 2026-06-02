from __future__ import annotations

import os
import tempfile


_TEST_DATA_DIR = tempfile.mkdtemp(prefix="nda-automation-tests-")

os.environ["NDA_DATA_DIR"] = _TEST_DATA_DIR
os.environ["NDA_AI_REVIEW_ENABLED"] = ""
