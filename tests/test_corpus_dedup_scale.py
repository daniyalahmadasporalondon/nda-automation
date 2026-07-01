"""Scale-hardening tests for the Corpus duplicate scan + app-state build cache.

Two independent scale defects are covered:

* **Bucketed dup detection** (``corpus_index._stamp_duplicate_document``): the flagged
  set MUST stay byte-identical to the historical all-pairs O(n²) scan while only
  comparing plausible candidates. The core guarantee is proven as a MUTATION CHECK --
  a randomized fuzz that runs the bucketed path and the retained all-pairs oracle
  (``_stamp_duplicate_document_full``) over the same random corpora and asserts every
  matter's stamped ``duplicate_document`` is identical. If bucketing ever drops a true
  pair or picks a different deterministic winner, some seed diverges and fails.

* **App-state build cache** (``corpus_index._build_app_state_matters``): an unchanged
  store must serve the cached build (no re-``list_matters``/re-build), and any change
  must invalidate it.

Pure/unit: no HTTP, no Drive. The dup tests drive the internal stamp functions with
hand-built fingerprint dicts (the bucketing operates purely on fingerprint fields, so
synthetic fingerprints exercise it exhaustively and deterministically). The cache
tests use the in-memory repository via the public ``build_corpus`` seam.
"""

from __future__ import annotations

import copy
import random
import unittest

from nda_automation import content_fingerprint, corpus_index
from nda_automation.matter_repository import InMemoryMatterRepository

_FP_KEY = corpus_index._FINGERPRINT_KEY


# --- fingerprint / matter fixture builders --------------------------------
def _fingerprint(exact: str, simhash: int | None) -> dict[str, object]:
    """A valid current-schema stored fingerprint dict with the given fields."""
    return {
        "schema_version": content_fingerprint.FINGERPRINT_SCHEMA_VERSION,
        "exact": exact,
        "simhash": None if simhash is None else str(simhash),
        "word_count": 50,
    }


def _matter(
    matter_id: str,
    *,
    counterparty: str,
    created_at: str,
    exact: str,
    simhash: int | None,
    title: str = "NDA",
) -> dict[str, object]:
    """A pre-group corpus matter carrying the internal fingerprint key."""
    return {
        "matter_id": matter_id,
        "counterparty": counterparty,
        "title": title,
        "created_at": created_at,
        _FP_KEY: _fingerprint(exact, simhash),
        "duplicate_document": None,
    }


def _dup_signals(matters: list[dict[str, object]]) -> dict[str, object]:
    """matter_id -> stamped duplicate_document, the comparison surface."""
    return {str(m["matter_id"]): m.get("duplicate_document") for m in matters}


