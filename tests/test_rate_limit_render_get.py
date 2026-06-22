from __future__ import annotations

import unittest

from nda_automation import rate_limit
from nda_automation.rate_limit import (
    RENDER_GET_BUCKET,
    RENDER_GET_RATE_LIMIT_ENV,
    _rate_limit_bucket_name,
    _rate_limit_client_key,
    _rate_limit_retry_after,
    _render_get_rate_limit_per_window,
    _reset_rate_limits,
)


class RenderGetBucketNameTests(unittest.TestCase):
    def test_byte_render_get_routes_are_bucketed(self):
        cases = [
            "/api/matters/m_123/source",
            "/api/matters/m_123/source-pdf",
            "/api/matters/m_123/source-docx",
            "/api/matters/m_123/render-status",
            "/api/matters/m_123/render-pdf",
            "/api/matters/m_123/render-page/2",
            "/api/matters/m_123/reviewed-docx",
            "/api/matters/m_123/reviewed-pdf",
            "/api/matters/m_123/working-docx",
            "/api/matters/m_123/marked-up-pdf",
            "/api/matters/m_123/signed-document",
        ]
        for path in cases:
            with self.subTest(path=path):
                self.assertEqual(_rate_limit_bucket_name("GET", path), RENDER_GET_BUCKET)

    def test_marked_up_pdf_is_bucketed(self):
        # Regression: /marked-up-pdf re-opens the PDF in PyMuPDF and stamps every
        # annotation on EVERY request; it MUST be throttled like the other render
        # routes, not left unbucketed for an authenticated loop to abuse.
        self.assertEqual(
            _rate_limit_bucket_name("GET", "/api/matters/m_1/marked-up-pdf"),
            RENDER_GET_BUCKET,
        )

    def test_signed_document_is_bucketed(self):
        self.assertEqual(
            _rate_limit_bucket_name("GET", "/api/matters/m_1/signed-document"),
            RENDER_GET_BUCKET,
        )

    def test_unrelated_get_routes_stay_unbucketed(self):
        self.assertEqual(_rate_limit_bucket_name("GET", "/api/matters/m_1"), "")
        self.assertEqual(_rate_limit_bucket_name("GET", "/api/matters"), "")
        self.assertEqual(_rate_limit_bucket_name("GET", "/static/app.js"), "")
        # The annotations LIST route is not a render route -> stays unbucketed.
        self.assertEqual(_rate_limit_bucket_name("GET", "/api/matters/m_1/pdf-annotations"), "")

    def test_matter_backup_get_bucket_unchanged(self):
        self.assertEqual(_rate_limit_bucket_name("GET", "/api/matters/export"), "matter-backup")

    def test_post_routes_unaffected(self):
        self.assertEqual(_rate_limit_bucket_name("POST", "/api/review"), "review")
        self.assertEqual(_rate_limit_bucket_name("POST", "/api/matters/m/source-pdf"), "")


class RenderGetThrottleTests(unittest.TestCase):
    def setUp(self):
        _reset_rate_limits()
        self.addCleanup(_reset_rate_limits)

    def test_render_get_is_throttled_per_user(self):
        with unittest_env(RENDER_GET_RATE_LIMIT_ENV, "3"):
            key = _rate_limit_client_key("10.0.0.1", "", "user-a")
            path = "/api/matters/m_1/render-pdf"
            # First 3 allowed, 4th throttled.
            allowed = [
                _rate_limit_retry_after("GET", path, key) for _ in range(3)
            ]
            self.assertEqual(allowed, [0, 0, 0])
            self.assertGreater(_rate_limit_retry_after("GET", path, key), 0)

    def test_render_get_bucket_is_per_caller_isolated(self):
        with unittest_env(RENDER_GET_RATE_LIMIT_ENV, "1"):
            path = "/api/matters/m_1/source-pdf"
            key_a = _rate_limit_client_key("10.0.0.1", "", "user-a")
            key_b = _rate_limit_client_key("10.0.0.2", "", "user-b")
            # user-a exhausts their bucket.
            self.assertEqual(_rate_limit_retry_after("GET", path, key_a), 0)
            self.assertGreater(_rate_limit_retry_after("GET", path, key_a), 0)
            # user-b is unaffected: separate bucket.
            self.assertEqual(_rate_limit_retry_after("GET", path, key_b), 0)

    def test_render_get_limit_is_independent_of_global(self):
        # The render bucket uses its own env knob, not NDA_RATE_LIMIT_PER_MINUTE.
        with unittest_env(RENDER_GET_RATE_LIMIT_ENV, "2"):
            self.assertEqual(_render_get_rate_limit_per_window(), 2)
        with unittest_env(RENDER_GET_RATE_LIMIT_ENV, None):
            self.assertEqual(
                _render_get_rate_limit_per_window(),
                rate_limit.DEFAULT_RENDER_GET_RATE_LIMIT_PER_MINUTE,
            )

    def test_default_render_limit_is_generous_enough_for_review(self):
        # A multi-page interactive review fans out many byte/render GETs; the
        # default must comfortably exceed a single review's burst.
        self.assertGreaterEqual(rate_limit.DEFAULT_RENDER_GET_RATE_LIMIT_PER_MINUTE, 60)


class unittest_env:
    """Context manager to set/unset an env var for the duration of a block."""

    def __init__(self, name: str, value: str | None):
        self.name = name
        self.value = value
        self._previous: str | None = None
        self._had_previous = False

    def __enter__(self):
        import os

        self._had_previous = self.name in os.environ
        self._previous = os.environ.get(self.name)
        if self.value is None:
            os.environ.pop(self.name, None)
        else:
            os.environ[self.name] = self.value
        return self

    def __exit__(self, *exc):
        import os

        if self._had_previous:
            os.environ[self.name] = self._previous  # type: ignore[assignment]
        else:
            os.environ.pop(self.name, None)
        return False


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
