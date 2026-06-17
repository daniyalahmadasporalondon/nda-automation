"""Unit tests for the corpus index builder (``corpus_index.build_corpus``).

These run without HTTP and without a live Drive: an ``InMemoryMatterRepository``
supplies app-state and a stateful ``FakeDriveService`` (modelling the app-owned
``NDAs`` folder tree + ``matter_summary.json`` files) is injected as
``drive_service=``. The fake interprets just enough of the Drive ``files().list``
``q=`` grammar (name=, parent-in-parents, folder vs non-folder) that the four
``drive_integration`` listing helpers use.
"""

from __future__ import annotations

import json
import re
import unittest

from nda_automation import artifact_registry, corpus_index, drive_integration, workflow
from nda_automation.matter_repository import InMemoryMatterRepository

FOLDER_MIME = "application/vnd.google-apps.folder"


# --- fake Drive service ----------------------------------------------------
class _FakeFilesRequest:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _FakeFiles:
    def __init__(self, service):
        self._service = service

    def list(self, *, q="", fields="", pageSize=100, spaces="drive", pageToken=""):
        self._service.list_calls += 1
        return _FakeFilesRequest(self._service.run_list(q))

    def get_media(self, *, fileId=""):
        self._service.download_calls += 1
        return _FakeFilesRequest(self._service.file_bytes.get(fileId, b""))


class FakeDriveService:
    """A minimal stateful Google Drive v3 ``service`` double.

    Nodes are ``{"id","name","parent","mime"}``; non-folder files also carry bytes
    in ``file_bytes``. Only the query shape the four listing helpers emit is
    interpreted.
    """

    def __init__(self):
        self.nodes: list[dict] = []
        self.file_bytes: dict[str, bytes] = {}
        self.list_calls = 0
        self.download_calls = 0
        self._counter = 0

    def files(self):
        return _FakeFiles(self)

    # --- builder helpers ---
    def _next_id(self, prefix):
        self._counter += 1
        return f"{prefix}_{self._counter}"

    def add_folder(self, name, parent=""):
        node_id = self._next_id("folder")
        self.nodes.append({"id": node_id, "name": name, "parent": parent, "mime": FOLDER_MIME})
        return node_id

    def add_file(self, name, parent, content=b"", mime="application/json"):
        node_id = self._next_id("file")
        self.nodes.append({"id": node_id, "name": name, "parent": parent, "mime": mime})
        self.file_bytes[node_id] = content
        return node_id

    def add_summary(self, parent, summary):
        return self.add_file(
            drive_integration.MATTER_SUMMARY_FILENAME,
            parent,
            content=json.dumps(summary).encode("utf-8"),
        )

    # --- query execution ---
    def run_list(self, q):
        name = _extract(q, r"name='((?:[^'\\]|\\.)*)'")
        parent = _extract(q, r"'((?:[^'\\]|\\.)*)' in parents")
        want_folder = "mimeType='" in q and "mimeType!='" not in q
        want_non_folder = "mimeType!='" in q
        if name is not None:
            name = name.replace("\\'", "'").replace("\\\\", "\\")
        if parent is not None:
            parent = parent.replace("\\'", "'").replace("\\\\", "\\")

        matches = []
        for node in self.nodes:
            if name is not None and node["name"] != name:
                continue
            if parent is not None and node["parent"] != parent:
                continue
            is_folder = node["mime"] == FOLDER_MIME
            if want_folder and not is_folder:
                continue
            if want_non_folder and is_folder:
                continue
            matches.append({"id": node["id"], "name": node["name"]})
        return {"files": matches, "nextPageToken": ""}


def _extract(q, pattern):
    match = re.search(pattern, q)
    return match.group(1) if match else None


# --- repository seeding helpers -------------------------------------------
def _seed_matter(repo, *, owner, title="NDA", subject="", board_column="in_review", created_at=""):
    matter = repo.create_matter(
        source_filename=f"{title}.docx",
        document_bytes=b"PK\x03\x04 fake docx",
        extracted_text="This Agreement is mutual.",
        review_result={"clauses": [{"id": "mutuality", "decision": "pass"}]},
        triage={"triage_status": "review"},
        source_type="manual_upload",
        board_column=board_column,
        intake_metadata={"subject": subject} if subject else None,
        owner_user_id=owner,
    )
    return matter


def _register_original_artifact(repo, matter, owner):
    """Add an 'original' artifact whose bytes reuse the matter's source doc."""
    stored_filename = str(matter.get("stored_filename") or "")
    artifact, artifacts_list, current_id = artifact_registry.register_artifact(
        matter,
        source="upload",
        actor=artifact_registry.ACTOR_COUNTERPARTY,
        role=artifact_registry.ROLE_ORIGINAL,
        stored_filename=stored_filename,
    )
    repo.update_matter_artifacts(
        matter["id"], artifacts_list, current_id, owner_user_id=owner
    )
    return artifact


def _summary_for(matter_id, *, counterparty, created_at="2026-05-01T09:00:00Z", artifacts=None, workflow_state=None):
    return {
        "matter_id": matter_id,
        "counterparty": counterparty,
        "created_at": created_at,
        "gmail_thread_id": "",
        "workflow_state": workflow_state or {},
        "matter_folder_url": "",
        "synced_at": "2026-06-01T00:00:00Z",
        "artifacts": artifacts or [],
    }


def _build_drive_tree(fake, *, counterparty, summary, matter_folder_name="2026-05-01 · NDA · ref"):
    """Build NDAs/<counterparty>/<matter folder>/metadata/matter_summary.json."""
    root = _find_or_make(fake, drive_integration.DEFAULT_ROOT_FOLDER_NAME, "")
    cp = _find_or_make(fake, counterparty, root)
    matter_folder = fake.add_folder(matter_folder_name, cp)
    metadata = fake.add_folder(drive_integration.METADATA_FOLDER_NAME, matter_folder)
    fake.add_summary(metadata, summary)
    return matter_folder


def _find_or_make(fake, name, parent):
    for node in fake.nodes:
        if node["name"] == name and node["parent"] == parent and node["mime"] == FOLDER_MIME:
            return node["id"]
    return fake.add_folder(name, parent)


