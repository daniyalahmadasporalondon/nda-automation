"""Perf regression guard for the board-list (``GET /api/matters``) playbook read.

``matter_view.public_matters`` runs ``public_matter`` -> ``workflow_state`` ->
the approval-gate staleness chain for EVERY matter. Each staleness check resolves
the active playbook runtime, which (``playbook_runtime.ensure_active_playbook_runtime``)
takes an exclusive flock, reads ``playbook.json`` and runs ``validate_playbook``.

Pre-fix, that resolution happened once PER MATTER -> N flock+read+validate cycles
per board poll, every 15s per session, all serialized on one lock. The fix
resolves the runtime ONCE per ``public_matters`` call and threads the constant
resolvers down (mirroring ``corpus_index.build_corpus``).

These tests count the resolutions and assert O(1), and assert the batched
verdicts are byte-identical to the per-matter (unbatched) resolution.
"""

from __future__ import annotations

import unittest

from nda_automation import matter_view, playbook_runtime, workflow


def _matter_reaching_staleness(n: int) -> dict:
    """A matter dict that carries a review_result so public_matter ->
    workflow_state -> _approval_status -> review_is_stale -> review_result_staleness
    actually resolves the playbook runtime (the hot path the fix batches)."""
    return {
        "id": f"matter-{n}",
        "source_filename": f"NDA {n}.docx",
        "extracted_text": "This Agreement is mutual.",
        "review_result": {"clauses": [{"id": "mutuality", "decision": "pass"}]},
        "triage": {"triage_status": "review"},
        "board_column": "in_review",
        "intake_metadata": {"subject": f"Entity {n}"},
        "owner_user_id": "owner-a",
    }


class BoardPollPlaybookOnceTests(unittest.TestCase):
    def test_playbook_runtime_resolved_once_per_board_list(self):
        matters = [_matter_reaching_staleness(n) for n in range(5)]

        real = playbook_runtime.ensure_active_playbook_runtime
        calls = {"n": 0}

        def counting_resolver(*args, **kwargs):
            calls["n"] += 1
            return real(*args, **kwargs)

        original = playbook_runtime.ensure_active_playbook_runtime
        playbook_runtime.ensure_active_playbook_runtime = counting_resolver
        try:
            public = matter_view.public_matters(matters)
        finally:
            playbook_runtime.ensure_active_playbook_runtime = original

        # The whole board list of 5 matters resolves the playbook exactly ONCE
        # (O(1)), not once per matter (which was 5 pre-fix).
        self.assertEqual(calls["n"], 1)
        # And every matter was still projected (the batch ran over all of them).
        self.assertEqual(len(public), 5)
        for view in public:
            self.assertIn("workflow_state", view)

    def test_single_matter_detail_path_unchanged(self):
        # public_matter called for ONE matter with no resolvers passed (the detail
        # path) must still produce a correct workflow_state via the lazy staleness
        # default -- byte-identical to a bare workflow_state(matter). The fix only
        # changes WHEN the runtime is resolved in the batch loop, never the verdict.
        matter = _matter_reaching_staleness(0)
        expected = workflow.workflow_state(matter)
        view = matter_view.public_matter(matter)  # detail=True, no resolvers
        self.assertIn("workflow_state", view)
        self.assertEqual(view["workflow_state"]["phase"], expected["phase"])
        self.assertEqual(view["workflow_state"]["status"], expected["status"])
        self.assertEqual(
            view["workflow_state"]["board_column"], expected["board_column"]
        )

    def test_batched_verdicts_identical_to_unbatched(self):
        # The batched resolver must produce byte-identical workflow verdicts to the
        # unbatched per-matter resolution (same phase/status/board_column).
        matters = [_matter_reaching_staleness(n) for n in range(3)]
        baseline = {m["id"]: workflow.workflow_state(m) for m in matters}

        public = matter_view.public_matters(matters)
        by_id = {v["id"]: v for v in public}
        for matter_id, expected in baseline.items():
            got = by_id[matter_id]["workflow_state"]
            self.assertEqual(got["phase"], expected["phase"])
            self.assertEqual(got["status"], expected["status"])
            self.assertEqual(got["board_column"], expected["board_column"])
            self.assertEqual(got["needs_attention"], expected["needs_attention"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