# --- 1. dup detection: the bucketed path equals the all-pairs oracle -------
class BucketedDedupEquivalenceTests(unittest.TestCase):
    """The bucketed scan stamps the byte-identical result the all-pairs scan does."""

    def _assert_paths_agree(self, matters: list[dict[str, object]]) -> tuple[int, int]:
        """Run both stamp paths on independent copies; assert identical stamps.

        Returns (full_count, bucketed_count) so callers can additionally assert on
        the flagged count for a specific fixture.
        """
        for_full = copy.deepcopy(matters)
        for_bucketed = copy.deepcopy(matters)
        full_count = corpus_index._stamp_duplicate_document_full(for_full)
        bucketed_count = corpus_index._stamp_duplicate_document_bucketed(for_bucketed)
        self.assertEqual(
            _dup_signals(for_full),
            _dup_signals(for_bucketed),
            "bucketed dup stamps diverged from the all-pairs oracle",
        )
        self.assertEqual(full_count, bucketed_count)
        return full_count, bucketed_count

    def test_exact_dup_flagged_across_counterparties(self):
        # Identical text (shared exact sha256) is a dup regardless of counterparty.
        matters = [
            _matter("m1", counterparty="Acme", created_at="2026-01-01", exact="SAME", simhash=1),
            _matter("m2", counterparty="Globex", created_at="2026-02-01", exact="SAME", simhash=999),
        ]
        full_count, _ = self._assert_paths_agree(matters)
        self.assertEqual(full_count, 2)

    def test_near_dup_same_counterparty_at_threshold_boundary(self):
        # Hamming 4 (sim 0.9375 >= 0.93) within one counterparty -> both flagged.
        matters = [
            _matter("m1", counterparty="Acme", created_at="2026-01-01", exact="EXA", simhash=0b0000),
            _matter("m2", counterparty="Acme", created_at="2026-02-01", exact="EXB", simhash=0b1111),
        ]
        full_count, _ = self._assert_paths_agree(matters)
        self.assertEqual(full_count, 2)

    def test_near_dup_just_below_threshold_not_flagged(self):
        # Hamming 5 (sim 0.921875 < 0.93) -> neither flagged; both paths must agree.
        matters = [
            _matter("m1", counterparty="Acme", created_at="2026-01-01", exact="EXA", simhash=0b00000),
            _matter("m2", counterparty="Acme", created_at="2026-02-01", exact="EXB", simhash=0b11111),
        ]
        full_count, _ = self._assert_paths_agree(matters)
        self.assertEqual(full_count, 0)

    def test_near_dup_cross_counterparty_not_flagged(self):
        # A high-SimHash pair across DIFFERENT counterparties is a template sibling,
        # not a duplicate -- neither path flags it.
        matters = [
            _matter("m1", counterparty="Acme", created_at="2026-01-01", exact="EXA", simhash=0b0000),
            _matter("m2", counterparty="Globex", created_at="2026-02-01", exact="EXB", simhash=0b1111),
        ]
        full_count, _ = self._assert_paths_agree(matters)
        self.assertEqual(full_count, 0)

    def test_unknown_counterparty_never_near_dup_matches(self):
        # Two ownerless/unknown matters (normalized key "") must not near-dup each
        # other even at Hamming 0 (only an exact-sha match could flag them).
        unknown = "Unknown Counterparty"
        matters = [
            _matter("m1", counterparty=unknown, created_at="2026-01-01", exact="EXA", simhash=42),
            _matter("m2", counterparty=unknown, created_at="2026-02-01", exact="EXB", simhash=42),
        ]
        full_count, _ = self._assert_paths_agree(matters)
        self.assertEqual(full_count, 0)

    def test_deterministic_earliest_match_target_preserved(self):
        # m3 is an exact dup of BOTH m1 and m2; the stamped match must be the
        # EARLIEST by (created_at, matter_id) -- m1 -- on both paths.
        matters = [
            _matter("m1", counterparty="Acme", created_at="2026-01-01", exact="SAME", simhash=1, title="First"),
            _matter("m2", counterparty="Acme", created_at="2026-03-01", exact="SAME", simhash=1, title="Second"),
            _matter("m3", counterparty="Acme", created_at="2026-05-01", exact="SAME", simhash=1, title="Third"),
        ]
        self._assert_paths_agree(matters)
        # Pin the actual winner so a tie-break regression is caught head-on.
        stamped = copy.deepcopy(matters)
        corpus_index._stamp_duplicate_document_bucketed(stamped)
        signals = _dup_signals(stamped)
        self.assertEqual(signals["m3"]["matched_matter_id"], "m1")
        self.assertEqual(signals["m3"]["matched_title"], "First")

    def test_created_at_tie_broken_by_matter_id(self):
        # Same created_at: the tie-break falls to matter_id, then corpus index. m_b
        # and m_c both exact-match m_a; the earliest is m_a, and m_a's own match is
        # the earliest OTHER, i.e. m_b (id "m_b" < "m_c").
        matters = [
            _matter("m_a", counterparty="Acme", created_at="2026-01-01", exact="SAME", simhash=1),
            _matter("m_b", counterparty="Acme", created_at="2026-01-01", exact="SAME", simhash=1),
            _matter("m_c", counterparty="Acme", created_at="2026-01-01", exact="SAME", simhash=1),
        ]
        self._assert_paths_agree(matters)
        stamped = copy.deepcopy(matters)
        corpus_index._stamp_duplicate_document_bucketed(stamped)
        signals = _dup_signals(stamped)
        self.assertEqual(signals["m_a"]["matched_matter_id"], "m_b")
        self.assertEqual(signals["m_c"]["matched_matter_id"], "m_a")

    def test_matters_without_fingerprint_are_ignored_identically(self):
        # A matter with no/invalid fingerprint never participates -- both paths leave
        # it None and never match against it.
        no_fp = {
            "matter_id": "m_nofp",
            "counterparty": "Acme",
            "title": "NDA",
            "created_at": "2026-01-01",
            _FP_KEY: None,
            "duplicate_document": None,
        }
        matters = [
            no_fp,
            _matter("m1", counterparty="Acme", created_at="2026-02-01", exact="SAME", simhash=1),
            _matter("m2", counterparty="Acme", created_at="2026-03-01", exact="SAME", simhash=1),
        ]
        self._assert_paths_agree(matters)
        stamped = copy.deepcopy(matters)
        corpus_index._stamp_duplicate_document_bucketed(stamped)
        self.assertIsNone(_dup_signals(stamped)["m_nofp"])

    def test_missing_simhash_only_exact_dups_identically(self):
        # A fingerprint lacking a simhash can still exact-dup but never near-dup; both
        # paths must treat it the same.
        matters = [
            _matter("m1", counterparty="Acme", created_at="2026-01-01", exact="SAME", simhash=None),
            _matter("m2", counterparty="Acme", created_at="2026-02-01", exact="SAME", simhash=None),
            # near neighbour with a simhash but a DIFFERENT exact -> no near-dup
            # partner without a simhash on m1/m2, so it stays unflagged.
            _matter("m3", counterparty="Acme", created_at="2026-03-01", exact="OTHER", simhash=0b1111),
        ]
        full_count, _ = self._assert_paths_agree(matters)
        self.assertEqual(full_count, 2)  # m1 & m2 exact-dup each other; m3 alone.

    def test_mutation_fuzz_bucketed_equals_all_pairs(self):
        # MUTATION CHECK: over many random corpora, the bucketed path and the
        # all-pairs oracle must stamp byte-identical results. The generator seeds
        # exact-collision clusters, tight SimHash clusters (near-dups), loner random
        # SimHashes, shared/distinct counterparties, colliding created_at values (to
        # exercise the id/index tie-break) and fingerprint-less matters -- the full
        # input space of the dup gate. A single divergent seed fails loudly.
        rng = random.Random(20260701)
        bits = content_fingerprint._SIMHASH_BITS
        counterparties = ["Acme", "Globex", "Initech", "Unknown Counterparty", ""]
        for trial in range(300):
            n = rng.randint(0, 25)
            # A small pool of exact digests + cluster centres so collisions are common
            # (a corpus of all-unique fingerprints would rarely exercise a real match).
            exact_pool = [f"EX{ei}" for ei in range(rng.randint(1, 6))]
            centres = [rng.getrandbits(bits) for _ in range(rng.randint(1, 4))]
            dates = [f"2026-{rng.randint(1, 3):02d}-01" for _ in range(rng.randint(1, 4))]
            matters: list[dict[str, object]] = []
            for i in range(n):
                roll = rng.random()
                if roll < 0.15:
                    # No fingerprint at all.
                    matters.append(
                        {
                            "matter_id": f"m{i}",
                            "counterparty": rng.choice(counterparties),
                            "title": f"T{i}",
                            "created_at": rng.choice(dates),
                            _FP_KEY: None,
                            "duplicate_document": None,
                        }
                    )
                    continue
                simhash: int | None
                if roll < 0.3:
                    simhash = None  # exact-only fingerprint
                elif roll < 0.7:
                    # Near a cluster centre: flip 0-6 random bits (spans both sides of
                    # the Hamming-4 near-dup boundary so the gate is genuinely tested).
                    value = rng.choice(centres)
                    for _ in range(rng.randint(0, 6)):
                        value ^= 1 << rng.randrange(bits)
                    simhash = value
                else:
                    simhash = rng.getrandbits(bits)  # loner
                matters.append(
                    _matter(
                        f"m{i}",
                        counterparty=rng.choice(counterparties),
                        created_at=rng.choice(dates),
                        exact=rng.choice(exact_pool),
                        simhash=simhash,
                        title=f"T{i}",
                    )
                )
            for_full = copy.deepcopy(matters)
            for_bucketed = copy.deepcopy(matters)
            full_count = corpus_index._stamp_duplicate_document_full(for_full)
            bucketed_count = corpus_index._stamp_duplicate_document_bucketed(for_bucketed)
            self.assertEqual(
                _dup_signals(for_full),
                _dup_signals(for_bucketed),
                f"trial {trial}: bucketed diverged from all-pairs on {matters!r}",
            )
            self.assertEqual(full_count, bucketed_count, f"trial {trial}: count diverged")

    def test_public_stamp_uses_bucketed_and_falls_open(self):
        # The public entry equals the oracle on a normal corpus...
        matters = [
            _matter("m1", counterparty="Acme", created_at="2026-01-01", exact="SAME", simhash=1),
            _matter("m2", counterparty="Acme", created_at="2026-02-01", exact="SAME", simhash=1),
        ]
        via_public = copy.deepcopy(matters)
        via_oracle = copy.deepcopy(matters)
        self.assertEqual(
            corpus_index._stamp_duplicate_document(via_public),
            corpus_index._stamp_duplicate_document_full(via_oracle),
        )
        self.assertEqual(_dup_signals(via_public), _dup_signals(via_oracle))

    def test_bucketing_failure_falls_back_to_full_path(self):
        # Fail-open contract: if the bucketed path raises, the public entry still
        # returns the correct all-pairs result rather than propagating the error.
        matters = [
            _matter("m1", counterparty="Acme", created_at="2026-01-01", exact="SAME", simhash=1),
            _matter("m2", counterparty="Acme", created_at="2026-02-01", exact="SAME", simhash=1),
        ]
        original = corpus_index._stamp_duplicate_document_bucketed

        def _boom(_matters):
            raise RuntimeError("bucketing exploded")

        corpus_index._stamp_duplicate_document_bucketed = _boom
        try:
            stamped = copy.deepcopy(matters)
            count = corpus_index._stamp_duplicate_document(stamped)
        finally:
            corpus_index._stamp_duplicate_document_bucketed = original
        self.assertEqual(count, 2)
        signals = _dup_signals(stamped)
        self.assertEqual(signals["m1"]["matched_matter_id"], "m2")
        self.assertEqual(signals["m2"]["matched_matter_id"], "m1")