class CorpusIndexTests(unittest.TestCase):
    def setUp(self):
        corpus_index.invalidate_cache()
        self.repo = InMemoryMatterRepository()

    def tearDown(self):
        corpus_index.invalidate_cache()

    # 1. Owner-scoping at the builder level.
    def test_owner_scoping_only_returns_owners_matters(self):
        _seed_matter(self.repo, owner="owner-a", title="Alpha NDA")
        _seed_matter(self.repo, owner="owner-b", title="Bravo NDA")

        payload = corpus_index.build_corpus(self.repo, "owner-a", "")

        titles = [
            matter["title"]
            for group in payload["groups"]
            for matter in group["matters"]
        ]
        self.assertEqual(titles, ["Alpha NDA"])
        self.assertEqual(payload["matter_count"], 1)
        # Drive not connected (no token, no injected service) => graceful fallback.
        self.assertFalse(payload["drive"]["reconciled"])
        self.assertEqual(payload["drive"]["reason"], corpus_index.REASON_NOT_CONNECTED)

    # 2. Reconciliation incl. a Drive-only matter.
    def test_reconciles_app_matter_and_surfaces_drive_only_matter(self):
        matter = _seed_matter(self.repo, owner="owner-a", title="Acme Mutual NDA", subject="Acme Mutual NDA")
        artifact = _register_original_artifact(self.repo, matter, "owner-a")

        fake = FakeDriveService()
        # X — the in-both matter (its summary carries the app matter_id).
        x_summary = _summary_for(
            matter["id"],
            counterparty="Acme Corp",
            artifacts=[
                {
                    "artifact_id": artifact.id,
                    "sequence": 1,
                    "role": "original",
                    "actor": "counterparty",
                    "version": 1,
                    "filename": "01_received.docx",
                    "drive_file_url": "https://drive.google.com/file/d/x_art/view",
                    "created_at": "2026-05-01T09:00:00Z",
                }
            ],
        )
        x_folder = _build_drive_tree(fake, counterparty="Acme Corp", summary=x_summary)
        # Y — Drive-only: a matter_id NOT in app-state.
        y_summary = _summary_for(
            "matter_driveonly99",
            counterparty="Globex Inc",
            artifacts=[
                {
                    "artifact_id": "artifact_yyy",
                    "sequence": 1,
                    "role": "signed",
                    "actor": "human",
                    "version": 1,
                    "filename": "08_signed.pdf",
                    "drive_file_url": "https://drive.google.com/file/d/y_art/view",
                    "created_at": "2026-04-01T09:00:00Z",
                }
            ],
        )
        _build_drive_tree(fake, counterparty="Globex Inc", summary=y_summary)

        payload = corpus_index.build_corpus(
            self.repo, "owner-a", "drive-owner", drive_service=fake
        )

        self.assertTrue(payload["drive"]["reconciled"])
        matters = {m["matter_id"]: m for g in payload["groups"] for m in g["matters"]}

        x = matters[matter["id"]]
        self.assertEqual(x["source"], "both")
        self.assertTrue(x["in_app"])
        self.assertEqual(x["open_in_drive_url"], drive_integration.folder_web_url(x_folder))
        self.assertTrue(x["open_matter_url"].startswith("/?tab=corpus&matter="))
        # Drive URL backfilled onto the matching app artifact.
        self.assertEqual(x["artifacts"][0]["drive_file_url"], "https://drive.google.com/file/d/x_art/view")
        self.assertTrue(x["artifacts"][0]["download_url"].startswith("/api/corpus/artifacts/"))

        y = matters["matter_driveonly99"]
        self.assertEqual(y["source"], "drive")
        self.assertFalse(y["in_app"])
        self.assertEqual(y["open_matter_url"], "")
        self.assertNotEqual(y["open_in_drive_url"], "")
        self.assertEqual(y["counterparty"], "Globex Inc")
        self.assertEqual(len(y["artifacts"]), 1)
        self.assertEqual(y["artifacts"][0]["stage_label"], "signed")
        self.assertEqual(y["artifacts"][0]["download_url"], "")
        self.assertEqual(y["artifacts"][0]["drive_file_url"], "https://drive.google.com/file/d/y_art/view")

    # 2b. Regression: non-numeric sequence/version in a hand-edited
    # matter_summary.json must NOT raise out of the Drive pass (it ran outside the
    # _crawl_drive try/except, so a bad value surfaced as an unhandled 500 that
    # broke the whole Corpus tab). The value coerces to the existing default.
    def test_non_numeric_artifact_version_sequence_does_not_raise(self):
        fake = FakeDriveService()
        bad_summary = _summary_for(
            "matter_badmeta01",
            counterparty="Acme Corp",
            artifacts=[
                {
                    "artifact_id": "artifact_bad",
                    "sequence": "v2",  # hand-edited, non-numeric
                    "role": "signed",
                    "actor": "human",
                    "version": "v2",  # hand-edited, non-numeric
                    "filename": "08_signed.pdf",
                    "drive_file_url": "https://drive.google.com/file/d/bad_art/view",
                    "created_at": "2026-04-01T09:00:00Z",
                }
            ],
        )
        _build_drive_tree(fake, counterparty="Acme Corp", summary=bad_summary)

        # Must not raise; the Drive pass stays reconciled.
        payload = corpus_index.build_corpus(
            self.repo, "owner-a", "drive-owner", drive_service=fake
        )

        self.assertTrue(payload["drive"]["reconciled"])
        matters = {m["matter_id"]: m for g in payload["groups"] for m in g["matters"]}
        # The matter still appears, just with coerced defaults.
        self.assertIn("matter_badmeta01", matters)
        artifact = matters["matter_badmeta01"]["artifacts"][0]
        self.assertEqual(artifact["sequence"], 0)
        self.assertEqual(artifact["version"], 1)

    # 3. Duplicate detection.
    def test_duplicate_detection_flags_repeated_matter_id(self):
        matter = _seed_matter(self.repo, owner="owner-a", title="Dup NDA", subject="Dup NDA")
        fake = FakeDriveService()
        summary = _summary_for(matter["id"], counterparty="Acme Corp")
        first_folder = _build_drive_tree(
            fake, counterparty="Acme Corp", summary=summary, matter_folder_name="2026-05-01 · Dup NDA · aaa"
        )
        second_folder = _build_drive_tree(
            fake, counterparty="Acme Corp", summary=summary, matter_folder_name="2026-05-01 · Dup NDA · bbb"
        )

        payload = corpus_index.build_corpus(self.repo, "owner-a", "drive-owner", drive_service=fake)
        matters = [m for g in payload["groups"] for m in g["matters"]]
        # Still renders once.
        self.assertEqual(len(matters), 1)
        dup = matters[0]
        self.assertTrue(dup["duplicate"])
        # First folder kept as canonical; the second listed as a duplicate.
        self.assertEqual(dup["open_in_drive_url"], drive_integration.folder_web_url(first_folder))
        self.assertEqual(dup["duplicate_folder_urls"], [drive_integration.folder_web_url(second_folder)])

    def test_no_duplicate_flag_when_matter_ids_unique(self):
        m1 = _seed_matter(self.repo, owner="owner-a", title="One NDA", subject="One NDA")
        m2 = _seed_matter(self.repo, owner="owner-a", title="Two NDA", subject="Two NDA")
        fake = FakeDriveService()
        _build_drive_tree(fake, counterparty="Acme Corp", summary=_summary_for(m1["id"], counterparty="Acme Corp"))
        _build_drive_tree(fake, counterparty="Acme Corp", summary=_summary_for(m2["id"], counterparty="Acme Corp"))

        payload = corpus_index.build_corpus(self.repo, "owner-a", "drive-owner", drive_service=fake)
        for group in payload["groups"]:
            for matter in group["matters"]:
                self.assertFalse(matter["duplicate"])
                self.assertEqual(matter["duplicate_folder_urls"], [])

    # 4. Empty corpus.
    def test_empty_corpus_returns_empty_wrapper(self):
        fake = FakeDriveService()  # no NDAs root at all
        payload = corpus_index.build_corpus(self.repo, "owner-a", "drive-owner", drive_service=fake)
        self.assertEqual(payload["groups"], [])
        self.assertEqual(payload["matter_count"], 0)
        self.assertEqual(payload["counterparty_count"], 0)
        # An injected service means Drive is "connected"; the empty crawl reconciles.
        self.assertTrue(payload["drive"]["reconciled"])

    # 5. Drive-off graceful fallback.
    def test_drive_not_connected_falls_back_to_app_state(self):
        _seed_matter(self.repo, owner="owner-a", title="Solo NDA")
        # No injected service and drive_connected() is False for an empty owner.
        payload = corpus_index.build_corpus(self.repo, "owner-a", "")
        self.assertEqual(payload["matter_count"], 1)
        self.assertFalse(payload["drive"]["reconciled"])
        self.assertEqual(payload["drive"]["reason"], corpus_index.REASON_NOT_CONNECTED)
        self.assertFalse(payload["drive"]["connected"])

    def test_drive_error_falls_back_with_drive_error_reason(self):
        _seed_matter(self.repo, owner="owner-a", title="Solo NDA")
        fake = _RaisingDriveService(drive_integration.DriveIntegrationError("boom"))
        payload = corpus_index.build_corpus(self.repo, "owner-a", "drive-owner", drive_service=fake)
        self.assertEqual(payload["matter_count"], 1)
        self.assertFalse(payload["drive"]["reconciled"])
        self.assertEqual(payload["drive"]["reason"], corpus_index.REASON_DRIVE_ERROR)

    def test_drive_rate_limit_falls_back_with_rate_limited_reason(self):
        _seed_matter(self.repo, owner="owner-a", title="Solo NDA")
        # A raw Drive API 429 -> find_folder's _raise_drive_api_error maps it to a
        # DriveRateLimitError, which the builder catches as reason "rate_limited".
        fake = _RaisingDriveService(_FakeHttp429())
        payload = corpus_index.build_corpus(self.repo, "owner-a", "drive-owner", drive_service=fake)
        self.assertEqual(payload["matter_count"], 1)
        self.assertFalse(payload["drive"]["reconciled"])
        self.assertEqual(payload["drive"]["reason"], corpus_index.REASON_RATE_LIMITED)

    def test_slow_drive_crawl_fails_fast_with_drive_timeout(self):
        # A Drive backend so slow each list call burns more than the crawl budget:
        # build_corpus must bail out at the wall-clock deadline (reason
        # 'drive_timeout') instead of grinding through every call, while the
        # app-state matters still come back intact.
        _seed_matter(self.repo, owner="owner-a", title="Solo NDA")
        # Populate a real NDAs tree so that, ABSENT the deadline, the crawl would
        # reconcile a Drive-only matter — proving the deadline is what trips, not an
        # empty tree.
        inner = FakeDriveService()
        _build_drive_tree(
            inner,
            counterparty="Globex",
            summary=_summary_for("drive-only-1", counterparty="Globex"),
        )
        clock = _Clock(start=1000.0)
        # Each Drive call burns half the budget, so the crawl trips a few calls in.
        per_call = corpus_index.CORPUS_DRIVE_CRAWL_DEADLINE_SECONDS / 2.0
        slow = _ClockAdvancingDriveService(inner, clock, advance_per_call=per_call)
        payload = corpus_index.build_corpus(
            self.repo, "owner-a", "drive-owner", drive_service=slow, clock=clock
        )
        # App-state corpus intact + returned.
        self.assertEqual(payload["matter_count"], 1)
        # Drive degraded fast with the new timeout reason; still flagged connected.
        self.assertFalse(payload["drive"]["reconciled"])
        self.assertTrue(payload["drive"]["connected"])
        self.assertEqual(payload["drive"]["reason"], corpus_index.REASON_DRIVE_TIMEOUT)
        # The crawl stopped early — it did NOT walk every call in the tree (which
        # would be 6: root + 2 listings + metadata + summary lookup + download).
        self.assertLess(slow.list_calls, 6)

    def test_drive_timeout_reason_is_distinct_from_drive_error(self):
        self.assertEqual(corpus_index.REASON_DRIVE_TIMEOUT, "drive_timeout")
        self.assertNotEqual(
            corpus_index.REASON_DRIVE_TIMEOUT, corpus_index.REASON_DRIVE_ERROR
        )

    def test_real_wallclock_crawl_returns_within_bound(self):
        # Production path (clock=None): a hung sequential crawl must still return
        # promptly. Each list call sleeps ~half the deadline; the guard trips after
        # a couple of calls, so build_corpus returns well under the Drive client's
        # ~30s socket timeout. We assert it finishes in a small multiple of the
        # deadline, never the 30s hang.
        import time

        _seed_matter(self.repo, owner="owner-a", title="Solo NDA")
        inner = FakeDriveService()
        _build_drive_tree(
            inner,
            counterparty="Globex",
            summary=_summary_for("drive-only-1", counterparty="Globex"),
        )
        deadline = corpus_index.CORPUS_DRIVE_CRAWL_DEADLINE_SECONDS
        slow = _SleepingDriveService(inner, sleep_seconds=deadline * 0.6)
        start = time.monotonic()
        payload = corpus_index.build_corpus(
            self.repo, "owner-a", "drive-owner", drive_service=slow
        )
        elapsed = time.monotonic() - start
        self.assertEqual(payload["matter_count"], 1)
        self.assertEqual(payload["drive"]["reason"], corpus_index.REASON_DRIVE_TIMEOUT)
        # Returned within a small multiple of the deadline (allow one in-flight
        # call to finish), far below the ~30s un-bounded hang.
        self.assertLess(elapsed, deadline + deadline * 0.6 + 2.0)

    # 6. Caching with an injected clock.
    def test_caching_serves_warm_then_refresh_rebuilds(self):
        matter = _seed_matter(self.repo, owner="owner-a", title="Cached NDA", subject="Cached NDA")
        fake = FakeDriveService()
        _build_drive_tree(fake, counterparty="Acme Corp", summary=_summary_for(matter["id"], counterparty="Acme Corp"))

        clock = _Clock(start=1000.0)

        # First call: hits the Drive fake.
        first = corpus_index.build_corpus(self.repo, "owner-a", "drive-owner", drive_service=fake, clock=clock)
        self.assertFalse(first["drive"]["from_cache"])
        calls_after_first = fake.list_calls
        self.assertGreater(calls_after_first, 0)

        # Second call within TTL: served from cache, no new Drive list calls.
        clock.advance(10)
        second = corpus_index.build_corpus(self.repo, "owner-a", "drive-owner", drive_service=fake, clock=clock)
        self.assertTrue(second["drive"]["from_cache"])
        self.assertEqual(fake.list_calls, calls_after_first)

        # App-state pass runs every call: a matter added between calls appears now.
        added = _seed_matter(self.repo, owner="owner-a", title="Fresh NDA", subject="Fresh NDA")
        clock.advance(10)
        third = corpus_index.build_corpus(self.repo, "owner-a", "drive-owner", drive_service=fake, clock=clock)
        self.assertTrue(third["drive"]["from_cache"])
        titles = {m["title"] for g in third["groups"] for m in g["matters"]}
        self.assertIn("Fresh NDA", titles)
        self.assertEqual(fake.list_calls, calls_after_first)

        # ?refresh=1 (force_refresh) rebuilds the Drive pass.
        fourth = corpus_index.build_corpus(
            self.repo, "owner-a", "drive-owner", drive_service=fake, force_refresh=True, clock=clock
        )
        self.assertFalse(fourth["drive"]["from_cache"])
        self.assertGreater(fake.list_calls, calls_after_first)
        calls_after_refresh = fake.list_calls

        # Past TTL rebuilds without an explicit refresh.
        clock.advance(corpus_index.CORPUS_DRIVE_CACHE_TTL_SECONDS + 5)
        fifth = corpus_index.build_corpus(self.repo, "owner-a", "drive-owner", drive_service=fake, clock=clock)
        self.assertFalse(fifth["drive"]["from_cache"])
        self.assertGreater(fake.list_calls, calls_after_refresh)
        # The added matter remains visible (app-state authoritative), and X too.
        self.assertEqual(fifth["matter_count"], 2)
        self.assertTrue(any(m["matter_id"] == added["id"] for g in fifth["groups"] for m in g["matters"]))

    # FIX A — the status field carries the Repository board column (the workflow
    # axis), and the dead 6-phase phase_label is no longer surfaced as a `stage`
    # field on the wire. The exact stored->derived column rollup is workflow's
    # business; here we assert status IS the workflow-derived board column and is
    # one of the 5 valid board columns (never a phantom phase).
    def test_app_matter_status_is_board_column_and_no_phantom_stage(self):
        matter = _seed_matter(self.repo, owner="owner-a", title="Reviewed NDA", subject="Acme", board_column="reviewed")
        expected = workflow.workflow_state(matter).get("board_column")
        payload = corpus_index.build_corpus(self.repo, "owner-a", "")
        surfaced = payload["groups"][0]["matters"][0]
        # Workflow axis = the derived board column.
        self.assertEqual(surfaced["status"], expected)
        valid_columns = {
            "generated",
            "manual_upload",
            "gmail_demo",
            "in_review",
            "reviewed",
            "sent",
        }
        self.assertIn(surfaced["status"], valid_columns)
        # No phantom phase_label field on the wire.
        self.assertNotIn("stage", surfaced)

    def test_drive_only_status_is_empty_when_no_board_column(self):
        # A Drive-only summary with no workflow_state.board_column => status "",
        # which the FE renders as "—" (NOT "On file"; that is a source state).
        fake = FakeDriveService()
        summary = _summary_for("matter_driveonly_nostatus", counterparty="Globex Inc")
        _build_drive_tree(fake, counterparty="Globex Inc", summary=summary)
        payload = corpus_index.build_corpus(self.repo, "owner-a", "drive-owner", drive_service=fake)
        matters = {m["matter_id"]: m for g in payload["groups"] for m in g["matters"]}
        drive_only = matters["matter_driveonly_nostatus"]
        self.assertEqual(drive_only["source"], "drive")
        self.assertEqual(drive_only["status"], "")
        self.assertNotIn("stage", drive_only)

    def test_drive_only_status_reads_board_column_from_summary(self):
        fake = FakeDriveService()
        summary = _summary_for(
            "matter_driveonly_sent",
            counterparty="Globex Inc",
            workflow_state={"board_column": "sent"},
        )
        _build_drive_tree(fake, counterparty="Globex Inc", summary=summary)
        payload = corpus_index.build_corpus(self.repo, "owner-a", "drive-owner", drive_service=fake)
        matters = {m["matter_id"]: m for g in payload["groups"] for m in g["matters"]}
        self.assertEqual(matters["matter_driveonly_sent"]["status"], "sent")

    # Groups sorted by counterparty casefold.
    def test_groups_sorted_by_counterparty_casefold(self):
        _seed_matter(self.repo, owner="owner-a", title="Z NDA", subject="zebra co")
        _seed_matter(self.repo, owner="owner-a", title="A NDA", subject="Apple co")
        payload = corpus_index.build_corpus(self.repo, "owner-a", "")
        names = [group["counterparty"] for group in payload["groups"]]
        self.assertEqual(names, sorted(names, key=lambda n: n.casefold()))


