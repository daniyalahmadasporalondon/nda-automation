import os
import unittest
from unittest.mock import Mock, patch

from nda_automation import app_settings, telemetry
from nda_automation.ai_assessor import AIAssessorError
from nda_automation.playbook_runtime import ActivePlaybookBundle
from nda_automation.review_engine import (
    ACTIVE_REVIEW_ENGINE_ENV,
    REVIEW_ENGINE_AI_FIRST,
    REVIEW_ENGINE_DETERMINISTIC,
    ActiveReviewEngineError,
    active_review_engine_status,
    active_review_engine,
    review_nda_with_active_engine,
)


def _playbook_runtime():
    return {
        "active_version_id": "pbv_test",
        "active_hash": "sha256:" + "a" * 64,
        "playbook_name": "Test Playbook",
        "playbook_version": "2026.06",
        "published_at": "2026-06-05T00:00:00+00:00",
        "published_by": "legal-admin",
        "source": "publish",
    }


def _playbook_bundle(playbook):
    return ActivePlaybookBundle(playbook=playbook, runtime=_playbook_runtime())


class ReviewEngineTests(unittest.TestCase):
    def setUp(self):
        telemetry.reset()
        self.runtime_settings = {
            "active_review_engine": None,
        }
        self.runtime_settings_patch = patch(
            "nda_automation.review_engine.app_settings.review_runtime_settings",
            lambda: self.runtime_settings,
        )
        self.runtime_settings_patch.start()
        self.addCleanup(self.runtime_settings_patch.stop)

    def test_active_review_engine_defaults_to_ai_first(self):
        deterministic = Mock(return_value={"review_mode": "deterministic"})
        ai_first = Mock(return_value={"review_mode": "ai_first_compat"})

        with patch.dict(os.environ, {ACTIVE_REVIEW_ENGINE_ENV: ""}):
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
        self.assertEqual(status["engine_source"], "default")
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
                playbook_runtime_func=_playbook_runtime,
            )

        deterministic.assert_not_called()
        ai_first.assert_called_once_with("Clause text", paragraphs=paragraphs)
        self.assertEqual(result["review_mode"], "ai_first_compat")
        self.assertEqual(result["active_review_engine"]["engine"], REVIEW_ENGINE_AI_FIRST)
        self.assertEqual(result["active_review_engine"]["selected_engine"], REVIEW_ENGINE_AI_FIRST)
        self.assertEqual(result["active_review_engine"]["executed_engine"], REVIEW_ENGINE_AI_FIRST)
        self.assertEqual(result["playbook_runtime"], {
            "active_version_id": "pbv_test",
            "active_hash": "sha256:" + "a" * 64,
            "playbook_name": "Test Playbook",
            "playbook_version": "2026.06",
            "published_at": "2026-06-05T00:00:00+00:00",
            "published_by": "legal-admin",
            "source": "active",
            "active_source": "publish",
        })
        counters = telemetry.snapshot()["counters"]
        self.assertEqual(counters["active_review_ai_first_attempted"], 1)
        self.assertEqual(counters["active_review_ai_first_completed"], 1)

    def test_deterministic_environment_is_ignored_for_normal_review(self):
        deterministic = Mock(return_value={"review_mode": "deterministic"})
        ai_first = Mock(return_value={"review_mode": "ai_first_compat"})

        with patch.dict(os.environ, {ACTIVE_REVIEW_ENGINE_ENV: "deterministic"}):
            result = review_nda_with_active_engine(
                "Clause text",
                deterministic_review_func=deterministic,
                ai_first_review_func=ai_first,
            )
            status = active_review_engine_status()

        deterministic.assert_not_called()
        ai_first.assert_called_once_with("Clause text", paragraphs=None)
        self.assertEqual(result["active_review_engine"]["selected_engine"], REVIEW_ENGINE_AI_FIRST)
        self.assertEqual(result["active_review_engine"]["executed_engine"], REVIEW_ENGINE_AI_FIRST)
        self.assertEqual(status["active_engine"], REVIEW_ENGINE_AI_FIRST)
        self.assertEqual(status["engine_source"], "default")
        self.assertEqual(status["environment_active_engine"], "")
        self.assertNotIn(REVIEW_ENGINE_DETERMINISTIC, status["supported_engines"])

    def test_stored_deterministic_engine_is_ignored_for_normal_review(self):
        deterministic = Mock(return_value={"review_mode": "deterministic"})
        ai_first = Mock(return_value={"review_mode": "ai_first_compat"})
        self.runtime_settings["active_review_engine"] = REVIEW_ENGINE_DETERMINISTIC

        with patch.dict(os.environ, {ACTIVE_REVIEW_ENGINE_ENV: ""}):
            result = review_nda_with_active_engine(
                "Clause text",
                deterministic_review_func=deterministic,
                ai_first_review_func=ai_first,
            )
            status = active_review_engine_status()

        deterministic.assert_not_called()
        ai_first.assert_called_once()
        self.assertEqual(result["active_review_engine"]["selected_engine"], REVIEW_ENGINE_AI_FIRST)
        self.assertEqual(result["active_review_engine"]["source"], "default")
        self.assertEqual(status["active_engine"], REVIEW_ENGINE_AI_FIRST)
        self.assertEqual(status["engine_source"], "default")

    def test_stored_deterministic_setting_migrates_to_ai_first(self):
        self.assertEqual(
            app_settings.review_runtime_settings_from_payload({"active_review_engine": "deterministic"}),
            {"active_review_engine": REVIEW_ENGINE_AI_FIRST},
        )

    def test_forced_deterministic_engine_records_playbook_runtime_metadata(self):
        deterministic = Mock(return_value={"review_mode": "deterministic"})
        ai_first = Mock(return_value={"review_mode": "ai_first_compat"})

        with patch.dict(os.environ, {ACTIVE_REVIEW_ENGINE_ENV: ""}):
            result = review_nda_with_active_engine(
                "Clause text",
                deterministic_review_func=deterministic,
                ai_first_review_func=ai_first,
                playbook_runtime_func=_playbook_runtime,
                force_engine=REVIEW_ENGINE_DETERMINISTIC,
            )

        deterministic.assert_called_once_with("Clause text", paragraphs=None)
        ai_first.assert_not_called()
        self.assertEqual(result["active_review_engine"]["selected_engine"], REVIEW_ENGINE_DETERMINISTIC)
        self.assertEqual(result["active_review_engine"]["executed_engine"], REVIEW_ENGINE_DETERMINISTIC)
        self.assertEqual(result["playbook_runtime"]["active_version_id"], "pbv_test")
        self.assertEqual(result["playbook_runtime"]["active_hash"], "sha256:" + "a" * 64)
        self.assertEqual(result["playbook_runtime"]["source"], "active")

    def test_deterministic_engine_receives_active_playbook_snapshot_from_bundle(self):
        playbook = {"name": "Bundled Playbook", "version": "snapshot", "clauses": []}
        captured = {}

        def deterministic(text, *, paragraphs=None, playbook=None):
            captured["playbook"] = playbook
            return {"review_mode": "deterministic"}

        with patch.dict(os.environ, {ACTIVE_REVIEW_ENGINE_ENV: ""}):
            review_nda_with_active_engine(
                "Clause text",
                deterministic_review_func=deterministic,
                playbook_runtime_func=lambda: _playbook_bundle(playbook),
                force_engine=REVIEW_ENGINE_DETERMINISTIC,
            )

        self.assertIs(captured["playbook"], playbook)

    def test_ai_first_engine_receives_active_playbook_snapshot_from_bundle(self):
        playbook = {"name": "Bundled Playbook", "version": "snapshot", "clauses": []}
        captured = {}

        def ai_first(text, *, paragraphs=None, playbook=None):
            captured["playbook"] = playbook
            return {"review_mode": "ai_first_compat"}

        with patch.dict(os.environ, {ACTIVE_REVIEW_ENGINE_ENV: "ai_first"}):
            review_nda_with_active_engine(
                "Clause text",
                ai_first_review_func=ai_first,
                playbook_runtime_func=lambda: _playbook_bundle(playbook),
            )

        self.assertIs(captured["playbook"], playbook)

    def test_runtime_settings_select_ai_first_when_environment_is_unset(self):
        deterministic = Mock(return_value={"review_mode": "deterministic"})
        ai_first = Mock(return_value={"review_mode": "ai_first_compat"})
        self.runtime_settings["active_review_engine"] = REVIEW_ENGINE_AI_FIRST

        with patch.dict(os.environ, {ACTIVE_REVIEW_ENGINE_ENV: ""}):
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
        self.assertEqual(status["engine_source"], "runtime_settings")

    def test_environment_engine_overrides_runtime_settings(self):
        self.runtime_settings["active_review_engine"] = REVIEW_ENGINE_DETERMINISTIC

        with patch.dict(os.environ, {ACTIVE_REVIEW_ENGINE_ENV: "ai_first"}):
            status = active_review_engine_status()

        self.assertEqual(status["active_engine"], REVIEW_ENGINE_AI_FIRST)
        self.assertEqual(status["engine_source"], "environment")

    def test_ai_first_failure_blocks_review_by_default(self):
        deterministic = Mock(return_value={"review_mode": "deterministic"})
        ai_first = Mock(side_effect=AIAssessorError("no key"))

        with patch.dict(os.environ, {ACTIVE_REVIEW_ENGINE_ENV: ""}):
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

    def test_review_result_requires_complete_playbook_runtime_metadata(self):
        deterministic = Mock(return_value={"review_mode": "deterministic"})

        with patch.dict(os.environ, {ACTIVE_REVIEW_ENGINE_ENV: ""}):
            with self.assertRaisesRegex(ActiveReviewEngineError, "active_hash"):
                review_nda_with_active_engine(
                    "NDA text",
                    deterministic_review_func=deterministic,
                    playbook_runtime_func=lambda: {"active_version_id": "pbv_test"},
                    force_engine=REVIEW_ENGINE_DETERMINISTIC,
                )

    def test_ai_first_errors_are_normalized_for_routes(self):
        with patch.dict(os.environ, {ACTIVE_REVIEW_ENGINE_ENV: "ai_first"}):
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

    def test_active_review_engine_status_exposes_engine(self):
        with patch.dict(os.environ, {ACTIVE_REVIEW_ENGINE_ENV: "ai-first"}):
            status = active_review_engine_status()

        self.assertEqual(status["active_engine"], REVIEW_ENGINE_AI_FIRST)
        self.assertIn(REVIEW_ENGINE_AI_FIRST, status["supported_engines"])
        self.assertNotIn(REVIEW_ENGINE_DETERMINISTIC, status["supported_engines"])


if __name__ == "__main__":
    unittest.main()
