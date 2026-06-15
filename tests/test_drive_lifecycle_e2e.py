"""End-to-end lifecycle test: a NEGOTIATED matter from received to signed.

This is the integration agent's single end-to-end test. It exercises the WHOLE
Drive-lifecycle feature — the CORE naming grammar plus all three hook modules
(``lifecycle_sent`` / ``lifecycle_counter`` / ``lifecycle_signed``) — by driving
one matter through every stage of a negotiated deal and then asserting that
:func:`drive_integration.sync_matter_folder` files it under the agreed
chronological Drive grammar ``{NN}_{stage}[_v{N}].{ext}``.

The negotiation thread modelled here (one full round-trip plus a re-review and
re-send before signature):

    received          counterparty original NDA arrives        01_received.docx
    ai_redline v1     the AI review output                     02_ai_redline_v1.docx
    legal_review v1   the human-approved doc (approval gate)   03_legal_review_v1.docx
    sent v1           we email our marked-up copy out          04_sent_v1.docx
    counter v1        the counterparty sends a counter back    05_counter_v1.docx
    legal_review v2   we re-review the counter                 06_legal_review_v2.docx
    sent v2           we email the re-reviewed copy out        07_sent_v2.docx
    signed            the executed PDF (terminal, no version)  08_signed.pdf

Nothing here touches the network: the backend is a fully isolated
``InMemoryMatterRepository`` and the Drive client is the stateful in-memory
``FakeDriveV2Service`` from :mod:`tests.test_drive_integration`. The capture
hooks and the registry writes all run against the SAME in-memory repository, and
``sync_matter_folder`` reads the artifact bytes back through that repository — so
the filenames asserted below are the ones the real grammar + real hooks produce.
"""

import unittest

from nda_automation import artifact_service, drive_integration
from nda_automation import lifecycle_counter, lifecycle_sent, lifecycle_signed
from nda_automation.artifact_registry import (
    ACTOR_AI,
    ACTOR_COUNTERPARTY,
    ACTOR_HUMAN,
    ROLE_ORIGINAL,
    ROLE_REDLINE,
    ROLE_REVIEWED,
    SOURCE_GMAIL,
    SOURCE_GENERATED,
)
from nda_automation.matter_repository import InMemoryMatterRepository

from tests.test_drive_integration import FakeDriveV2Service


# Single-tenant (auth-disabled) path: an empty owner_user_id keeps every matter
# in scope, so the captures + registry writes all resolve the same matter.
OWNER = ""


class DriveLifecycleEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.repo = InMemoryMatterRepository()
        # The inbound counterparty NDA seeds the matter (its bytes are the
        # source document). create_matter does not register an artifact, so the
        # "received" artifact is registered explicitly below against the same
        # repository, pointing at these stored bytes.
        self.matter = self.repo.create_matter(
            source_filename="Acme Mutual NDA.docx",
            document_bytes=b"PK\x03\x04 received-counterparty-original",
            extracted_text="MUTUAL NON-DISCLOSURE AGREEMENT ...",
            review_result={},
            triage={},
            source_type="gmail_demo",
            board_column="gmail_demo",
            owner_user_id=OWNER,
        )
        self.matter_id = self.matter["id"]

    def _bytes_reader(self):
        """A ``get_artifact_bytes`` bound to this test's in-memory repository."""

        def get_artifact_bytes(matter_id, artifact_id, *, owner_user_id=""):
            return artifact_service.get_artifact_bytes(
                matter_id,
                artifact_id,
                repository=self.repo,
                owner_user_id=owner_user_id,
            )

        return get_artifact_bytes

    def test_negotiated_matter_files_under_lifecycle_grammar(self):
        # --- received: the inbound counterparty original ----------------------
        received = artifact_service.add_artifact(
            self.matter_id,
            source=SOURCE_GMAIL,
            actor=ACTOR_COUNTERPARTY,
            role=ROLE_ORIGINAL,
            stored_filename=self.matter["stored_filename"],
            repository=self.repo,
            owner_user_id=OWNER,
        )

        # --- ai_redline v1: the AI review output ------------------------------
        ai_redline = artifact_service.add_artifact(
            self.matter_id,
            source=SOURCE_GENERATED,
            actor=ACTOR_AI,
            role=ROLE_REDLINE,
            document_bytes=b"PK\x03\x04 ai-redline-v1",
            based_on_artifact_id=received.id,
            repository=self.repo,
            owner_user_id=OWNER,
        )

        # --- legal_review v1: the human-approved doc at the approval gate ------
        # (sent_v1's lineage is auto-derived from the latest reviewed doc by the
        # SENT hook, so this v1 handle is not referenced again directly.)
        legal_review_v1 = artifact_service.add_artifact(
            self.matter_id,
            source=SOURCE_GENERATED,
            actor=ACTOR_HUMAN,
            role=ROLE_REVIEWED,
            document_bytes=b"PK\x03\x04 legal-review-v1",
            based_on_artifact_id=ai_redline.id,
            repository=self.repo,
            owner_user_id=OWNER,
        )

        # --- sent v1: we email our marked-up copy out (SENT hook) -------------
        sent_v1 = lifecycle_sent.capture_sent_artifact(
            self.repo,
            self.matter_id,
            OWNER,
            b"PK\x03\x04 sent-v1",
            "Acme Mutual NDA - ours.docx",
            "legal@acme.example",
        )
        self.assertIsNotNone(sent_v1)
        self.assertEqual(sent_v1.version, 1)

        # --- counter v1: the counterparty sends a counter back (COUNTER hook) --
        counter_v1 = lifecycle_counter.capture_counter_artifact(
            self.repo,
            self.matter_id,
            OWNER,
            b"PK\x03\x04 counter-v1",
            "Acme Mutual NDA - counter.docx",
        )
        self.assertIsNotNone(counter_v1)
        self.assertEqual(counter_v1.version, 1)

        # --- legal_review v2: we re-review the counter ------------------------
        legal_review_v2 = artifact_service.add_artifact(
            self.matter_id,
            source=SOURCE_GENERATED,
            actor=ACTOR_HUMAN,
            role=ROLE_REVIEWED,
            document_bytes=b"PK\x03\x04 legal-review-v2",
            based_on_artifact_id=counter_v1.id,
            repository=self.repo,
            owner_user_id=OWNER,
        )
        self.assertEqual(legal_review_v2.version, 2)

        # --- sent v2: we email the re-reviewed copy out (SENT hook, v2) -------
        sent_v2 = lifecycle_sent.capture_sent_artifact(
            self.repo,
            self.matter_id,
            OWNER,
            b"PK\x03\x04 sent-v2",
            "Acme Mutual NDA - ours-rev2.docx",
            "legal@acme.example",
        )
        self.assertIsNotNone(sent_v2)
        self.assertEqual(sent_v2.version, 2)

        # --- signed: the executed PDF (terminal, no version) (SIGNED hook) ----
        signed = lifecycle_signed.capture_signed_artifact(
            self.repo,
            self.matter_id,
            OWNER,
            b"%PDF-1.7 executed-signed-copy",
            "Acme Mutual NDA - EXECUTED.pdf",
        )
        self.assertIsNotNone(signed)
        # signed is a one-shot stage: it carries no _v{N} suffix and is a PDF.
        self.assertEqual(signed.ext, "pdf")

        # The lifecycle produced exactly eight artifacts, in chronological order.
        matter = self.repo.get_matter(self.matter_id, owner_user_id=OWNER)
        artifacts = matter["artifacts"]
        self.assertEqual(len(artifacts), 8)
        self.assertEqual(
            [(a["role"], a["actor"], a["version"]) for a in artifacts],
            [
                ("original", "counterparty", 1),
                ("redline", "ai", 1),
                ("reviewed", "human", 1),
                ("sent", "human", 1),
                ("counter", "counterparty", 1),
                ("reviewed", "human", 2),
                ("sent", "human", 2),
                ("signed", "human", 1),
            ],
        )

        # --- byte-content integrity: each version owns its OWN distinct bytes --
        # The corruption this guards against (a v2 overwriting v1's stored bytes
        # under a non-version-aware storage key) passes a filename-only check; so
        # assert the EXACT bytes are retrievable per version through the registry
        # service the Drive sync reads from.
        expected_bytes = {
            legal_review_v1.id: b"PK\x03\x04 legal-review-v1",
            sent_v1.id: b"PK\x03\x04 sent-v1",
            counter_v1.id: b"PK\x03\x04 counter-v1",
            legal_review_v2.id: b"PK\x03\x04 legal-review-v2",
            sent_v2.id: b"PK\x03\x04 sent-v2",
            signed.id: b"%PDF-1.7 executed-signed-copy",
        }
        read_bytes = self._bytes_reader()
        for artifact_id, want in expected_bytes.items():
            self.assertEqual(
                read_bytes(self.matter_id, artifact_id, owner_user_id=OWNER),
                want,
                f"artifact {artifact_id} returned the wrong bytes",
            )
        # v1 vs v2 of the SAME role are byte-distinct (the overwrite bug would make
        # these identical) and stored under distinct keys.
        self.assertNotEqual(
            read_bytes(self.matter_id, legal_review_v1.id, owner_user_id=OWNER),
            read_bytes(self.matter_id, legal_review_v2.id, owner_user_id=OWNER),
        )
        self.assertNotEqual(
            read_bytes(self.matter_id, sent_v1.id, owner_user_id=OWNER),
            read_bytes(self.matter_id, sent_v2.id, owner_user_id=OWNER),
        )

        # --- sync to Drive and assert the numbered/versioned filenames --------
        fake = FakeDriveV2Service()
        result = drive_integration.sync_matter_folder(
            matter=matter,
            matter_id=self.matter_id,
            synced_at="2026-06-15T12:00:00+00:00",
            service=fake,
            get_artifact_bytes=self._bytes_reader(),
        )

        expected_filenames = [
            "01_received.docx",
            "02_ai_redline_v1.docx",
            "03_legal_review_v1.docx",
            "04_sent_v1.docx",
            "05_counter_v1.docx",
            "06_legal_review_v2.docx",
            "07_sent_v2.docx",
            "08_signed.pdf",
        ]

        # Every artifact (all eight) was newly uploaded under its grammar name.
        self.assertEqual(result["total_count"], 8)
        self.assertEqual(result["synced_count"], 8)
        synced_names = [record["filename"] for record in result["artifacts"]]
        self.assertEqual(synced_names, expected_filenames)
        # The Drive fake actually holds all eight files (plus the summary json).
        self.assertEqual(sorted(fake.file_names()), sorted(expected_filenames + ["matter_summary.json"]))

        # The Drive-synced FILE for each version carries that version's own correct
        # bytes — including the v1/v2 pairs that the overwrite bug would corrupt.
        expected_drive_content = {
            "01_received.docx": b"PK\x03\x04 received-counterparty-original",
            "02_ai_redline_v1.docx": b"PK\x03\x04 ai-redline-v1",
            "03_legal_review_v1.docx": b"PK\x03\x04 legal-review-v1",
            "04_sent_v1.docx": b"PK\x03\x04 sent-v1",
            "05_counter_v1.docx": b"PK\x03\x04 counter-v1",
            "06_legal_review_v2.docx": b"PK\x03\x04 legal-review-v2",
            "07_sent_v2.docx": b"PK\x03\x04 sent-v2",
            "08_signed.pdf": b"%PDF-1.7 executed-signed-copy",
        }
        for filename, want in expected_drive_content.items():
            self.assertEqual(
                fake.content_for(filename), want, f"{filename} uploaded the wrong bytes"
            )

        # The executed copy was uploaded exactly once under its terminal PDF name
        # (the .pdf -> application/pdf mimetype routing itself is covered by
        # test_drive_integration.test_sync_uses_grammar_filenames_and_correct_mimetypes).
        signed_creates = [
            c for c in fake.store["create_calls"] if c["body"].get("name") == "08_signed.pdf"
        ]
        self.assertEqual(len(signed_creates), 1)

        # Re-running the sync is idempotent: no new uploads, same filenames.
        rerun = drive_integration.sync_matter_folder(
            matter=matter,
            matter_id=self.matter_id,
            synced_at="2026-06-15T12:05:00+00:00",
            service=fake,
            get_artifact_bytes=self._bytes_reader(),
        )
        self.assertEqual(rerun["synced_count"], 0)
        self.assertEqual(rerun["total_count"], 8)
        self.assertEqual(
            [record["filename"] for record in rerun["artifacts"]], expected_filenames
        )


if __name__ == "__main__":
    unittest.main()