def _only_matter(payload):
    matters = [matter for group in payload["groups"] for matter in group["matters"]]
    assert len(matters) == 1, matters
    return matters[0]


class CorpusFacetTests(unittest.TestCase):
    """Rich-facet derivation on app-state matters + read from a Drive summary."""

    def setUp(self):
        corpus_index.invalidate_cache()
        self.repo = InMemoryMatterRepository()

    def tearDown(self):
        corpus_index.invalidate_cache()

    def test_app_state_matter_carries_derived_facets(self):
        # A generated NDA (manifest governing law) + a term clause with a persisted
        # term_years scalar, fully signed -> all facets resolve from live review data.
        matter = self.repo.create_matter(
            source_filename="Acme NDA.docx",
            document_bytes=b"PK\x03\x04 fake docx",
            extracted_text="This Agreement is mutual.",
            review_result={
                "clauses": [
                    {
                        "id": "governing_law",
                        "decision": "pass",
                        "governing_law_analysis": {
                            "candidate_records": [{"value": "DIFC", "approved": True}]
                        },
                    },
                    {"id": "term_and_survival", "decision": "pass", "term_years": 5.0},
                    {"id": "mutuality", "decision": "pass"},
                ]
            },
            triage={"triage_status": "review"},
            source_type="manual_upload",
            board_column="signed",
            owner_user_id="owner-a",
        )
        self.repo.update_matter_fields(
            matter["id"], {"signed_off_at": "2026-06-01T00:00:00Z"}, owner_user_id="owner-a"
        )
        payload = corpus_index.build_corpus(self.repo, "owner-a", "")
        facets = _only_matter(payload)["facets"]
        self.assertTrue(facets["facets_available"])
        self.assertEqual(facets["governing_law"], "difc")
        self.assertIn("governing_law", facets["has_clauses"])
        self.assertIn("term_and_survival", facets["has_clauses"])
        self.assertEqual(facets["term_years"], 5.0)
        # signed depends on the workflow status; assert the facet is bool-or-null (never
        # an opposite-polarity guess) and carries the workflow enums for status/phase.
        self.assertIn(facets["signed"], (True, False, None))
        self.assertIn("phase", facets)
        self.assertIn("status", facets)
        # The workflow-state failure/gate axes + requirement counts are always present
        # so the FE adapter can reconstruct workflow_state for the human_gate /
        # needs_attention / has_issues filters (parity with the Python matcher).
        self.assertIn("needs_attention", facets)
        self.assertIn("human_gate", facets)
        self.assertIn("requirements_failed", facets)
        self.assertIn("requirements_needs_review", facets)

    def test_app_state_facets_carry_workflow_state_axes_and_counts(self):
        # A matter with a stored AI (ai_first) review result that failed a requirement
        # AND is parked at a human gate surfaces those signals on the corpus facets, so
        # the FE human_gate / has_issues filters can positively match it (the divergence
        # this fix closes). The axes come from the SAME workflow_state the Python twin
        # reads. The review carries an ai_first active-engine marker so the AI-ran gate
        # (which suppresses deterministic-only counts) keeps the real counts here.
        matter = self.repo.create_matter(
            source_filename="Stuck NDA.docx",
            document_bytes=b"PK\x03\x04 fake docx",
            extracted_text="This Agreement is mutual.",
            review_result={
                "requirements_failed": 2,
                "requirements_needs_review": 1,
                "clauses": [{"id": "mutuality", "decision": "check"}],
                "active_review_engine": {"executed_engine": "ai_first"},
            },
            triage={"triage_status": "review"},
            source_type="manual_upload",
            board_column="awaiting_approval",
            owner_user_id="owner-a",
        )
        self.repo.update_matter_fields(
            matter["id"], {"status": "awaiting_approval"}, owner_user_id="owner-a"
        )
        payload = corpus_index.build_corpus(self.repo, "owner-a", "")
        facets = _only_matter(payload)["facets"]
        self.assertEqual(facets["requirements_failed"], 2)
        self.assertEqual(facets["requirements_needs_review"], 1)
        # human_gate / needs_attention are booleans straight off workflow_state.
        self.assertIsInstance(facets["human_gate"], bool)
        self.assertIsInstance(facets["needs_attention"], bool)
        # The corpus matcher (Python twin) now positively matches has_issues over this
        # matter (was silently ignored before the parity fix).
        from nda_automation import dashboard_search_intent as dsi

        corpus_matter = _only_matter(payload)
        self.assertTrue(facets["ai_review_ran"])
        self.assertTrue(
            dsi.corpus_matter_matches_spec(corpus_matter, dsi.validate_filter_spec({"has_issues": True}))
        )

    def test_app_state_clause_facets_light_up_for_ai_reviewed_matter(self):
        # An AI-reviewed matter whose review carried the dynamic non_solicitation +
        # non_compete clause ids surfaces the keyed clause-presence facets the FE rich
        # rail reads (non_solicit / non_compete), each resolving to the "present"
        # sentinel. The engine id `non_solicitation` maps to the FE key `non_solicit`.
        matter = self.repo.create_matter(
            source_filename="Restrictive NDA.docx",
            document_bytes=b"PK\x03\x04 fake docx",
            extracted_text="This Agreement is mutual.",
            review_result={
                "active_review_engine": {"executed_engine": "ai_first"},
                "clauses": [
                    {"id": "mutuality", "decision": "pass"},
                    {"id": "non_solicitation", "decision": "review"},
                    {"id": "non_compete", "decision": "check"},
                ],
            },
            triage={"triage_status": "review"},
            source_type="manual_upload",
            board_column="in_review",
            owner_user_id="owner-a",
        )
        self.assertTrue(matter["id"])
        payload = corpus_index.build_corpus(self.repo, "owner-a", "")
        corpus_matter = _only_matter(payload)
        facets = corpus_matter["facets"]
        # has_clauses still carries the raw engine ids...
        self.assertIn("non_solicitation", facets["has_clauses"])
        self.assertIn("non_compete", facets["has_clauses"])
        # ...and the keyed clause-presence facets resolve to the "present" sentinel.
        self.assertEqual(facets["non_solicit"], "present")
        self.assertEqual(facets["non_compete"], "present")

        # count == filtered parity: the FE option count equals the matters the facet
        # filter keeps (this single matter matches both).
        flat = corpus_index.flatten_corpus(payload)
        ns_hits = [m for m in flat if (m.get("facets") or {}).get("non_solicit") == "present"]
        nc_hits = [m for m in flat if (m.get("facets") or {}).get("non_compete") == "present"]
        self.assertEqual(len(ns_hits), 1)
        self.assertEqual(len(nc_hits), 1)

    def test_app_state_clause_facets_none_when_clause_absent(self):
        # A matter whose review did NOT carry these clause ids (deterministic-only, or
        # simply no restrictive clause) leaves the keyed facets None -- the FE treats
        # None as absent so the facet group never falsely matches the matter.
        _seed_matter(self.repo, owner="owner-a", title="Plain NDA")
        payload = corpus_index.build_corpus(self.repo, "owner-a", "")
        facets = _only_matter(payload)["facets"]
        self.assertIsNone(facets["non_solicit"])
        self.assertIsNone(facets["non_compete"])

    def test_deterministic_only_review_does_not_surface_issue_counts(self):
        # A matter whose stored review_result was NOT produced by the AI (ai_first)
        # engine -- e.g. outbound generation, which pins the deterministic engine and
        # defers AI to on-demand -- carries deterministic requirement counts that must
        # NOT leak into the corpus "has issues" search. The AI-ran gate zeroes the
        # counts and drops ai_review_ran, so the matter never matches has_issues.
        matter = self.repo.create_matter(
            source_filename="Generated NDA.docx",
            document_bytes=b"PK\x03\x04 fake docx",
            extracted_text="This Agreement is mutual.",
            review_result={
                # Non-zero deterministic counts, but no ai_first active-engine marker.
                "requirements_failed": 3,
                "requirements_needs_review": 2,
                "clauses": [{"id": "mutuality", "decision": "check"}],
                "active_review_engine": {"executed_engine": "deterministic"},
            },
            triage={"triage_status": "review"},
            source_type="generated",
            board_column="awaiting_approval",
            owner_user_id="owner-a",
        )
        self.repo.update_matter_fields(
            matter["id"], {"status": "awaiting_approval"}, owner_user_id="owner-a"
        )
        payload = corpus_index.build_corpus(self.repo, "owner-a", "")
        facets = _only_matter(payload)["facets"]
        # Deterministic counts are suppressed (gated on the AI-ran signal).
        self.assertEqual(facets["requirements_failed"], 0)
        self.assertEqual(facets["requirements_needs_review"], 0)
        self.assertFalse(facets["ai_review_ran"])
        from nda_automation import dashboard_search_intent as dsi

        corpus_matter = _only_matter(payload)
        self.assertFalse(
            dsi.corpus_matter_matches_spec(corpus_matter, dsi.validate_filter_spec({"has_issues": True}))
        )

    def test_stale_facets_with_counts_but_no_ai_ran_do_not_match_has_issues(self):
        # Belt-and-suspenders read gate: even if a STALE facet block (persisted before
        # this fix) still carries non-zero deterministic counts WITHOUT ai_review_ran,
        # the consumer must not count it as "has issues".
        from nda_automation import dashboard_search_intent as dsi

        stale = {
            "matter_id": "m-stale",
            "facets": {
                "requirements_failed": 4,
                "requirements_needs_review": 1,
                "schema_version": 1,
                "facets_available": True,
                # ai_review_ran absent -> treated as False (the stale-facet shape).
            },
        }
        self.assertFalse(
            dsi.corpus_matter_matches_spec(stale, dsi.validate_filter_spec({"has_issues": True}))
        )

    def test_app_state_matter_without_review_degrades_per_facet_but_available(self):
        # An intake-only matter (no governing law / term) still has facets_available
        # true (it IS app-state) with empty per-facet values.
        _seed_matter(self.repo, owner="owner-a", title="Plain NDA")
        payload = corpus_index.build_corpus(self.repo, "owner-a", "")
        facets = _only_matter(payload)["facets"]
        self.assertTrue(facets["facets_available"])
        self.assertEqual(facets["governing_law"], "")
        self.assertIsNone(facets["term_years"])

    def test_drive_only_matter_reads_facets_from_enriched_summary(self):
        fake = FakeDriveService()
        summary = _summary_for("drive-only-1", counterparty="DriveCo")
        summary["facets"] = {
            "governing_law": "india",
            "signed": True,
            "has_clauses": ["mutuality", "governing_law", "non_solicitation"],
            "term_years": 3,
            "schema_version": 1,
        }
        summary["workflow_state"] = {"phase": "executed", "status": "fully_signed"}
        _build_drive_tree(fake, counterparty="DriveCo", summary=summary)

        payload = corpus_index.build_corpus(self.repo, "owner-a", "owner-a", drive_service=fake)
        facets = _only_matter(payload)["facets"]
        self.assertTrue(facets["facets_available"])
        self.assertEqual(facets["governing_law"], "india")
        self.assertIs(facets["signed"], True)
        self.assertEqual(facets["has_clauses"], ["mutuality", "governing_law", "non_solicitation"])
        self.assertEqual(facets["term_years"], 3.0)
        self.assertEqual(facets["phase"], "executed")
        self.assertEqual(facets["status"], "fully_signed")
        # Drive pass also derives the keyed clause-presence facets from has_clauses:
        # non_solicitation present -> non_solicit "present"; non_compete absent -> None.
        self.assertEqual(facets["non_solicit"], "present")
        self.assertIsNone(facets["non_compete"])

    def test_legacy_drive_summary_without_facets_degrades_unavailable(self):
        fake = FakeDriveService()
        # A legacy summary written before the facets enrichment (no `facets` block).
        summary = _summary_for("legacy-1", counterparty="LegacyCo")
        _build_drive_tree(fake, counterparty="LegacyCo", summary=summary)

        payload = corpus_index.build_corpus(self.repo, "owner-a", "owner-a", drive_service=fake)
        facets = _only_matter(payload)["facets"]
        self.assertFalse(facets["facets_available"])
        self.assertEqual(facets["governing_law"], "")
        self.assertIsNone(facets["signed"])
        self.assertEqual(facets["has_clauses"], [])
        # The keyed clause-presence facets default None on a degraded block.
        self.assertIsNone(facets["non_solicit"])
        self.assertIsNone(facets["non_compete"])

    def test_unknown_facet_never_positively_matches_a_filter(self):
        # The graceful-degradation linchpin, asserted via the corpus matcher: a legacy
        # Drive matter (facets_available=false) is never a positive match for any facet
        # filter, either polarity.
        from nda_automation import dashboard_search_intent as dsi

        fake = FakeDriveService()
        summary = _summary_for("legacy-2", counterparty="LegacyCo")
        _build_drive_tree(fake, counterparty="LegacyCo", summary=summary)
        payload = corpus_index.build_corpus(self.repo, "owner-a", "owner-a", drive_service=fake)
        matter = _only_matter(payload)
        for spec_in in ({"signed": True}, {"signed": False}, {"governing_law": "difc"}, {"has_clause": "mutuality"}):
            spec = dsi.validate_filter_spec(spec_in)
            self.assertFalse(
                dsi.corpus_matter_matches_spec(matter, spec),
                f"legacy matter should not match {spec_in}",
            )


