"""Regression: Playbook mutations invalidate the playbook-derived module caches.

``governing_law_view`` (dashboard search-intent / corpus-facet option maps),
``law_forum_check`` (approved-law JURISDICTIONS buckets) and
``dashboard_search_intent`` (the ``has_clause`` clause-id allowlist) cache data
derived from the active Playbook. Before the fix, publish/save/restore in
``playbook_authoring`` never invalidated them, so the process served the OLD
governing-law option / clause set until restart. These tests prove a
newly-approved law AND a newly-added clause become visible in all three modules
immediately after publish/save, and disappear again after a restore -- WITHOUT
any manual reset call.

A regression that moves the reset INSIDE the ``locked_playbook`` block deadlocks
(``law_forum_check.reset_buckets`` re-reads the Playbook through
``ensure_active_playbook_bundle`` -> ``locked_playbook``, and a second flock
acquisition on the same file in the same process blocks forever). The repo has no
pytest-timeout, so ``_run_mutation`` runs every mutation on a watchdog thread and
turns that hang into a loud failure instead of a wedged suite.
"""
from __future__ import annotations

import functools
import json
import tempfile
import threading
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from nda_automation import (
    dashboard_search_intent,
    governing_law_view,
    law_forum_check,
    playbook_authoring,
    playbook_runtime,
)
from nda_automation.checker import load_playbook
from nda_automation.governing_law_view import (
    governing_law_label,
    governing_law_option_ids,
    normalize_governing_law,
)
from tests.test_playbook_add_clause import _fe_scaffold_clause

# The user-authored dynamic clause added alongside the ruritania option; reuses the
# FE Add-Clause scaffold fixture, which is pinned to publish cleanly through the
# real validate + rules + lint gate.
_NEW_CLAUSE_ID = "ruritania_disclosure"


class PlaybookMutationCacheResetTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.playbook_path = Path(self._tmpdir.name) / "playbook.json"
        # The active playbook on disk must itself be valid so the mutation flows get
        # past the active-playbook bootstrap.
        self.active_playbook = deepcopy(load_playbook())
        self.playbook_path.write_text(json.dumps(self.active_playbook), encoding="utf-8")

        # CLEANUP ORDERING (addCleanup is LIFO): register the cache resets BEFORE
        # starting the ensure_active_playbook_bundle patch, so on teardown the patch
        # is undone FIRST and the resets then rebuild from the REAL playbook.
        # Otherwise every later test in the process would see temp-playbook-derived
        # governing-law state.
        self.addCleanup(governing_law_view.reset_caches)
        self.addCleanup(law_forum_check.reset_buckets)
        self.addCleanup(dashboard_search_intent.reset_clause_id_cache)

        # governing_law_view._approved_governing_law_options resolves
        # ``playbook_runtime.ensure_active_playbook_bundle`` at CALL time, so
        # patching that symbol redirects every derived-cache rebuild to the TEMP
        # playbook. (Patching PLAYBOOK_PATH would NOT work: default args bind at
        # def time.)
        patcher = patch.object(
            playbook_runtime,
            "ensure_active_playbook_bundle",
            functools.partial(
                playbook_runtime.ensure_active_playbook_bundle,
                playbook_path=self.playbook_path,
            ),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run_mutation(self, fn, *args, **kwargs):
        """Run a playbook mutation on a worker thread with a deadlock watchdog.

        Converts the documented reset-inside-the-lock deadlock into a failure
        instead of hanging the suite (see the module docstring). Exceptions are
        re-raised on the test thread; the mutation's return value is passed through.
        """
        result: dict = {}

        def _call() -> None:
            try:
                result["value"] = fn(*args, **kwargs)
            except BaseException as exc:  # noqa: BLE001 -- re-raised below.
                result["error"] = exc

        worker = threading.Thread(target=_call, daemon=True)
        worker.start()
        worker.join(timeout=20)
        if worker.is_alive():
            self.fail(
                "playbook mutation deadlocked -- the cache reset likely runs inside "
                "the locked_playbook block"
            )
        if "error" in result:
            raise result["error"]
        return result["value"]

    def _prime_caches_without_ruritania(self) -> None:
        """Reset then PRIME the derived caches from the (temp) playbook pre-mutation."""
        governing_law_view.reset_caches()
        law_forum_check.reset_buckets()
        dashboard_search_intent.reset_clause_id_cache()
        self.assertEqual(normalize_governing_law("Ruritania"), "")
        self.assertNotIn("ruritania", law_forum_check.approved_law_buckets())
        self.assertNotIn(_NEW_CLAUSE_ID, dashboard_search_intent.allowed_clause_ids())

    def _candidate_with_ruritania(self, base: dict) -> dict:
        """A copy of ``base`` with a new approved governing-law option, mirroring
        the exact key shape of an existing sibling option."""
        candidate = deepcopy(base)
        clause = next(c for c in candidate["clauses"] if c.get("id") == "governing_law")
        options = clause["rules"]["approved_options"]
        template = deepcopy(options[0])
        template.update({
            "id": "ruritania",
            "label": "Ruritania",
            "value": "Ruritania",
            "default": False,
            "forum_jurisdiction": "Strelsau, Ruritania",
            "aliases": ["Republic of Ruritania"],
        })
        if "entity_prefixes" in template:
            template["entity_prefixes"] = []
        options.append(template)
        # The governing_law clause's structured fields must stay consistent with the
        # options (validate_playbook_rules enforces the join): the option value must
        # appear in approved_laws and law_phrases must carry a phrase for it.
        clause["approved_laws"].append("Ruritania")
        clause["law_phrases"]["Ruritania"] = "the laws of Ruritania"
        # Also add a brand-new dynamic clause so the mutation exercises the
        # dashboard_search_intent clause-id allowlist cache too.
        candidate["clauses"].append(_fe_scaffold_clause(_NEW_CLAUSE_ID))
        return candidate

    def _assert_ruritania_visible(self) -> None:
        """The derived lookups serve the NEW option WITHOUT any manual reset call."""
        self.assertEqual(normalize_governing_law("Ruritania"), "ruritania")
        self.assertIn("ruritania", governing_law_option_ids())
        self.assertEqual(governing_law_label("ruritania"), "Ruritania")
        buckets = law_forum_check.approved_law_buckets()
        self.assertIn("ruritania", buckets)
        self.assertTrue(buckets["ruritania"]["law"])
        self.assertIn(_NEW_CLAUSE_ID, dashboard_search_intent.allowed_clause_ids())

    def test_publish_invalidates_derived_caches(self) -> None:
        self._prime_caches_without_ruritania()
        candidate = self._candidate_with_ruritania(self.active_playbook)
        response = self._run_mutation(
            playbook_authoring.publish_playbook,
            {"playbook": candidate, "actor": "legal-admin"},
            playbook_path=self.playbook_path,
        )
        self.assertEqual(response["playbook"], candidate)
        self._assert_ruritania_visible()

    def test_save_active_invalidates_derived_caches(self) -> None:
        self._prime_caches_without_ruritania()
        candidate = self._candidate_with_ruritania(self.active_playbook)
        response = self._run_mutation(
            playbook_authoring.save_active_playbook,
            {"playbook": candidate, "actor": "legal-admin"},
            playbook_path=self.playbook_path,
        )
        self.assertEqual(response["playbook"], candidate)
        self._assert_ruritania_visible()

    def test_restore_invalidates_derived_caches(self) -> None:
        # Publish the original first so history holds a restorable pre-ruritania
        # snapshot, then publish the ruritania option, then restore back.
        self._run_mutation(
            playbook_authoring.publish_playbook,
            {"playbook": deepcopy(self.active_playbook), "actor": "legal-admin"},
            playbook_path=self.playbook_path,
        )
        candidate = self._candidate_with_ruritania(self.active_playbook)
        response = self._run_mutation(
            playbook_authoring.publish_playbook,
            {"playbook": candidate, "actor": "legal-admin"},
            playbook_path=self.playbook_path,
        )
        self._assert_ruritania_visible()

        # The second-newest history entry is the pre-ruritania publish snapshot.
        history = response["history"]
        self.assertGreaterEqual(len(history), 2)
        restore_id = str(history[1]["id"])
        self._run_mutation(
            playbook_authoring.restore_playbook_history_entry,
            {"history_id": restore_id, "actor": "legal-admin"},
            playbook_path=self.playbook_path,
        )
        # The restore DROPPED ruritania + the new clause from the derived lookups --
        # no manual reset.
        self.assertEqual(normalize_governing_law("Ruritania"), "")
        self.assertNotIn("ruritania", governing_law_option_ids())
        self.assertNotIn("ruritania", law_forum_check.approved_law_buckets())
        self.assertNotIn(_NEW_CLAUSE_ID, dashboard_search_intent.allowed_clause_ids())


if __name__ == "__main__":
    unittest.main()
