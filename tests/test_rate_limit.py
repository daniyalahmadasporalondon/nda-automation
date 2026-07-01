from __future__ import annotations

import unittest

from nda_automation import rate_limit


class AiEndpointRateLimitResolutionTests(unittest.TestCase):
    """The pure-AI POST buckets get their OWN tight cap, not the 300 default.

    ``review`` (POST /api/review) is deliberately excluded from the tight cap: it
    is dual-use (the viewer re-POSTs it with ``offline:true`` on a debounce after
    every edit, no AI spend), so it stays on the general 300/min default -- same
    as base main -- and an active editor is never 429'd.
    """

    def setUp(self) -> None:
        for env in (
            rate_limit.AI_ENDPOINT_RATE_LIMIT_ENV,
            rate_limit.RENDER_GET_RATE_LIMIT_ENV,
            "NDA_RATE_LIMIT_PER_MINUTE",
            "NDA_RATE_LIMIT_WINDOW_SECONDS",
        ):
            self._clear_env(env)
        rate_limit._reset_rate_limits()
        self.addCleanup(rate_limit._reset_rate_limits)

    def _clear_env(self, name: str) -> None:
        import os

        original = os.environ.get(name)
        if original is not None:
            del os.environ[name]

        def _restore() -> None:
            if original is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = original

        self.addCleanup(_restore)

    def _set_env(self, name: str, value: str) -> None:
        import os

        original = os.environ.get(name)
        os.environ[name] = value

        def _restore() -> None:
            if original is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = original

        self.addCleanup(_restore)

    # (1) Each of the four pure-AI buckets resolves to the AI cap (10), not 300.
    # ``review`` is intentionally NOT in the set (see (1c)).
    def test_all_ai_buckets_resolve_to_ai_cap_not_general_default(self):
        self.assertEqual(
            rate_limit.AI_ENDPOINT_BUCKETS,
            frozenset(
                {
                    "ai-second-opinion",
                    "ai-draft-validation",
                    "generate-nda",
                    "dashboard-assistant",
                }
            ),
        )
        # The dual-use review bucket must stay OUT of the tight-cap set.
        self.assertNotIn("review", rate_limit.AI_ENDPOINT_BUCKETS)
        for bucket in rate_limit.AI_ENDPOINT_BUCKETS:
            with self.subTest(bucket=bucket):
                resolved = rate_limit._rate_limit_per_window_for_bucket(bucket)
                self.assertEqual(
                    resolved,
                    rate_limit.DEFAULT_AI_ENDPOINT_RATE_LIMIT_PER_MINUTE,
                )
                self.assertEqual(resolved, 10)
                self.assertNotEqual(resolved, rate_limit.DEFAULT_RATE_LIMIT_PER_MINUTE)

    # (1b) Confirm the POST path map actually emits these bucket names, so the
    # resolver branch is reachable end-to-end (name map is unchanged).
    def test_ai_endpoint_post_paths_map_to_ai_bucket_names(self):
        cases = {
            "/api/review/ai-second-opinion": "ai-second-opinion",
            "/api/review/ai-draft-validation": "ai-draft-validation",
            "/api/generate-nda": "generate-nda",
            "/api/dashboard/assistant": "dashboard-assistant",
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                bucket = rate_limit._rate_limit_bucket_name("POST", path)
                self.assertEqual(bucket, expected)
                self.assertIn(bucket, rate_limit.AI_ENDPOINT_BUCKETS)

    # (1c) POST /api/review still maps to the "review" bucket (name map is
    # UNCHANGED from base) but that bucket resolves to the general 300 default,
    # NOT the tight AI cap -- so an active editor hitting the offline-review
    # debounce is never throttled at 10/min.
    def test_review_bucket_falls_back_to_general_default_not_ai_cap(self):
        bucket = rate_limit._rate_limit_bucket_name("POST", "/api/review")
        self.assertEqual(bucket, "review")
        self.assertNotIn(bucket, rate_limit.AI_ENDPOINT_BUCKETS)
        resolved = rate_limit._rate_limit_per_window_for_bucket(bucket)
        self.assertEqual(resolved, rate_limit.DEFAULT_RATE_LIMIT_PER_MINUTE)
        self.assertEqual(resolved, 300)
        self.assertNotEqual(
            resolved, rate_limit.DEFAULT_AI_ENDPOINT_RATE_LIMIT_PER_MINUTE
        )

    # (2) RENDER_GET keeps its render cap; other buckets keep the 300 default.
    def test_render_and_other_buckets_keep_their_caps(self):
        self.assertEqual(
            rate_limit._rate_limit_per_window_for_bucket(rate_limit.RENDER_GET_BUCKET),
            rate_limit.DEFAULT_RENDER_GET_RATE_LIMIT_PER_MINUTE,
        )
        # ``review`` is included here on purpose: it is a POST bucket that now
        # falls back to the general 300 default instead of the tight AI cap.
        for bucket in (
            "review",
            "matter-upload",
            "docx-export",
            "gmail-send-redline",
            "matter-backup",
        ):
            with self.subTest(bucket=bucket):
                self.assertEqual(
                    rate_limit._rate_limit_per_window_for_bucket(bucket),
                    rate_limit.DEFAULT_RATE_LIMIT_PER_MINUTE,
                )
                self.assertEqual(
                    rate_limit._rate_limit_per_window_for_bucket(bucket), 300
                )

    # (3) Env override + fail-open on a non-int value.
    def test_env_override_sets_ai_cap(self):
        self._set_env(rate_limit.AI_ENDPOINT_RATE_LIMIT_ENV, "3")
        self.assertEqual(rate_limit._ai_endpoint_rate_limit_per_window(), 3)
        for bucket in rate_limit.AI_ENDPOINT_BUCKETS:
            with self.subTest(bucket=bucket):
                self.assertEqual(
                    rate_limit._rate_limit_per_window_for_bucket(bucket), 3
                )

    def test_env_override_non_int_fails_open_to_default(self):
        self._set_env(rate_limit.AI_ENDPOINT_RATE_LIMIT_ENV, "not-a-number")
        self.assertEqual(
            rate_limit._ai_endpoint_rate_limit_per_window(),
            rate_limit.DEFAULT_AI_ENDPOINT_RATE_LIMIT_PER_MINUTE,
        )
        self.assertEqual(rate_limit._ai_endpoint_rate_limit_per_window(), 10)

    # (4) End-to-end counter: a pure-AI bucket throttles at its tight cap within
    # a window, while a general (matter-upload=300) bucket does not throttle at
    # the same request count.
    def test_ai_bucket_throttles_at_tight_cap_while_general_bucket_does_not(self):
        host = "203.0.113.7"
        limit = rate_limit.DEFAULT_AI_ENDPOINT_RATE_LIMIT_PER_MINUTE  # 10

        # First `limit` POSTs to a pure-AI endpoint are allowed (retry_after == 0).
        ai_path = "/api/dashboard/assistant"
        for i in range(limit):
            self.assertEqual(
                rate_limit._rate_limit_retry_after("POST", ai_path, host),
                0,
                msg=f"request {i + 1} within AI cap should not be throttled",
            )
        # The (limit + 1)th is throttled: positive retry_after.
        self.assertGreater(
            rate_limit._rate_limit_retry_after("POST", ai_path, host),
            0,
        )

        # The SAME number of requests to /api/matters (matter-upload, cap 300) is
        # not throttled -- proves the tight cap is scoped to the AI bucket only.
        for i in range(limit + 1):
            self.assertEqual(
                rate_limit._rate_limit_retry_after("POST", "/api/matters", host),
                0,
                msg=f"matter-upload request {i + 1} should not be throttled at AI cap",
            )

    # (4b) The dual-use review endpoint is NOT throttled at the tight AI cap. An
    # active editor bursts the offline-review debounce well past 10/min; every one
    # of those (limit + several) POSTs to /api/review must still be allowed,
    # proving live editing (and a genuine Review click in the same window) is not
    # 429'd. It is bounded only by the general 300/min default, same as base main.
    def test_review_endpoint_not_throttled_at_tight_ai_cap(self):
        host = "198.51.100.42"
        limit = rate_limit.DEFAULT_AI_ENDPOINT_RATE_LIMIT_PER_MINUTE  # 10

        # Simulate an active editor: many more than the tight cap in one window.
        for i in range(limit * 3):
            self.assertEqual(
                rate_limit._rate_limit_retry_after("POST", "/api/review", host),
                0,
                msg=(
                    f"/api/review request {i + 1} must not be throttled at the "
                    "tight AI cap (active-editing debounce)"
                ),
            )


if __name__ == "__main__":
    unittest.main()