class CorpusMasterFilterFacetTests(unittest.TestCase):
    """The 6 master-filter facets (mutuality / term_band / restraint_types /
    review_outcome / clauses_present / origin): derivation, count==filtered parity,
    Drive-summary round-trip, and null/missing-data handling."""

    def setUp(self):
        corpus_index.invalidate_cache()
        self.repo = InMemoryMatterRepository()

    def tearDown(self):
        corpus_index.invalidate_cache()

    def _facets_for_review(self, review_result, *, source_type="manual_upload", **extra):
        # A throwaway single-matter repo per call so a test method can derive several
        # independent matters without them accumulating in one corpus.
        repo = InMemoryMatterRepository()
        matter = repo.create_matter(
            source_filename="MF NDA.docx",
            document_bytes=b"PK\x03\x04 fake docx",
            extracted_text="This Agreement is mutual.",
            review_result=review_result,
            triage={"triage_status": "review"},
            source_type=source_type,
            board_column="in_review",
            owner_user_id="owner-a",
            **extra,
        )
        self.assertTrue(matter["id"])
        corpus_index.invalidate_cache()
        payload = corpus_index.build_corpus(repo, "owner-a", "")
        return _only_matter(payload)["facets"]

    # --- mutuality ---------------------------------------------------------
    def test_mutuality_mutual_from_strong_reciprocal_obligation(self):
        facets = self._facets_for_review(
            {
                "active_review_engine": {"executed_engine": "ai_first"},
                "clauses": [
                    {
                        "id": "mutuality",
                        "decision": "pass",
                        "mutuality_analysis": {
                            "strong_mutuality_paragraph_ids": ["p1"],
                            "one_way_paragraph_ids": [],
                        },
                    }
                ],
            }
        )
        self.assertEqual(facets["mutuality"], "mutual")

    def test_mutuality_one_way_from_one_way_paragraphs(self):
        # A one-way signal wins even when the clause decision is review/check.
        facets = self._facets_for_review(
            {
                "active_review_engine": {"executed_engine": "ai_first"},
                "clauses": [
                    {
                        "id": "mutuality",
                        "decision": "review",
                        "mutuality_analysis": {
                            "strong_mutuality_paragraph_ids": [],
                            "one_way_paragraph_ids": ["p5"],
                        },
                    }
                ],
            }
        )
        self.assertEqual(facets["mutuality"], "one_way")

    def test_mutuality_none_when_only_weak_or_absent(self):
        # A weak label-only signal is not a confident polarity -> None (never guessed).
        facets = self._facets_for_review(
            {
                "active_review_engine": {"executed_engine": "ai_first"},
                "clauses": [
                    {
                        "id": "mutuality",
                        "decision": "review",
                        "mutuality_analysis": {"weak_mutuality_paragraph_ids": ["p2"]},
                    }
                ],
            }
        )
        self.assertIsNone(facets["mutuality"])
        # No mutuality clause at all -> None.
        facets2 = self._facets_for_review(
            {"clauses": [{"id": "governing_law", "decision": "pass"}]}
        )
        self.assertIsNone(facets2["mutuality"])

    # --- term_band ---------------------------------------------------------
    def test_term_band_buckets_from_term_years(self):
        # Boundaries: <=2y, 3-5y (>2 and <=5), >5y.
        for years, band in ((1.0, "<=2y"), (2.0, "<=2y"), (3.0, "3-5y"), (5.0, "3-5y"), (7.0, ">5y")):
            facets = self._facets_for_review(
                {"clauses": [{"id": "term_and_survival", "decision": "pass", "term_years": years}]}
            )
            self.assertEqual(facets["term_band"], band, f"{years} -> {band}")
            self.assertEqual(facets["term_years"], years)

    def test_term_band_none_when_term_unknown(self):
        facets = self._facets_for_review({"clauses": [{"id": "mutuality", "decision": "pass"}]})
        self.assertIsNone(facets["term_band"])
        self.assertIsNone(facets["term_years"])

    # --- restraint_types ---------------------------------------------------
    def test_restraint_types_tagged_from_non_circumvention_flagged_text(self):
        # The EXISTING prohibited_positions regexes run against the non_circumvention
        # finding's flagged text; the families found are returned in a stable order.
        facets = self._facets_for_review(
            {
                "active_review_engine": {"executed_engine": "ai_first"},
                "clauses": [
                    {
                        "id": "non_circumvention",
                        "decision": "fail",
                        "matched_text": "a non-compete and non-solicit and non-circumvent provision",
                        "evidence": [],
                    }
                ],
            }
        )
        self.assertEqual(facets["restraint_types"], ["non_compete", "non_solicit", "non_circumvention"])

    def test_restraint_types_reads_evidence_paragraphs(self):
        # Flagged text also comes from the evidence list (string or dict paragraphs).
        facets = self._facets_for_review(
            {
                "active_review_engine": {"executed_engine": "ai_first"},
                "clauses": [
                    {
                        "id": "non_circumvention",
                        "decision": "review",
                        "matched_text": "",
                        "evidence": [{"text": "the parties shall not circumvent each other"}],
                    }
                ],
            }
        )
        self.assertEqual(facets["restraint_types"], ["non_circumvention"])

    def test_restraint_types_empty_when_clause_absent_or_no_match(self):
        # No non_circumvention clause -> [].
        facets = self._facets_for_review({"clauses": [{"id": "mutuality", "decision": "pass"}]})
        self.assertEqual(facets["restraint_types"], [])
        # Clause present but its flagged text trips no restraint family -> [].
        facets2 = self._facets_for_review(
            {
                "active_review_engine": {"executed_engine": "ai_first"},
                "clauses": [
                    {
                        "id": "non_circumvention",
                        "decision": "pass",
                        "matched_text": "the parties may freely deal with anyone",
                        "evidence": [],
                    }
                ],
            }
        )
        self.assertEqual(facets2["restraint_types"], [])

    # --- review_outcome ----------------------------------------------------
    def test_review_outcome_has_fail_needs_review_clean(self):
        has_fail = self._facets_for_review(
            {"clauses": [{"id": "a", "decision": "fail"}, {"id": "b", "decision": "pass"}]}
        )
        self.assertEqual(has_fail["review_outcome"], "has_fail")
        needs_review = self._facets_for_review(
            {"clauses": [{"id": "a", "decision": "review"}, {"id": "b", "decision": "pass"}]}
        )
        self.assertEqual(needs_review["review_outcome"], "needs_review")
        clean = self._facets_for_review({"clauses": [{"id": "a", "decision": "pass"}]})
        self.assertEqual(clean["review_outcome"], "clean")

    def test_review_outcome_none_when_unreviewed(self):
        # A review_result with no clause verdicts is unreviewed -> None (never "clean").
        facets = self._facets_for_review({})
        self.assertIsNone(facets["review_outcome"])

    # --- clauses_present ---------------------------------------------------
    def test_clauses_present_mirrors_has_clauses(self):
        facets = self._facets_for_review(
            {
                "active_review_engine": {"executed_engine": "ai_first"},
                "clauses": [
                    {"id": "mutuality", "decision": "pass"},
                    {"id": "governing_law", "decision": "review"},
                ],
            }
        )
        self.assertEqual(sorted(facets["clauses_present"]), sorted(facets["has_clauses"]))
        self.assertIn("mutuality", facets["clauses_present"])
        self.assertIn("governing_law", facets["clauses_present"])

    # --- origin ------------------------------------------------------------
    def test_origin_generated_vs_received_vs_unknown(self):
        gen = self._facets_for_review(
            {"clauses": [{"id": "mutuality", "decision": "pass"}]}, source_type="generated"
        )
        self.assertEqual(gen["origin"], "generated")
        received = self._facets_for_review(
            {"clauses": [{"id": "mutuality", "decision": "pass"}]}, source_type="manual_upload"
        )
        self.assertEqual(received["origin"], "received")
        gmail = self._facets_for_review(
            {"clauses": [{"id": "mutuality", "decision": "pass"}]}, source_type="gmail_inbound"
        )
        self.assertEqual(gmail["origin"], "received")

    def _seed_review(self, review_result, *, source_type, title):
        return self.repo.create_matter(
            source_filename=f"{title}.docx",
            document_bytes=b"PK\x03\x04 fake docx",
            extracted_text="This Agreement is mutual.",
            review_result=review_result,
            triage={"triage_status": "review"},
            source_type=source_type,
            board_column="in_review",
            owner_user_id="owner-a",
        )

    # --- facet counts == filtered parity -----------------------------------
    def test_facet_counts_equal_filtered_matter_counts(self):
        # Three matters with distinct facet values; the top-level facet_counts must
        # equal the number of matters a filter on each value would keep.
        self._seed_review(
            {
                "active_review_engine": {"executed_engine": "ai_first"},
                "clauses": [
                    {"id": "term_and_survival", "decision": "pass", "term_years": 1.0},
                    {"id": "mutuality", "decision": "pass",
                     "mutuality_analysis": {"strong_mutuality_paragraph_ids": ["p1"], "one_way_paragraph_ids": []}},
                    {"id": "non_circumvention", "decision": "fail",
                     "matched_text": "a non-compete provision", "evidence": []},
                ],
            },
            source_type="manual_upload",
            title="MF One",
        )
        self._seed_review(
            {
                "active_review_engine": {"executed_engine": "ai_first"},
                "clauses": [
                    {"id": "term_and_survival", "decision": "pass", "term_years": 4.0},
                    {"id": "mutuality", "decision": "review",
                     "mutuality_analysis": {"strong_mutuality_paragraph_ids": [], "one_way_paragraph_ids": ["p3"]}},
                ],
            },
            source_type="generated",
            title="MF Two",
        )
        self._seed_review(
            {"clauses": [{"id": "term_and_survival", "decision": "pass", "term_years": 9.0}]},
            source_type="gmail_inbound",
            title="MF Three",
        )
        corpus_index.invalidate_cache()
        payload = corpus_index.build_corpus(self.repo, "owner-a", "")
        flat = corpus_index.flatten_corpus(payload)
        counts = payload["facet_counts"]
        # Each emitted count equals the matters that carry that value (parity).
        for key, by_value in counts.items():
            for value, count in by_value.items():
                if key in ("restraint_types", "clauses_present"):
                    hits = [m for m in flat if value in (m["facets"].get(key) or [])]
                else:
                    hits = [m for m in flat if m["facets"].get(key) == value]
                self.assertEqual(count, len(hits), f"{key}={value}: count {count} != filtered {len(hits)}")
        # Spot-check a few expected values are present with the right counts.
        self.assertEqual(counts["term_band"]["<=2y"], 1)
        self.assertEqual(counts["term_band"]["3-5y"], 1)
        self.assertEqual(counts["term_band"][">5y"], 1)
        self.assertEqual(counts["mutuality"]["mutual"], 1)
        self.assertEqual(counts["mutuality"]["one_way"], 1)
        # manual_upload + gmail_inbound both map to "received"; generated maps to "generated".
        self.assertEqual(counts["origin"]["received"], 2)
        self.assertEqual(counts["origin"]["generated"], 1)
        self.assertEqual(counts["restraint_types"]["non_compete"], 1)

    def test_facet_counts_skip_null_and_empty_values(self):
        # The _seed_matter helper's review carries one passing clause (mutuality) with
        # no mutuality_analysis, no term, no non_circ finding -> mutuality/term_band are
        # null and restraint_types is empty, so NONE of those advertise a value in the
        # count block (an unknown/empty facet never advertises a value). The known
        # facets (review_outcome=clean from the passing clause, origin=received from the
        # manual upload, clauses_present=mutuality) DO appear.
        _seed_matter(self.repo, owner="owner-a", title="Plain NDA")
        corpus_index.invalidate_cache()
        payload = corpus_index.build_corpus(self.repo, "owner-a", "")
        counts = payload["facet_counts"]
        self.assertEqual(counts["mutuality"], {})
        self.assertEqual(counts["term_band"], {})
        self.assertEqual(counts["restraint_types"], {})
        self.assertEqual(counts["review_outcome"], {"clean": 1})
        self.assertEqual(counts["origin"], {"received": 1})
        self.assertEqual(counts["clauses_present"], {"mutuality": 1})

    # --- Drive-summary round-trip ------------------------------------------
    def test_drive_only_matter_reads_master_filter_facets_from_summary(self):
        fake = FakeDriveService()
        summary = _summary_for("mf-drive-1", counterparty="DriveCo")
        summary["facets"] = {
            "governing_law": "india",
            "signed": True,
            "has_clauses": ["mutuality", "non_circumvention"],
            "term_years": 4,
            "mutuality": "one_way",
            "term_band": "3-5y",
            "restraint_types": ["non_compete", "non_solicit"],
            "review_outcome": "has_fail",
            "clauses_present": ["mutuality", "non_circumvention"],
            "origin": "received",
            "schema_version": 1,
        }
        _build_drive_tree(fake, counterparty="DriveCo", summary=summary)
        payload = corpus_index.build_corpus(self.repo, "owner-a", "owner-a", drive_service=fake)
        facets = _only_matter(payload)["facets"]
        self.assertTrue(facets["facets_available"])
        self.assertEqual(facets["mutuality"], "one_way")
        # term_band is re-derived from the durable term_years (cannot drift on disk).
        self.assertEqual(facets["term_band"], "3-5y")
        self.assertEqual(facets["restraint_types"], ["non_compete", "non_solicit"])
        self.assertEqual(facets["review_outcome"], "has_fail")
        self.assertEqual(sorted(facets["clauses_present"]), ["mutuality", "non_circumvention"])
        self.assertEqual(facets["origin"], "received")

    def test_legacy_drive_summary_without_master_facets_degrades(self):
        # A summary with a facets block predating the master-filter enrichment leaves
        # the new facets at their null/empty defaults (never a false positive match).
        fake = FakeDriveService()
        summary = _summary_for("mf-legacy-1", counterparty="LegacyCo")
        summary["facets"] = {
            "governing_law": "india",
            "signed": True,
            "has_clauses": ["mutuality"],
            "schema_version": 1,
        }
        _build_drive_tree(fake, counterparty="LegacyCo", summary=summary)
        payload = corpus_index.build_corpus(self.repo, "owner-a", "owner-a", drive_service=fake)
        facets = _only_matter(payload)["facets"]
        self.assertIsNone(facets["mutuality"])
        self.assertIsNone(facets["term_band"])
        self.assertEqual(facets["restraint_types"], [])
        self.assertIsNone(facets["review_outcome"])
        self.assertIsNone(facets["origin"])
        # clauses_present falls back to has_clauses when the durable key is absent.
        self.assertEqual(facets["clauses_present"], ["mutuality"])

    def test_summary_facets_persist_master_filter_block(self):
        # drive_integration._summary_facets (the write side) lands the master-filter
        # facets on disk so a Drive-only matter keeps them after a /tmp wipe.
        matter = {
            "id": "m1",
            "source_type": "generated",
            "review_result": {
                "active_review_engine": {"executed_engine": "ai_first"},
                "clauses": [
                    {"id": "term_and_survival", "decision": "pass", "term_years": 6.0},
                    {"id": "non_circumvention", "decision": "fail",
                     "matched_text": "a non-solicit provision", "evidence": []},
                    {"id": "mutuality", "decision": "pass",
                     "mutuality_analysis": {"strong_mutuality_paragraph_ids": ["p1"], "one_way_paragraph_ids": []}},
                ],
            },
        }
        block = drive_integration._summary_facets(matter, {"status": "fully_signed"})
        self.assertEqual(block["mutuality"], "mutual")
        self.assertEqual(block["term_band"], ">5y")
        self.assertEqual(block["restraint_types"], ["non_solicit"])
        self.assertEqual(block["review_outcome"], "has_fail")
        self.assertEqual(block["origin"], "generated")
        self.assertIn("non_circumvention", block["clauses_present"])

    def test_drive_pass_degrades_on_unexpected_exception_type(self):
        # P1: an UNEXPECTED Drive exception type (not one of the wrapped
        # drive_integration errors) must NOT escape build_corpus and kill /api/corpus.
        # It degrades to an app-state-only corpus (reason=drive_error) so the live
        # matters always survive.
        _seed_matter(self.repo, owner="owner-a", title="Survivor NDA")
        fake = _RaisingDriveService(RuntimeError("raw client/socket error never wrapped"))
        payload = corpus_index.build_corpus(self.repo, "owner-a", "drive-owner", drive_service=fake)
        # App-state matters survive (the corpus did not go down)...
        self.assertEqual(payload["matter_count"], 1)
        self.assertEqual(_only_matter(payload)["title"], "Survivor NDA")
        # ...and the Drive side degraded with reason=drive_error.
        self.assertFalse(payload["drive"]["reconciled"])
        self.assertEqual(payload["drive"]["reason"], corpus_index.REASON_DRIVE_ERROR)


