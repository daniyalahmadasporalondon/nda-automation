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
        # A matter with a stored review result that failed a requirement AND is parked
        # at a human gate surfaces those signals on the corpus facets, so the FE
        # human_gate / has_issues filters can positively match it (the divergence this
        # fix closes). The axes come from the SAME workflow_state the Python twin reads.
        matter = self.repo.create_matter(
            source_filename="Stuck NDA.docx",
            document_bytes=b"PK\x03\x04 fake docx",
            extracted_text="This Agreement is mutual.",
            review_result={
                "requirements_failed": 2,
                "requirements_needs_review": 1,
                "clauses": [{"id": "mutuality", "decision": "check"}],
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
        self.assertTrue(
            dsi.corpus_matter_matches_spec(corpus_matter, dsi.validate_filter_spec({"has_issues": True}))
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
            "has_clauses": ["mutuality", "governing_law"],
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
        self.assertEqual(facets["has_clauses"], ["mutuality", "governing_law"])
        self.assertEqual(facets["term_years"], 3.0)
        self.assertEqual(facets["phase"], "executed")
        self.assertEqual(facets["status"], "fully_signed")

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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