# --- 2. app-state build cache: hit on unchanged, invalidate on change ------
class _CountingRepository:
    """Wraps InMemoryMatterRepository and counts list_matters calls per owner.

    Lets a cache test assert the app-state build was actually skipped (no fresh
    build work) on a warm hit, beyond just checking the payload is equal.
    """

    def __init__(self, inner: InMemoryMatterRepository):
        self._inner = inner
        self.list_calls = 0

    def list_matters(self, owner_user_id: str = ""):
        self.list_calls += 1
        return self._inner.list_matters(owner_user_id=owner_user_id)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _seed(repo, *, owner, title):
    return repo.create_matter(
        source_filename=f"{title}.docx",
        document_bytes=b"PK\x03\x04 fake docx",
        extracted_text=(
            "This mutual non disclosure agreement is entered into between the parties "
            "for the protection of confidential information exchanged in discussions."
        ),
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


class AppStateBuildCacheTests(unittest.TestCase):
    def setUp(self):
        corpus_index.invalidate_cache()
        self.inner = InMemoryMatterRepository()
        self.repo = _CountingRepository(self.inner)

    def tearDown(self):
        corpus_index.invalidate_cache()

    def _build(self):
        return corpus_index._build_app_state_matters(self.repo, "owner-a")

    def _titles(self, built):
        return sorted(str(m["title"]) for m in built.values())

    def test_unchanged_store_serves_cache_without_rebuilding(self):
        _seed(self.inner, owner="owner-a", title="Alpha NDA")

        first = self._build()
        # Cold build: the content fingerprint is persisted lazily on the first build
        # (changing matter content), so the records-fingerprint settles after one more
        # rebuild. This second build settles it; the third must then be a warm hit.
        self._build()
        calls_before_warm = self.repo.list_calls
        third = self._build()

        # Once warm, a hit still reads list_matters once (to fingerprint) but does NOT
        # increment beyond that single read per call, and returns the same payload.
        self.assertEqual(self.repo.list_calls, calls_before_warm + 1)
        self.assertEqual(self._titles(first), ["Alpha NDA"])
        self.assertEqual(self._titles(third), ["Alpha NDA"])

        # A hit hands back an INDEPENDENT copy: mutating it must not poison the cache.
        any_id = next(iter(third))
        third[any_id]["title"] = "MUTATED IN CALLER"
        fourth = self._build()
        self.assertEqual(self._titles(fourth), ["Alpha NDA"])

    def test_cache_serves_identical_payload_when_warm(self):
        _seed(self.inner, owner="owner-a", title="Warm NDA")
        # Settle the lazy-fingerprint churn, then two consecutive warm reads must be
        # byte-identical.
        self._build()
        self._build()
        a = self._build()
        b = self._build()
        self.assertEqual(a, b)

    def test_store_change_invalidates_cache(self):
        _seed(self.inner, owner="owner-a", title="Only NDA")
        self._build()
        self._build()
        warm = self._build()
        self.assertEqual(self._titles(warm), ["Only NDA"])

        # Adding a matter changes the records-fingerprint -> the next build reflects it
        # (cache miss), proving the cache is not stale.
        _seed(self.inner, owner="owner-a", title="Second NDA")
        after_add = self._build()
        self.assertEqual(self._titles(after_add), ["Only NDA", "Second NDA"])

    def test_field_update_invalidates_cache(self):
        matter = _seed(self.inner, owner="owner-a", title="Editable NDA")
        self._build()
        warm = self._build()  # settle + warm
        warm_status = next(iter(warm.values()))["status"]

        # Mutate a whitelisted field that feeds the built payload (board_column drives
        # the corpus 'status' axis). The records-fingerprint hashes matter content, so
        # this change invalidates the cache and the rebuild reflects the new column --
        # proving a real field write is never served stale.
        self.inner.update_matter_fields(
            matter["id"], {"board_column": "signed_closed"}, owner_user_id="owner-a"
        )
        rebuilt = self._build()
        rebuilt_status = next(iter(rebuilt.values()))["status"]
        self.assertNotEqual(rebuilt_status, warm_status)
        self.assertEqual(self._titles(rebuilt), ["Editable NDA"])

    def test_explicit_invalidate_forces_rebuild(self):
        _seed(self.inner, owner="owner-a", title="Cached NDA")
        self._build()
        self._build()
        self._build()  # warm

        corpus_index.invalidate_cache("owner-a")
        calls_before = self.repo.list_calls
        rebuilt = self._build()
        # A rebuild reads list_matters (>=1) and reproduces the payload; the point is
        # the invalidate cleared the entry so we did not serve a dropped cache.
        self.assertGreater(self.repo.list_calls, calls_before)
        self.assertEqual(self._titles(rebuilt), ["Cached NDA"])

    def test_owners_do_not_share_cache(self):
        _seed(self.inner, owner="owner-a", title="A NDA")
        _seed(self.inner, owner="owner-b", title="B NDA")

        built_a = self._build()
        built_b = corpus_index._build_app_state_matters(self.repo, "owner-b")
        self.assertEqual(self._titles(built_a), ["A NDA"])
        self.assertEqual(self._titles(built_b), ["B NDA"])

    def test_build_corpus_stays_correct_across_repeated_calls(self):
        # End-to-end through the public seam: the cache must not change what
        # build_corpus returns across repeated calls on an unchanged store, and must
        # reflect a change on the next call.
        _seed(self.inner, owner="owner-a", title="Public NDA")
        p1 = corpus_index.build_corpus(self.repo, "owner-a", "")
        p2 = corpus_index.build_corpus(self.repo, "owner-a", "")
        titles1 = {m["title"] for g in p1["groups"] for m in g["matters"]}
        titles2 = {m["title"] for g in p2["groups"] for m in g["matters"]}
        self.assertEqual(titles1, {"Public NDA"})
        self.assertEqual(titles1, titles2)

        _seed(self.inner, owner="owner-a", title="Added NDA")
        p3 = corpus_index.build_corpus(self.repo, "owner-a", "")
        titles3 = {m["title"] for g in p3["groups"] for m in g["matters"]}
        self.assertEqual(titles3, {"Public NDA", "Added NDA"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
