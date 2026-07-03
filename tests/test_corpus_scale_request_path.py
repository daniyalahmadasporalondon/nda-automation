"""Request-path scale fixes for /api/corpus: bounded fingerprint work + single-flight.

Covers the F1 (P0) fixes layered over the bucketed-dedup/app-state-cache hardening:

* **Bounded inline fingerprint compute**: a build computes at most
  ``FINGERPRINT_INLINE_COMPUTE_BUDGET`` missing fingerprints on the request thread.
  A large cold store (every matter missing its fingerprint -- the whole prod store
  the first time the dup feature sees it) must NOT serialize O(store) SimHash
  computes onto one request; the remainder is reported pending and handed to the
  background backfill.
* **Honest degradation**: the payload carries ``duplicate_scan`` = {pending,
  complete} so a consumer can render "duplicate scan pending (N remaining)" instead
  of silently under-reporting duplicates while the backfill converges.
* **Background backfill**: computes + persists the remainder off-thread
  (single-flight per owner) through the same lazy-cache writer, converging the
  store so later builds are pure scalar-compares.
* **Single-flight builds**: concurrent build_corpus calls for one owner share the
  leader's payload instead of stacking N identical builds.

Pure/unit: no HTTP, no Drive; the in-memory repository via public seams.
"""

from __future__ import annotations

import threading
import unittest

from nda_automation import content_fingerprint, corpus_index
from nda_automation.matter_repository import InMemoryMatterRepository

_NDA_TEXT_TEMPLATE = (
    "This mutual non disclosure agreement number {n} is entered into between the "
    "parties for the protection of confidential information exchanged in "
    "discussions concerning a potential business relationship and related matters."
)


def _seed(repo, *, owner: str, title: str, n: int):
    return repo.create_matter(
        source_filename=f"{title}.docx",
        document_bytes=b"PK\x03\x04 fake docx",
        extracted_text=_NDA_TEXT_TEMPLATE.format(n=n),
        review_result={
            "active_review_engine": {"executed_engine": "ai_first"},
            "clauses": [{"id": "mutuality", "decision": "pass"}],
        },
        triage={"triage_status": "review"},
        source_type="manual_upload",
        board_column="in_review",
        intake_metadata={"subject": title},
        owner_user_id=owner,
    )


def _drain_backfill_threads() -> None:
    """Join any live corpus fingerprint-backfill worker threads (test hygiene)."""
    for thread in threading.enumerate():
        if thread.name.startswith("corpus-fingerprint-backfill-"):
            thread.join(timeout=30)


