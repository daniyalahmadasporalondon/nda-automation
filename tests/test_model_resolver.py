"""Unit tests for the central per-role AI model resolver.

Covers, per the build spec:
  - precedence per role: persisted (ai_models) > legacy (reviewer only) > env > default;
  - decoupling proof: setting dashboard_assistant does NOT move reviewer, and vice-versa;
  - behaviour-unchanged proof: with NO persisted settings, every role resolves to
    today's effective model (the three decoupled roles -> opus-4.8-fast).
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from nda_automation import app_settings, matter_store, model_resolver


# The effective model every role MUST resolve to with NO persisted settings and NO
# env var set -- i.e. today's deployed behaviour. The three decoupled roles default
# to the reviewer's effective model so the decoupling is behaviour-neutral.
TODAYS_DEFAULTS = {
    "reviewer": "anthropic/claude-opus-4.8-fast",
    "verifier": "deepseek/deepseek-v4-pro",
    "structure": "deepseek/deepseek-v4-flash",
    "semantic_lint": "anthropic/claude-opus-4.8",
    "generation": "deepseek/deepseek-v4-flash",
    "gmail_triage": "deepseek/deepseek-v4-pro",
    "gmail_intake": "deepseek/deepseek-v4-flash",
    "pdf_ocr": "google/gemini-2.5-flash",
    "dashboard_assistant": "anthropic/claude-opus-4.8-fast",
    "search_intent": "anthropic/claude-opus-4.8-fast",
    "matter_summary": "anthropic/claude-opus-4.8-fast",
}


class ModelResolverTests(unittest.TestCase):
    def setUp(self):
        model_resolver._reset_caches_for_tests()
        self.addCleanup(model_resolver._reset_caches_for_tests)
        # Strip every NDA_*_MODEL env var so env never leaks into the default-path
        # tests; the env-precedence test sets them explicitly.
        self._model_env_keys = [
            k for k in os.environ if k.endswith("_MODEL") and k.startswith("NDA")
        ]
        self._patches = [patch.dict(os.environ, {}, clear=False)]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        for k in self._model_env_keys:
            os.environ.pop(k, None)

    def _temp_data_dir(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return patch.object(matter_store, "DATA_DIR", model_resolver_path(tmp.name))

    # --- behaviour-unchanged -------------------------------------------------

    def test_all_roles_resolve_to_todays_defaults_with_no_settings(self):
        with self._temp_data_dir():
            for role in model_resolver.ROLES:
                with self.subTest(role=role):
                    self.assertEqual(
                        model_resolver.resolve_model(role), TODAYS_DEFAULTS[role]
                    )

    def test_role_count_is_eleven(self):
        self.assertEqual(len(model_resolver.ROLES), 11)
        self.assertEqual(set(model_resolver.ROLES), set(TODAYS_DEFAULTS))

    # --- env precedence ------------------------------------------------------

    def test_env_var_overrides_default(self):
        with self._temp_data_dir():
            with patch.dict(os.environ, {"NDA_STRUCTURE_VALIDATION_MODEL": "x/env-structure"}):
                self.assertEqual(model_resolver.resolve_model("structure"), "x/env-structure")

    def test_decoupled_role_env_overrides_default(self):
        with self._temp_data_dir():
            with patch.dict(os.environ, {"NDA_MATTER_SUMMARY_MODEL": "x/env-summary"}):
                self.assertEqual(model_resolver.resolve_model("matter_summary"), "x/env-summary")

    # --- persisted precedence ------------------------------------------------

    def test_persisted_overrides_env_and_default(self):
        with self._temp_data_dir():
            app_settings.update_model_settings({"verifier": "x/persisted-verifier"})
            with patch.dict(os.environ, {"NDA_AI_VERIFIER_MODEL": "x/env-verifier"}):
                self.assertEqual(
                    model_resolver.resolve_model("verifier"), "x/persisted-verifier"
                )

    def test_clearing_persisted_falls_back_to_env(self):
        with self._temp_data_dir():
            app_settings.update_model_settings({"structure": "x/persisted"})
            self.assertEqual(model_resolver.resolve_model("structure"), "x/persisted")
            app_settings.update_model_settings({"structure": ""})  # clear
            with patch.dict(os.environ, {"NDA_STRUCTURE_VALIDATION_MODEL": "x/env"}):
                self.assertEqual(model_resolver.resolve_model("structure"), "x/env")

    # --- reviewer legacy layer ----------------------------------------------

    def test_reviewer_honours_legacy_ai_review_setting_over_env(self):
        # The legacy ai_review "model" setting (what the existing reviewer picker
        # writes) sits BETWEEN persisted ai_models and env, per the locked contract.
        with self._temp_data_dir():
            app_settings.update_ai_settings({"model": "x/legacy-reviewer", "provider": "openrouter"})
            with patch.dict(os.environ, {"NDA_AI_MODEL": "x/env-reviewer"}):
                self.assertEqual(model_resolver.resolve_model("reviewer"), "x/legacy-reviewer")

    def test_reviewer_ai_models_beats_legacy(self):
        with self._temp_data_dir():
            app_settings.update_ai_settings({"model": "x/legacy-reviewer", "provider": "openrouter"})
            app_settings.update_model_settings({"reviewer": "x/new-reviewer"})
            self.assertEqual(model_resolver.resolve_model("reviewer"), "x/new-reviewer")

    # --- decoupling proof ----------------------------------------------------

    def test_setting_dashboard_assistant_does_not_move_reviewer(self):
        with self._temp_data_dir():
            app_settings.update_model_settings({"dashboard_assistant": "x/cheap-assistant"})
            self.assertEqual(model_resolver.resolve_model("dashboard_assistant"), "x/cheap-assistant")
            # Reviewer is untouched -- still today's default.
            self.assertEqual(
                model_resolver.resolve_model("reviewer"), TODAYS_DEFAULTS["reviewer"]
            )

    def test_setting_reviewer_does_not_move_decoupled_roles(self):
        with self._temp_data_dir():
            app_settings.update_model_settings({"reviewer": "x/new-reviewer"})
            self.assertEqual(model_resolver.resolve_model("reviewer"), "x/new-reviewer")
            for role in ("dashboard_assistant", "search_intent", "matter_summary"):
                with self.subTest(role=role):
                    self.assertEqual(
                        model_resolver.resolve_model(role), TODAYS_DEFAULTS[role]
                    )

    # --- detail / overview ---------------------------------------------------

    def test_resolve_detail_reports_source(self):
        with self._temp_data_dir():
            d = model_resolver.resolve_model_detail("structure")
            self.assertEqual(d.source, "default")
            with patch.dict(os.environ, {"NDA_STRUCTURE_VALIDATION_MODEL": "x/env"}):
                self.assertEqual(model_resolver.resolve_model_detail("structure").source, "env")
            app_settings.update_model_settings({"structure": "x/persisted"})
            self.assertEqual(model_resolver.resolve_model_detail("structure").source, "persisted")

    def test_overview_has_all_roles_with_recommended(self):
        with self._temp_data_dir():
            overview = model_resolver.role_model_overview()
            self.assertEqual([e["role"] for e in overview], list(model_resolver.ROLES))
            for entry in overview:
                self.assertIn("model", entry)
                self.assertIn("source", entry)
                self.assertIn("env_var", entry)
                self.assertIn("default", entry)
                self.assertIsInstance(entry["recommended"], list)
                self.assertTrue(entry["recommended"], f"role {entry['role']} has no recommendations")
                self.assertIn("enabled", entry)
                self.assertIsInstance(entry["enabled"], bool)

    def test_unknown_role_raises(self):
        with self.assertRaises(KeyError):
            model_resolver.resolve_model("not_a_role")


class RoleFeatureEnabledTests(unittest.TestCase):
    """The informational `enabled` flag mirrors each feature's real gate.

    Two roles are dormant by default (their picked model never runs until the
    feature is turned on): `pdf_ocr` and `structure`. Everything else is on.
    """

    def setUp(self):
        model_resolver._reset_caches_for_tests()
        self.addCleanup(model_resolver._reset_caches_for_tests)
        # Isolate the two gate env flags so the process env can't leak into either
        # direction of these assertions.
        p = patch.dict(os.environ, {}, clear=False)
        p.start()
        self.addCleanup(p.stop)
        for key in ("NDA_PDF_OCR_ENABLED", "NDA_STRUCTURE_VALIDATION_ENABLED"):
            os.environ.pop(key, None)

    def test_dormant_roles_report_disabled_by_default(self):
        self.assertFalse(model_resolver.role_feature_enabled("pdf_ocr"))
        self.assertFalse(model_resolver.role_feature_enabled("structure"))

    def test_other_roles_report_enabled(self):
        for role in model_resolver.ROLES:
            if role in ("pdf_ocr", "structure"):
                continue
            self.assertTrue(
                model_resolver.role_feature_enabled(role),
                f"role {role} should report enabled=True",
            )

    def test_pdf_ocr_flips_true_when_its_flag_is_on(self):
        with patch.dict(os.environ, {"NDA_PDF_OCR_ENABLED": "1"}):
            self.assertTrue(model_resolver.role_feature_enabled("pdf_ocr"))

    def test_structure_flips_true_when_its_flag_is_on(self):
        with patch.dict(os.environ, {"NDA_STRUCTURE_VALIDATION_ENABLED": "true"}):
            self.assertTrue(model_resolver.role_feature_enabled("structure"))

    def test_overview_enabled_tracks_gates(self):
        with tempfile.TemporaryDirectory() as tmp:
            from pathlib import Path

            with patch.object(matter_store, "DATA_DIR", Path(tmp)):
                by_role = {e["role"]: e for e in model_resolver.role_model_overview()}
                self.assertFalse(by_role["pdf_ocr"]["enabled"])
                self.assertFalse(by_role["structure"]["enabled"])
                self.assertTrue(by_role["reviewer"]["enabled"])

            with patch.dict(
                os.environ,
                {"NDA_PDF_OCR_ENABLED": "1", "NDA_STRUCTURE_VALIDATION_ENABLED": "on"},
            ), patch.object(matter_store, "DATA_DIR", Path(tmp)):
                by_role = {e["role"]: e for e in model_resolver.role_model_overview()}
                self.assertTrue(by_role["pdf_ocr"]["enabled"])
                self.assertTrue(by_role["structure"]["enabled"])


def model_resolver_path(name):
    # Small helper kept module-local so the test reads cleanly; matter_store.DATA_DIR
    # is a pathlib.Path, so wrap the temp dir name.
    from pathlib import Path

    return Path(name)


if __name__ == "__main__":
    unittest.main()
