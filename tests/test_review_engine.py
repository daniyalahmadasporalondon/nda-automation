import os
import unittest
from unittest.mock import Mock, patch

from nda_automation import telemetry
from nda_automation.ai_assessor import AIAssessorError
from nda_automation.review_engine import (
    ACTIVE_REVIEW_ENGINE_ENV,
    AI_FIRST_FALLBACK_MODE_ENV,
    FALLBACK_MODE_DETERMINISTIC,
    FALLBACK_MODE_FAIL_CLOSED,
    REVIEW_ENGINE_AI_FIRST,
    REVIEW_ENGINE_DETERMINISTIC,
    ActiveReviewEngineError,
    active_review_engine_status,
    active_review_engine,
    review_nda_with_active_engine,
)


class ReviewEngineTests(unittest.TestCase):
    def setUp(self):
        telemetry.reset()
        self.runtime_settings = {
            "active_review_engine": None,
            "ai_first_fallback_mode": None,
        }
        self.runtime_settings_patch = patch(
            "nda_automation.review_engine.app_settings.review_runtime_settings",
            lambda: self.runtime_settings,
        )
        self.runtime_settings_patch.start()
        self.addCleanup(self.runtime_settings_patch.stop)

    def test_active_review_engine_defaults_to_ai_first_fail_closed(self):
        deterministic = Mock(return_value={"review_mode": "deterministic"})
        ai_first = Mock(return_value={"review_mode": "ai_first_compat"})

        with patch.dict(os.environ, {ACTIVE_REVIEW_ENGINE_ENV: "", AI_FIRST_FALLBACK_MODE_ENV: ""}):
            result = review_nda_with_active_engine(
                "NDA text",
                deterministic_review_func=deterministic,
                ai_first_review_func=ai_first,
            )
            selected_engine = active_review_engine()
            status = active_review_engine_status()

        self.assertEqual(selected_engine, REVIEW_ENGINE_AI_FIRST)
        self.assertEqual(result["review_mode"], "ai_first_compat")
        self.assertEqual(result["active_review_engine"]["selected_engine"], REVIEW_ENGINE_AI_FIRST)
        self.assertEqual(result["active_review_engine"]["executed_engine"], REVIEW_ENGINE_AI_FIRST)
        self.assertEqual(result["active_review_engine"]["fallback_mode"], FALLBACK_MODE_FAIL_CLOSED)
        self.assertFalse(result["active_review_engine"]["fallback_used"])
        self.assertEqual(status["engine_source"], "default")
        self.assertEqual(status["fallback_source"], "default")
        self.assertEqual(telemetry.snapshot()["counters"]["active_review_ai_first_completed"], 1)
        deterministic.assert_not_called()
        ai_first.assert_called_once_with("NDA text", paragraphs=None)

    def test_active_review_engine_accepts_ai_first_aliases(self):
        for value in ["ai", "ai-first", "ai_first", "ai_first_review"]:
            with self.subTest(value=value):
                with patch.dict(os.environ, {ACTIVE_REVIEW_ENGINE_ENV: value}):
                    self.assertEqual(active_review_engine(), REVIEW_ENGINE_AI_FIRST)

    def test_ai_first_engine_becomes_active_result_when_selected(self):
        deterministic = Mock(return_value={"review_mode": "deterministic"})
        ai_first = Mock(return_value={"review_mode": "ai_first_compat"})
        paragraphs = [{"id": "p1", "text": "Clause text"}]

        with patch.dict(os.environ, {ACTIVE_REVIEW_ENGINE_ENV: "ai_first"}):
            result = review_nda_with_active_engine(
                "Clause text",
                paragraphs=paragraphs,
                deterministic_review_func=deterministic,
                ai_first_review_func=ai_first,
            )

        deterministic.assert_not_called()
        ai_first.assert_called_once_with("Clause text", paragraphs=paragraphs)
        self.assertEqual(result["review_mode"], "ai_first_compat")
        self.assertEqual(result["active_review_engine"]["engine"], REVIEW_ENGINE_AI_FIRST)
        self.assertEqual(result["active_review_engine"]["selected_engine"], REVIEW_ENGINE_AI_FIRST)
        self.assertEqual(result["active_review_engine"]["executed_engine"], REVIEW_ENGINE_AI_FIRST)
        self.assertFalse(result["active_review_engine"]["fallback_used"])
        counters = telemetry.snapshot()["counters"]
        self.assertEqual(counters["active_review_ai_first_attempted"], 1)
        self.assertEqual(counters["active_review_ai_first_completed"], 1)

    def test_runtime_settings_select_ai_first_when_environment_is_unset(self):
        deterministic = Mock(return_value={"review_mode": "deterministic"})
        ai_first = Mock(return_value={"review_mode": "ai_first_compat"})
        self.runtime_settings["active_review_engine"] = REVIEW_ENGINE_AI_FIRST
        self.runtime_settings["ai_first_fallback_mode"] = FALLBACK_MODE_FAIL_CLOSED

        with patch.dict(os.environ, {ACTIVE_REVIEW_ENGINE_ENV: "", AI_FIRST_FALLBACK_MODE_ENV: ""}):
            result = review_nda_with_active_engine(
                "Clause text",
                deterministic_review_func=deterministic,
                ai_first_review_func=ai_first,
            )
            status = active_review_engine_status()

        deterministic.assert_not_called()
        ai_first.assert_called_once()
        self.assertEqual(result["review_mode"], "ai_first_compat")
        self.assertEqual(result["active_review_engine"]["source"], "runtime_settings")
        self.assertEqual(result["active_review_engine"]["fallback_source"], "runtime_settings")
        self.assertEqual(result["active_review_engine"]["fallback_mode"], FALLBACK_MODE_FAIL_CLOSED)
        self.assertEqual(status["engine_source"], "runtime_settings")
        self.assertEqual(status["fallback_source"], "runtime_settings")

    def test_environment_engine_overrides_runtime_settings(self):
        self.runtime_settings["active_review_engine"] = REVIEW_ENGINE_DETERMINISTIC
        self.runtime_settings["ai_first_fallback_mode"] = FALLBACK_MODE_DETERMINISTIC

        with patch.dict(os.environ, {ACTIVE_REVIEW_ENGINE_ENV: "ai_first", AI_FIRST_FALLBACK_MODE_ENV: "fail_closed"}):
            status = active_review_engine_status()

        self.assertEqual(status["active_engine"], REVIEW_ENGINE_AI_FIRST)
        self.assertEqual(status["engine_source"], "environment")
        self.assertEqual(status["ai_first_fallback_mode"], FALLBACK_MODE_FAIL_CLOSED)
        self.assertEqual(status["fallback_source"], "environment")

    def test_ai_first_failure_fails_closed_by_default(self):
        deterministic = Mock(return_value={"review_mode": "deterministic"})
        ai_first = Mock(side_effect=AIAssessorError("no key"))

        with patch.dict(os.environ, {ACTIVE_REVIEW_ENGINE_ENV: "", AI_FIRST_FALLBACK_MODE_ENV: ""}):
            with self.assertRaisesRegex(ActiveReviewEngineError, "AI-first review failed"):
                review_nda_with_active_engine(
                    "NDA text",
                    deterministic_review_func=deterministic,
                    ai_first_review_func=ai_first,
                )

        ai_first.assert_called_once()
        deterministic.assert_not_called()
        counters = telemetry.snapshot()["counters"]
        self.assertEqual(counters["active_review_ai_first_attempted"], 1)
        self.assertEqual(counters["active_review_ai_first_failed"], 1)
        self.assertEqual(counters["active_review_ai_first_fail_closed"], 1)

    def test_ai_first_failure_falls_back_to_deterministic_when_configured(self):
        deterministic = Mock(return_value={"review_mode": "deterministic"})
        ai_first = Mock(side_effect=AIAssessorError("no key"))

        with patch.dict(os.environ, {ACTIVE_REVIEW_ENGINE_ENV: "ai_first", AI_FIRST_FALLBACK_MODE_ENV: "deterministic"}):
            result = review_nda_with_active_engine(
                "NDA text",
                deterministic_review_func=deterministic,
                ai_first_review_func=ai_first,
            )

        ai_first.assert_called_once()
        deterministic.assert_called_once_with("NDA text", paragraphs=None)
        self.assertEqual(result["review_mode"], "deterministic")
        self.assertEqual(result["ai_first_review"]["status"], "failed")
        self.assertEqual(result["active_review_engine"]["selected_engine"], REVIEW_ENGINE_AI_FIRST)
        self.assertEqual(result["active_review_engine"]["executed_engine"], REVIEW_ENGINE_DETERMINISTIC)
        self.assertEqual(result["active_review_engine"]["fallback_mode"], FALLBACK_MODE_DETERMINISTIC)
        self.assertTrue(result["active_review_engine"]["fallback_used"])
        self.assertIn("deterministic fallback", result["review_warnings"][0])
        counters = telemetry.snapshot()["counters"]
        self.assertEqual(counters["active_review_ai_first_attempted"], 1)
        self.assertEqual(counters["active_review_ai_first_failed"], 1)
        self.assertEqual(counters["active_review_ai_first_fallback_deterministic"], 1)

    def test_ai_first_fail_closed_errors_are_normalized_for_routes(self):
        with patch.dict(os.environ, {ACTIVE_REVIEW_ENGINE_ENV: "ai_first", AI_FIRST_FALLBACK_MODE_ENV: "fail_closed"}):
            with self.assertRaisesRegex(ActiveReviewEngineError, "AI-first review failed"):
                review_nda_with_active_engine(
                    "NDA text",
                    deterministic_review_func=Mock(),
                    ai_first_review_func=Mock(side_effect=AIAssessorError("no key")),
                )
        counters = telemetry.snapshot()["counters"]
        self.assertEqual(counters["active_review_ai_first_attempted"], 1)
        self.assertEqual(counters["active_review_ai_first_failed"], 1)
        self.assertEqual(counters["active_review_ai_first_fail_closed"], 1)

    def test_active_review_engine_status_exposes_engine_and_fallback(self):
        with patch.dict(os.environ, {ACTIVE_REVIEW_ENGINE_ENV: "ai-first", AI_FIRST_FALLBACK_MODE_ENV: "error"}):
            status = active_review_engine_status()

        self.assertEqual(status["active_engine"], REVIEW_ENGINE_AI_FIRST)
        self.assertEqual(status["ai_first_fallback_mode"], FALLBACK_MODE_FAIL_CLOSED)
        self.assertIn(REVIEW_ENGINE_AI_FIRST, status["supported_engines"])


if __name__ == "__main__":
    unittest.main()