class BoundedInlineFingerprintTests(unittest.TestCase):
    def setUp(self):
        corpus_index.invalidate_cache()
        self.repo = InMemoryMatterRepository()
        self.addCleanup(corpus_index.invalidate_cache)
        self.addCleanup(_drain_backfill_threads)

    def _count_computes(self):
        seen = {"compute": 0}
        original = corpus_index.content_fingerprint.compute_fingerprint

        def _counting(text):
            seen["compute"] += 1
            return original(text)

        corpus_index.content_fingerprint.compute_fingerprint = _counting
        self.addCleanup(
            setattr, corpus_index.content_fingerprint, "compute_fingerprint", original
        )
        return seen

    def test_cold_large_store_computes_nothing_inline(self):
        # A store whose missing-fingerprint set EXCEEDS the budget (the prod store
        # the first time the dup feature sees it): the request must compute ZERO
        # fingerprints inline — no O(store) computes AND no store writes on the
        # request thread — and report every missing matter pending.
        total = corpus_index.FINGERPRINT_INLINE_COMPUTE_BUDGET + 7
        for i in range(total):
            _seed(self.repo, owner="o", title=f"NDA {i}", n=i)
        # Neutralize the background backfill for THIS assertion so only the inline
        # request-path computes are counted (the backfill is tested separately).
        original_start = corpus_index._start_fingerprint_backfill
        corpus_index._start_fingerprint_backfill = lambda *a, **k: False
        self.addCleanup(
            setattr, corpus_index, "_start_fingerprint_backfill", original_start
        )
        seen = self._count_computes()

        payload = corpus_index.build_corpus(self.repo, "o", "")

        self.assertEqual(seen["compute"], 0)
        self.assertEqual(payload["duplicate_scan"], {"pending": total, "complete": False})

    def test_missing_set_within_budget_computes_all_inline(self):
        # Exactly at the budget: legacy behavior — everything computes + persists
        # in this build and the dup scan is complete.
        total = corpus_index.FINGERPRINT_INLINE_COMPUTE_BUDGET
        for i in range(total):
            _seed(self.repo, owner="o", title=f"NDA {i}", n=i)
        seen = self._count_computes()

        payload = corpus_index.build_corpus(self.repo, "o", "")

        self.assertEqual(seen["compute"], total)
        self.assertEqual(payload["duplicate_scan"], {"pending": 0, "complete": True})

    def test_small_corpus_completes_in_one_build_exactly_as_before(self):
        # Below the budget the legacy behavior holds: first build computes +
        # persists everything, dup scan is complete, and the payload says so.
        a = _seed(self.repo, owner="o", title="A", n=1)
        _seed(self.repo, owner="o", title="B", n=2)
        seen = self._count_computes()

        payload = corpus_index.build_corpus(self.repo, "o", "")

        self.assertEqual(seen["compute"], 2)
        self.assertEqual(payload["duplicate_scan"], {"pending": 0, "complete": True})
        stored = self.repo.get_matter(a["id"], owner_user_id="o")
        self.assertTrue(
            content_fingerprint.is_valid_fingerprint(
                stored.get(corpus_index.MATTER_FINGERPRINT_FIELD)
            )
        )

    def test_unfingerprintable_matters_are_never_pending(self):
        # Empty extracted_text can never fingerprint; it must not wedge the pending
        # counter above zero forever.
        matter = _seed(self.repo, owner="o", title="Empty", n=1)
        for stored in self.repo._matters:
            if stored["id"] == matter["id"]:
                stored["extracted_text"] = ""
        payload = corpus_index.build_corpus(self.repo, "o", "")
        self.assertEqual(payload["duplicate_scan"], {"pending": 0, "complete": True})

    def test_backfill_converges_the_store_and_the_pending_count(self):
        total = corpus_index.FINGERPRINT_INLINE_COMPUTE_BUDGET + 5
        for i in range(total):
            _seed(self.repo, owner="o", title=f"NDA {i}", n=i)

        first = corpus_index.build_corpus(self.repo, "o", "")
        self.assertEqual(first["duplicate_scan"]["pending"], total)

        _drain_backfill_threads()

        # Every matter now carries a persisted fingerprint...
        for stored in self.repo.list_matters(owner_user_id="o"):
            self.assertTrue(
                content_fingerprint.is_valid_fingerprint(
                    stored.get(corpus_index.MATTER_FINGERPRINT_FIELD)
                ),
                f"matter {stored['id']} missing fingerprint after backfill",
            )
        # ...and the next build is complete with zero pending.
        second = corpus_index.build_corpus(self.repo, "o", "")
        self.assertEqual(second["duplicate_scan"], {"pending": 0, "complete": True})

    def test_backfill_is_single_flight_per_owner(self):
        started = threading.Event()
        release = threading.Event()

        def _slow_run(repository, owner_user_id, matter_ids):
            started.set()
            release.wait(timeout=10)
            return {"state": "done", "computed": 0, "skipped": 0, "failed": 0}

        original = corpus_index._run_fingerprint_backfill
        corpus_index._run_fingerprint_backfill = _slow_run
        self.addCleanup(setattr, corpus_index, "_run_fingerprint_backfill", original)

        self.assertTrue(corpus_index._start_fingerprint_backfill(self.repo, "o", ["m1"]))
        self.assertTrue(started.wait(timeout=5))
        # A second start for the same owner while one runs is refused...
        self.assertFalse(corpus_index._start_fingerprint_backfill(self.repo, "o", ["m2"]))
        # ...but another owner is independent.
        self.assertTrue(corpus_index._start_fingerprint_backfill(self.repo, "p", ["m3"]))
        release.set()
        _drain_backfill_threads()
        # After completion the owner can start again (converged stores refuse via
        # the empty pending list, not via a stuck run-flag).
        self.assertTrue(corpus_index._start_fingerprint_backfill(self.repo, "o", ["m4"]))
        _drain_backfill_threads()

    def test_backfill_status_reports_the_sweep(self):
        total = corpus_index.FINGERPRINT_INLINE_COMPUTE_BUDGET + 3
        for i in range(total):
            _seed(self.repo, owner="o", title=f"NDA {i}", n=i)
        corpus_index.build_corpus(self.repo, "o", "")
        _drain_backfill_threads()
        status = corpus_index.fingerprint_backfill_status("o")
        self.assertEqual(status.get("state"), "done")
        self.assertEqual(status.get("computed"), total)