class _RaisingDriveService:
    """A Drive ``service`` double whose first ``files().list`` raises.

    Wired through ``drive_integration.find_folder`` -> ``_raise_drive_api_error``
    for the rate-limit case; the explicit error variants are raised directly so the
    builder's except-arms are exercised.
    """

    def __init__(self, error):
        self._error = error

    def files(self):
        return self

    def list(self, **kwargs):
        raise self._error

    def execute(self):  # pragma: no cover - defensive
        raise self._error


class _ClockAdvancingDriveService:
    """Wraps a populated :class:`FakeDriveService` but advances an injected clock on
    every ``files().list`` execute, modelling a Drive backend so slow each request
    burns wall-clock time. Deterministic (no real sleeping): the crawl's deadline
    guard reads the same injected clock, so the crawl trips ``drive_timeout`` after a
    few calls. The wrapped fake holds a real NDAs tree, so absent the deadline the
    crawl would reconcile — proving the deadline (not an empty tree) is what trips.
    """

    def __init__(self, inner, clock, *, advance_per_call):
        self._inner = inner
        self._clock = clock
        self._advance = float(advance_per_call)
        self.list_calls = 0

    def files(self):
        return _ClockAdvancingFiles(self, self._inner.files())


class _ClockAdvancingFiles:
    def __init__(self, parent, inner_files):
        self._parent = parent
        self._inner_files = inner_files

    def list(self, **kwargs):
        return _ClockAdvancingExec(self._parent, self._inner_files.list(**kwargs))

    def get_media(self, **kwargs):
        return _ClockAdvancingExec(self._parent, self._inner_files.get_media(**kwargs))


class _ClockAdvancingExec:
    def __init__(self, parent, inner_request):
        self._parent = parent
        self._inner_request = inner_request

    def execute(self):
        self._parent.list_calls += 1
        self._parent._clock.advance(self._parent._advance)
        return self._inner_request.execute()


class _SleepingDriveService:
    """Wraps a populated :class:`FakeDriveService` but blocks on real wall-clock time
    for ``sleep_seconds`` on each Drive call, modelling a genuinely slow sequential
    crawl. Used to prove ``build_corpus`` returns within the bound on the production
    wall-clock path (clock=None)."""

    def __init__(self, inner, sleep_seconds):
        self._inner = inner
        self._sleep = float(sleep_seconds)
        self.list_calls = 0

    def files(self):
        return _SleepingFiles(self, self._inner.files())


class _SleepingFiles:
    def __init__(self, parent, inner_files):
        self._parent = parent
        self._inner_files = inner_files

    def list(self, **kwargs):
        return _SleepingExec(self._parent, self._inner_files.list(**kwargs))

    def get_media(self, **kwargs):
        return _SleepingExec(self._parent, self._inner_files.get_media(**kwargs))


class _SleepingExec:
    def __init__(self, parent, inner_request):
        self._parent = parent
        self._inner_request = inner_request

    def execute(self):
        import time as _time

        self._parent.list_calls += 1
        _time.sleep(self._parent._sleep)
        return self._inner_request.execute()