class SingleFlightBuildTests(unittest.TestCase):
    def setUp(self):
        corpus_index.invalidate_cache()
        self.repo = InMemoryMatterRepository()
        self.addCleanup(corpus_index.invalidate_cache)
        self.addCleanup(_drain_backfill_threads)

    def test_concurrent_builds_share_one_leader_build(self):
        _seed(self.repo, owner="o", title="Shared NDA", n=1)

        build_calls = {"count": 0}
        entered = threading.Event()
        release = threading.Event()
        original = corpus_index._build_corpus_payload

        def _slow_build(*args, **kwargs):
            build_calls["count"] += 1
            entered.set()
            release.wait(timeout=10)
            return original(*args, **kwargs)

        corpus_index._build_corpus_payload = _slow_build
        self.addCleanup(setattr, corpus_index, "_build_corpus_payload", original)

        results: list[dict] = []
        errors: list[BaseException] = []

        def _request():
            try:
                results.append(corpus_index.build_corpus(self.repo, "o", ""))
            except BaseException as error:  # noqa: BLE001 -- surfaced via the assertion
                errors.append(error)

        threads = [threading.Thread(target=_request) for _ in range(4)]
        threads[0].start()
        self.assertTrue(entered.wait(timeout=5))  # leader is inside the build
        for thread in threads[1:]:
            thread.start()
        # Give followers a moment to park on the in-flight entry, then release.
        release.set()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 4)
        # Exactly ONE build ran for the burst (followers rode the leader). Allow 2
        # for the benign race where a follower arrives after the leader finished
        # (it becomes its own leader) -- but never one build per caller.
        self.assertLessEqual(build_calls["count"], 2)
        titles = [
            sorted(m["title"] for g in payload["groups"] for m in g["matters"])
            for payload in results
        ]
        self.assertTrue(all(t == ["Shared NDA"] for t in titles))

    def test_followers_get_independent_payload_copies(self):
        _seed(self.repo, owner="o", title="Copy NDA", n=1)
        first = corpus_index.build_corpus(self.repo, "o", "")
        second = corpus_index.build_corpus(self.repo, "o", "")
        first["groups"][0]["matters"][0]["title"] = "MUTATED"
        self.assertNotEqual(
            second["groups"][0]["matters"][0]["title"], "MUTATED"
        )

    def test_failed_leader_never_wedges_followers(self):
        _seed(self.repo, owner="o", title="Recovering NDA", n=1)
        original = corpus_index._build_corpus_payload
        calls = {"count": 0}

        def _failing_then_ok(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("leader build exploded")
            return original(*args, **kwargs)

        corpus_index._build_corpus_payload = _failing_then_ok
        self.addCleanup(setattr, corpus_index, "_build_corpus_payload", original)

        with self.assertRaises(RuntimeError):
            corpus_index.build_corpus(self.repo, "o", "")
        # The in-flight entry was cleared on the failure path: the next call leads
        # a fresh build and succeeds.
        payload = corpus_index.build_corpus(self.repo, "o", "")
        self.assertEqual(payload["matter_count"], 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