class _FakeResp:
    def __init__(self, status):
        self.status = status


class _FakeHttp429(Exception):
    """Mimics a googleapiclient HttpError with a 429 status + rateLimitExceeded."""

    def __init__(self):
        super().__init__("rate limit exceeded")
        self.resp = _FakeResp(429)
        self.content = json.dumps(
            {"error": {"message": "rate limit", "errors": [{"reason": "rateLimitExceeded"}]}}
        ).encode("utf-8")


class _Clock:
    def __init__(self, start=0.0):
        self._now = float(start)

    def __call__(self):
        return self._now

    def advance(self, seconds):
        self._now += float(seconds)


def _seed_named_matter(repo, *, owner, counterparty, title="NDA"):
    """Seed an app-state matter whose derived counterparty is ``counterparty``.

    Drives the counterparty through the email ``subject`` (the deterministic
    normalize_counterparty path), which for a plain company name is an identity
    transform -- so the corpus groups the matter under exactly ``counterparty``.
    An empty ``counterparty`` leaves the matter with no derivable name -> it lands
    under "Unknown Counterparty". Carries a review_result so the matter reaches the
    approval-gate staleness check.
    """
    return repo.create_matter(
        source_filename=f"{title}.docx",
        document_bytes=b"PK\x03\x04 fake docx",
        extracted_text="This Agreement is mutual.",
        review_result={"clauses": [{"id": "mutuality", "decision": "pass"}]},
        triage={"triage_status": "review"},
        source_type="manual_upload",
        board_column="in_review",
        intake_metadata={"subject": counterparty} if counterparty else None,
        owner_user_id=owner,
    )


class CorpusPerfResolveOnceTests(unittest.TestCase):
    """The active playbook runtime is resolved ONCE per build_corpus, not per matter."""

    def setUp(self):
        corpus_index.invalidate_cache()
        self.repo = InMemoryMatterRepository()

    def tearDown(self):
        corpus_index.invalidate_cache()

    def test_playbook_runtime_resolved_once_per_build(self):
        # Several matters that all carry a review_result, so each one reaches the
        # approval-gate staleness check (workflow_state -> _approval_status ->
        # approval.review_is_stale -> review_result_staleness). Pre-fix this read
        # playbook.json once PER MATTER; post-fix it is resolved once per build.
        for n in range(4):
            _seed_named_matter(self.repo, owner="owner-a", counterparty=f"Entity {n}", title=f"NDA {n}")

        real = corpus_index.playbook_runtime.ensure_active_playbook_runtime
        calls = {"n": 0}

        def counting_resolver(*args, **kwargs):
            calls["n"] += 1
            return real(*args, **kwargs)

        original = corpus_index.playbook_runtime.ensure_active_playbook_runtime
        corpus_index.playbook_runtime.ensure_active_playbook_runtime = counting_resolver
        try:
            payload = corpus_index.build_corpus(self.repo, "owner-a", "")
        finally:
            corpus_index.playbook_runtime.ensure_active_playbook_runtime = original

        # Exactly one playbook resolution for the whole build (the O(matters) read
        # collapsed to O(1)).
        self.assertEqual(calls["n"], 1)
        # And every matter surfaced (the build still ran over all of them).
        self.assertEqual(payload["matter_count"], 4)

    def test_staleness_verdicts_unchanged_by_batching(self):
        # The batched resolver must produce byte-identical workflow verdicts to the
        # unbatched per-matter resolution: status (= board_column) is the same.
        matters = [
            _seed_named_matter(self.repo, owner="owner-a", counterparty=f"Entity {n}", title=f"NDA {n}")
            for n in range(3)
        ]
        baseline = {m["id"]: workflow.workflow_state(m) for m in matters}

        payload = corpus_index.build_corpus(self.repo, "owner-a", "")
        surfaced = {m["matter_id"]: m for g in payload["groups"] for m in g["matters"]}
        for matter_id, expected in baseline.items():
            self.assertEqual(surfaced[matter_id]["status"], expected["board_column"])


class CorpusRepeatEntityTests(unittest.TestCase):
    """repeat_entity fires for a counterparty with >=2 distinct matters, not singletons."""

    def setUp(self):
        corpus_index.invalidate_cache()
        self.repo = InMemoryMatterRepository()

    def tearDown(self):
        corpus_index.invalidate_cache()

    def _facets_by_matter(self, payload):
        return {
            m["matter_id"]: m["facets"]
            for g in payload["groups"]
            for m in g["matters"]
        }

    def test_repeat_entity_fires_for_two_same_counterparty_not_singletons(self):
        # Acme has two distinct matters -> both repeat_entity True.
        a1 = _seed_named_matter(self.repo, owner="owner-a", counterparty="Acme Corp", title="Acme 1")
        a2 = _seed_named_matter(self.repo, owner="owner-a", counterparty="Acme Corp", title="Acme 2")
        # Globex has a single matter -> repeat_entity False.
        g1 = _seed_named_matter(self.repo, owner="owner-a", counterparty="Globex Inc", title="Globex 1")

        payload = corpus_index.build_corpus(self.repo, "owner-a", "")
        facets = self._facets_by_matter(payload)

        self.assertTrue(facets[a1["id"]]["repeat_entity"])
        self.assertTrue(facets[a2["id"]]["repeat_entity"])
        self.assertFalse(facets[g1["id"]]["repeat_entity"])
        # Facet count = number of matters that are repeat entities (Acme's 2).
        self.assertEqual(payload["facet_counts"]["repeat_entity"], 2)

    def test_repeat_entity_normalizes_case_and_whitespace(self):
        # "Acme  Corp" (double space) / "acme corp" normalize to one entity.
        a1 = _seed_named_matter(self.repo, owner="owner-a", counterparty="Acme  Corp", title="Acme 1")
        a2 = _seed_named_matter(self.repo, owner="owner-a", counterparty="acme corp", title="Acme 2")

        payload = corpus_index.build_corpus(self.repo, "owner-a", "")
        facets = self._facets_by_matter(payload)
        self.assertTrue(facets[a1["id"]]["repeat_entity"])
        self.assertTrue(facets[a2["id"]]["repeat_entity"])
        self.assertEqual(payload["facet_counts"]["repeat_entity"], 2)

    def test_unknown_counterparty_never_repeat_entity(self):
        # Two matters whose subject normalizes to empty both group under "Unknown
        # Counterparty" but must NOT be treated as a repeat entity (a bag of
        # unrelated unknowns is not the same counterparty).
        m1 = _seed_named_matter(self.repo, owner="owner-a", counterparty="Fwd:", title="Mystery 1")
        m2 = _seed_named_matter(self.repo, owner="owner-a", counterparty="Re:", title="Mystery 2")

        payload = corpus_index.build_corpus(self.repo, "owner-a", "")
        # Sanity: both landed under the unknown sentinel.
        unknown_group = next(
            g for g in payload["groups"] if g["counterparty"] == artifact_registry.COUNTERPARTY_UNKNOWN
        )
        self.assertEqual(unknown_group["matter_count"], 2)

        facets = self._facets_by_matter(payload)
        self.assertFalse(facets[m1["id"]]["repeat_entity"])
        self.assertFalse(facets[m2["id"]]["repeat_entity"])
        self.assertEqual(payload["facet_counts"]["repeat_entity"], 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
