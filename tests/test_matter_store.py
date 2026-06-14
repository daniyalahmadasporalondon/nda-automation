from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from nda_automation import matter_store
from nda_automation.matter_repository import DiskMatterRepository


def _gmail_create_kwargs(**overrides):
    """create_matter kwargs for an inbound gmail attachment that should dedupe."""
    kwargs = {
        "source_filename": "Inbound NDA.docx",
        "document_bytes": b"identical attachment bytes",
        "extracted_text": "This Agreement is mutual.",
        "review_result": {"clauses": []},
        "triage": {"triage_status": "review"},
        "source_type": "gmail_inbound",
        "board_column": "gmail_demo",
        "dedupe_gmail": True,
        "intake_metadata": {
            "gmail_message_id": "msg-1",
            "gmail_attachment_id": "att-1",
            "gmail_part_id": "1",
            "attachment_filename": "Inbound NDA.docx",
        },
    }
    kwargs.update(overrides)
    return kwargs


def _create_kwargs(**overrides):
    kwargs = {
        "source_filename": "Mutual NDA.docx",
        "document_bytes": b"doc bytes",
        "extracted_text": "This Agreement is mutual.",
        "review_result": {"clauses": [{"id": "mutuality", "decision": "pass"}]},
        "triage": {"triage_status": "review"},
        "source_type": "manual_upload",
        "board_column": "intake",
    }
    kwargs.update(overrides)
    return kwargs


class MatterStorePersistenceTests(unittest.TestCase):
    def matter_store_patches(self, data_dir: str):
        root = Path(data_dir)
        return (
            patch.object(matter_store, "DATA_DIR", root),
            patch.object(matter_store, "MATTERS_PATH", root / "matters.json"),
            patch.object(matter_store, "UPLOADS_DIR", root / "uploads"),
        )

    def test_disk_store_migrates_legacy_matters_json_to_record_files(self):
        with tempfile.TemporaryDirectory() as data_dir:
            root = Path(data_dir)
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                matter_store._save_matters([{
                    "id": "matter_legacy",
                    "created_at": "2026-06-01T00:00:00+00:00",
                    "updated_at": "2026-06-01T00:00:00+00:00",
                    "source_filename": "Legacy NDA.docx",
                    "stored_filename": "matter_legacy-Legacy-NDA.docx",
                    "board_column": "gmail_demo",
                    "status": "active",
                }])
                repo = DiskMatterRepository()

                updated = repo.update_matter_stage("matter_legacy", "in_review")

                self.assertEqual(updated["board_column"], "in_review")
                self.assertTrue((root / "matters" / "matter_legacy.json").is_file())
                self.assertFalse(matter_store.MATTERS_PATH.exists())
                self.assertTrue((root / "matters.json.legacy").is_file())
                self.assertEqual(repo.get_matter("matter_legacy")["board_column"], "in_review")

    def test_disk_store_prefers_legacy_file_until_migration_finishes(self):
        with tempfile.TemporaryDirectory() as data_dir:
            root = Path(data_dir)
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                legacy_matter = {
                    "id": "matter_legacy",
                    "created_at": "2026-06-01T00:00:00+00:00",
                    "updated_at": "2026-06-01T00:00:00+00:00",
                    "source_filename": "Legacy NDA.docx",
                    "stored_filename": "matter_legacy-Legacy-NDA.docx",
                    "board_column": "gmail_demo",
                    "status": "active",
                }
                matter_store._save_matters([legacy_matter])
                (root / "matters").mkdir()
                matter_store._write_matter_record({
                    **legacy_matter,
                    "id": "matter_partial",
                    "board_column": "signed_closed",
                })
                repo = DiskMatterRepository()

                listed_before_migration = repo.list_matters()
                updated = repo.update_matter_stage("matter_legacy", "in_review")

                self.assertEqual([matter["id"] for matter in listed_before_migration], ["matter_legacy"])
                self.assertEqual(updated["board_column"], "in_review")
                self.assertEqual(repo.get_matter("matter_legacy")["board_column"], "in_review")

    def test_disk_create_update_delete_do_not_use_monolithic_save(self):
        with tempfile.TemporaryDirectory() as data_dir:
            root = Path(data_dir)
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                repo = DiskMatterRepository()

                with patch.object(matter_store, "_save_matters", side_effect=AssertionError("monolithic save used")):
                    matter = repo.create_matter(**_create_kwargs())
                    updated = repo.update_matter_stage(matter["id"], "in_review")
                    fielded = repo.update_matter_fields(matter["id"], {"human_reviewed": True})
                    deleted = repo.delete_matter(matter["id"])

                self.assertEqual(matter["id"], updated["id"])
                self.assertEqual(matter["id"], fielded["id"])
                self.assertEqual(matter["id"], deleted["id"])
                self.assertFalse(matter_store.MATTERS_PATH.exists())
                self.assertEqual(list((root / "matters").glob("*.json")), [])

    def test_disk_get_update_delete_load_single_record_after_migration(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                repo = DiskMatterRepository()
                matter = repo.create_matter(**_create_kwargs())

                with patch.object(matter_store, "_load_matters", side_effect=AssertionError("full scan used")):
                    fetched = repo.get_matter(matter["id"])
                    updated = repo.update_matter_stage(matter["id"], "in_review")
                    fielded = repo.update_matter_fields(matter["id"], {"human_reviewed": True})
                    deleted = repo.delete_matter(matter["id"])

                self.assertEqual(fetched["id"], matter["id"])
                self.assertEqual(updated["board_column"], "in_review")
                self.assertTrue(fielded["human_reviewed"])
                self.assertEqual(deleted["id"], matter["id"])

    def test_retention_prune_archives_source_document_before_deleting_live_upload(self):
        with tempfile.TemporaryDirectory() as data_dir:
            root = Path(data_dir)
            patches = self.matter_store_patches(data_dir)
            with patch.dict(os.environ, {"NDA_MATTER_RETENTION_LIMIT": "1"}):
                with patches[0], patches[1], patches[2]:
                    repo = DiskMatterRepository()
                    first = repo.create_matter(**_create_kwargs(
                        source_filename="First NDA.docx",
                        document_bytes=b"first source bytes",
                    ))
                    first_live_path = matter_store.UPLOADS_DIR / first["stored_filename"]
                    repo.update_matter_stage(first["id"], "signed_closed")

                    second = repo.create_matter(**_create_kwargs(
                        source_filename="Second NDA.docx",
                        document_bytes=b"second source bytes",
                    ))

                    matters = repo.list_matters()
                    archived_record = root / "pruned-matters" / f"{first['id']}.json"
                    archived_source = root / "pruned-matters" / "uploads" / first["stored_filename"]
                    first_live_exists = first_live_path.exists()
                    archived_record_exists = archived_record.is_file()
                    archived_record_payload = (
                        json.loads(archived_record.read_text(encoding="utf-8")) if archived_record_exists else {}
                    )
                    archived_source_bytes = archived_source.read_bytes() if archived_source.exists() else None

        self.assertEqual([matter["id"] for matter in matters], [second["id"]])
        self.assertFalse(first_live_exists)
        self.assertTrue(archived_record_exists)
        self.assertEqual(
            archived_record_payload["archived_source_document"]["archive_path"],
            "uploads/" + first["stored_filename"],
        )
        self.assertEqual(archived_source_bytes, b"first source bytes")

    def test_retention_prune_keeps_live_source_document_when_source_archive_fails(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patch.dict(os.environ, {"NDA_MATTER_RETENTION_LIMIT": "1"}):
                with patches[0], patches[1], patches[2]:
                    repo = DiskMatterRepository()
                    first = repo.create_matter(**_create_kwargs(
                        source_filename="First NDA.docx",
                        document_bytes=b"first source bytes",
                    ))
                    first_live_path = matter_store.UPLOADS_DIR / first["stored_filename"]
                    repo.update_matter_stage(first["id"], "signed_closed")

                    with (
                        patch.object(matter_store, "_write_bytes_atomic", side_effect=OSError("boom")),
                        patch("builtins.print"),
                    ):
                        second = repo.create_matter(**_create_kwargs(
                            source_filename="Second NDA.docx",
                            document_bytes=b"second source bytes",
                        ))

                    matters = repo.list_matters()
                    first_live_exists = first_live_path.exists()

        self.assertEqual({matter["id"] for matter in matters}, {first["id"], second["id"]})
        self.assertTrue(first_live_exists)


class MatterStoreConcurrencyTests(unittest.TestCase):
    """Concurrency + read-cost guarantees on the SHIPPED disk store.

    These assert against matter_store / DiskMatterRepository directly (not the
    in-memory test double) so they fail if the real dedupe/write path regresses.
    """

    def matter_store_patches(self, data_dir: str):
        root = Path(data_dir)
        return (
            patch.object(matter_store, "DATA_DIR", root),
            patch.object(matter_store, "MATTERS_PATH", root / "matters.json"),
            patch.object(matter_store, "UPLOADS_DIR", root / "uploads"),
        )

    def test_concurrent_gmail_dedupe_persists_exactly_one_matter(self):
        # Several threads import the same attachment at once. The dedupe + write
        # is one locked critical section, so exactly one matter must persist and
        # the rest must come back as duplicates — no lost update, no double-store.
        thread_count = 8
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                repo = DiskMatterRepository()
                barrier = threading.Barrier(thread_count)
                results: list[dict] = []
                errors: list[BaseException] = []
                lock = threading.Lock()

                def worker():
                    try:
                        barrier.wait()
                        created = repo.create_matter(**_gmail_create_kwargs())
                        with lock:
                            results.append(created)
                    except BaseException as error:  # noqa: BLE001 - surfaced via assert below
                        with lock:
                            errors.append(error)

                threads = [threading.Thread(target=worker) for _ in range(thread_count)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

                stored = repo.list_matters()
                fresh = [matter for matter in results if not matter.get("_existing_gmail_duplicate")]
                duplicates = [matter for matter in results if matter.get("_existing_gmail_duplicate")]

                self.assertEqual(errors, [])
                self.assertEqual(len(stored), 1, "the same attachment must persist exactly once")
                self.assertEqual(len(fresh), 1)
                self.assertEqual(len(duplicates), thread_count - 1)
                # Every duplicate response points at the one stored matter.
                self.assertEqual({matter["id"] for matter in duplicates}, {stored[0]["id"]})
                # And only one source document was written to disk.
                upload_files = list((matter_store.UPLOADS_DIR).glob("*"))
                self.assertEqual(len(upload_files), 1)

    def test_concurrent_field_update_not_lost_under_dedupe_sweeps(self):
        # An HTTP-style field writer runs while a gmail-style dedupe sweep runs.
        # Because both take _locked_store() for their whole read-modify-write, the
        # last field write must survive and the matter must never vanish.
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                repo = DiskMatterRepository()
                base = repo.create_matter(**_create_kwargs())
                matter_id = base["id"]
                stop = threading.Event()
                anomalies: list[str] = []

                def sweeper():
                    while not stop.is_set():
                        repo.deduplicate_gmail_matters()

                sweep = threading.Thread(target=sweeper)
                sweep.start()
                try:
                    for round_index in range(50):
                        expected = f"subject-{round_index}"
                        repo.update_matter_fields(matter_id, {"last_outbound_subject": expected})
                        observed = repo.get_matter(matter_id)
                        if observed is None:
                            anomalies.append("matter disappeared under a concurrent dedupe sweep")
                            break
                        if observed.get("last_outbound_subject") != expected:
                            anomalies.append(
                                f"lost update: wrote {expected!r}, read {observed.get('last_outbound_subject')!r}"
                            )
                finally:
                    stop.set()
                    sweep.join()

                self.assertEqual(anomalies, [])
                final = repo.get_matter(matter_id)
                self.assertIsNotNone(final)
                self.assertEqual(final["last_outbound_subject"], "subject-49")

    def test_dedupe_create_reads_store_once(self):
        # Read-amplification bound: a dedupe create must load the store exactly
        # once (the single locked check), not twice (an unlocked pre-check plus the
        # locked check). Asserting the call count keeps create/dedupe off the O(2N)
        # double-read path.
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                repo = DiskMatterRepository()
                for index in range(5):
                    repo.create_matter(**_create_kwargs(
                        source_filename=f"Existing {index}.docx",
                        document_bytes=f"bytes-{index}".encode(),
                    ))

                load_calls = {"count": 0}
                real_load = matter_store._load_matters

                def counting_load():
                    load_calls["count"] += 1
                    return real_load()

                with patch.object(matter_store, "_load_matters", side_effect=counting_load):
                    repo.create_matter(**_gmail_create_kwargs())

                self.assertEqual(load_calls["count"], 1, "dedupe create must not re-read the whole store")

    def test_dedupe_lookup_does_not_scan_every_matter(self):
        # The dedupe lookup is keyed, not a linear scan: with many stored matters,
        # _gmail_attachments_match must be consulted far fewer than O(N) times for
        # a single create (only the key-colliding candidates).
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                repo = DiskMatterRepository()
                stored_count = 40
                for index in range(stored_count):
                    repo.create_matter(**_gmail_create_kwargs(
                        document_bytes=f"bytes-{index}".encode(),
                        intake_metadata={
                            "gmail_message_id": f"msg-{index}",
                            "gmail_attachment_id": f"att-{index}",
                            "gmail_part_id": "1",
                            "attachment_filename": f"Inbound {index}.docx",
                        },
                    ))

                match_calls = {"count": 0}
                real_match = matter_store._gmail_attachments_match

                def counting_match(left, right):
                    match_calls["count"] += 1
                    return real_match(left, right)

                with patch.object(matter_store, "_gmail_attachments_match", side_effect=counting_match):
                    # A brand-new attachment shares no keys with any stored matter.
                    repo.create_matter(**_gmail_create_kwargs(
                        document_bytes=b"brand-new-bytes",
                        intake_metadata={
                            "gmail_message_id": "msg-new",
                            "gmail_attachment_id": "att-new",
                            "gmail_part_id": "1",
                            "attachment_filename": "Brand New.docx",
                        },
                    ))

                self.assertLess(
                    match_calls["count"],
                    stored_count,
                    "dedupe must consult only key-colliding candidates, not every stored matter",
                )


class GmailFilenameCollisionDedupeTests(unittest.TestCase):
    """A shared filename is NOT a content identity. Two genuinely different
    documents that happen to share a filename (and gmail message) must BOTH be
    preserved; a real duplicate (same name AND same bytes) must still dedupe to
    one. Dedupe keys on the stored-bytes sha256, not the filename alone.
    """

    def matter_store_patches(self, data_dir: str):
        root = Path(data_dir)
        return (
            patch.object(matter_store, "DATA_DIR", root),
            patch.object(matter_store, "MATTERS_PATH", root / "matters.json"),
            patch.object(matter_store, "UPLOADS_DIR", root / "uploads"),
        )

    def _same_filename_kwargs(self, *, attachment_id: str, part_id: str, document_bytes: bytes):
        # Two attachments under the same gmail message + same filename but with
        # DIFFERENT attachment ids / part ids, so the ONLY dedupe key they share is
        # the filename key. Content identity must then come from the sha256.
        return _gmail_create_kwargs(
            source_filename="NDA.docx",
            document_bytes=document_bytes,
            intake_metadata={
                "gmail_message_id": "msg-shared",
                "gmail_attachment_id": attachment_id,
                "gmail_part_id": part_id,
                "attachment_filename": "NDA.docx",
            },
        )

    def test_same_filename_different_content_both_preserved_on_create(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                repo = DiskMatterRepository()
                first = repo.create_matter(**self._same_filename_kwargs(
                    attachment_id="att-A",
                    part_id="1",
                    document_bytes=b"counterparty A's NDA text",
                ))
                second = repo.create_matter(**self._same_filename_kwargs(
                    attachment_id="att-B",
                    part_id="2",
                    document_bytes=b"counterparty B's COMPLETELY DIFFERENT NDA text",
                ))

                self.assertFalse(first.get("_existing_gmail_duplicate"))
                self.assertFalse(
                    second.get("_existing_gmail_duplicate"),
                    "a different document sharing only the filename must not be deduped away",
                )
                stored = repo.list_matters()
                self.assertEqual(
                    {matter["id"] for matter in stored},
                    {first["id"], second["id"]},
                    "both genuinely different same-named documents must be preserved",
                )

    def test_same_filename_same_content_still_dedupes_on_create(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                repo = DiskMatterRepository()
                identical = b"the exact same NDA bytes"
                first = repo.create_matter(**self._same_filename_kwargs(
                    attachment_id="att-A",
                    part_id="1",
                    document_bytes=identical,
                ))
                second = repo.create_matter(**self._same_filename_kwargs(
                    attachment_id="att-B",
                    part_id="2",
                    document_bytes=identical,
                ))

                self.assertTrue(
                    second.get("_existing_gmail_duplicate"),
                    "same filename AND same bytes is a real duplicate — must still dedupe",
                )
                self.assertEqual(second["id"], first["id"])
                self.assertEqual(len(repo.list_matters()), 1)

    def _sweep_matter(self, *, matter_id: str, sha256: str) -> dict:
        # A stored gmail matter that shares the message id + filename with its
        # siblings, so the ONLY collision key is the filename key — the sweep then
        # has to fall back to the content sha256 to decide identity.
        return {
            "id": matter_id,
            "board_column": "gmail_demo",
            "gmail_message_id": "msg-shared",
            "attachment_filename": "NDA.docx",
            "gmail_attachment_sha256": sha256,
        }

    def test_sweep_keeps_distinct_same_named_documents(self):
        matters = [
            self._sweep_matter(matter_id="m1", sha256="hash-of-document-one"),
            self._sweep_matter(matter_id="m2", sha256="hash-of-a-different-doc"),
        ]
        removal_ids = matter_store._gmail_duplicate_removal_ids(matters)
        self.assertEqual(removal_ids, set(), "the sweep must not merge different same-named documents")

    def test_sweep_removes_true_duplicate(self):
        matters = [
            self._sweep_matter(matter_id="m1", sha256="identical-content-hash"),
            self._sweep_matter(matter_id="m2", sha256="identical-content-hash"),
        ]
        removal_ids = matter_store._gmail_duplicate_removal_ids(matters)
        self.assertEqual(len(removal_ids), 1, "same filename AND same bytes must still dedupe to one")

    def test_sweep_keeps_same_named_matter_missing_a_hash(self):
        # A matter with no content hash cannot be confirmed a duplicate by filename
        # alone, so it must be preserved rather than merged away (legacy import).
        matters = [
            self._sweep_matter(matter_id="m1", sha256="some-hash"),
            self._sweep_matter(matter_id="m2", sha256=""),
        ]
        removal_ids = matter_store._gmail_duplicate_removal_ids(matters)
        self.assertEqual(removal_ids, set(), "a hash-less same-named matter must not be deduped away")

    def test_match_keys_on_content_not_filename(self):
        # _gmail_attachments_match is the create-time dedupe predicate. When the only
        # shared key is the filename, identity must come from the content sha256.
        def att(sha256: str) -> dict:
            return {
                "gmail_message_id": "msg-shared",
                "attachment_filename": "NDA.docx",
                "gmail_attachment_sha256": sha256,
            }

        # Same filename, different content -> NOT a duplicate.
        self.assertFalse(matter_store._gmail_attachments_match(att("hash-a"), att("hash-b")))
        # Same filename, same content -> a real duplicate.
        self.assertTrue(matter_store._gmail_attachments_match(att("hash-x"), att("hash-x")))
        # Same filename, one side missing a hash -> cannot confirm -> NOT a duplicate.
        self.assertFalse(matter_store._gmail_attachments_match(att("hash-a"), att("")))
        self.assertFalse(matter_store._gmail_attachments_match(att(""), att("")))


class MatterStoreLockTimeoutTests(unittest.TestCase):
    """Verify that _locked_store() raises MatterStoreError rather than blocking
    indefinitely when the in-process lock is already held by another thread.

    The timeout is patched to a very short value (0.05 s) so the test completes
    quickly without being flaky.
    """

    def matter_store_patches(self, data_dir: str):
        root = Path(data_dir)
        return (
            patch.object(matter_store, "DATA_DIR", root),
            patch.object(matter_store, "MATTERS_PATH", root / "matters.json"),
            patch.object(matter_store, "UPLOADS_DIR", root / "uploads"),
        )

    def test_rlock_timeout_raises_matter_store_error(self):
        """A second thread that cannot acquire the in-process RLock within the
        timeout must receive MatterStoreError, not block forever."""
        SHORT_TIMEOUT = 0.05  # seconds — fast test, not flaky

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2], \
                    patch.object(matter_store, "_LOCK_TIMEOUT_SECONDS", SHORT_TIMEOUT):
                # A barrier lets the holder thread signal it holds the lock
                # before the waiter tries to acquire it.
                holder_ready = threading.Event()
                holder_release = threading.Event()
                errors: list[BaseException] = []

                def holder():
                    # Acquire the RLock directly so the waiter cannot get it.
                    matter_store._MATTERS_LOCK.acquire()
                    try:
                        holder_ready.set()
                        holder_release.wait(timeout=5)
                    finally:
                        matter_store._MATTERS_LOCK.release()

                def waiter():
                    try:
                        # _locked_store() must raise within SHORT_TIMEOUT seconds.
                        with matter_store._locked_store():
                            pass
                    except matter_store.MatterStoreError:
                        pass  # expected
                    except BaseException as exc:  # noqa: BLE001
                        errors.append(exc)

                t_holder = threading.Thread(target=holder, daemon=True)
                t_waiter = threading.Thread(target=waiter, daemon=True)
                t_holder.start()
                holder_ready.wait(timeout=5)

                t_waiter.start()
                # Give the waiter ample time to time out and exit
                t_waiter.join(timeout=SHORT_TIMEOUT * 20)

                holder_release.set()
                t_holder.join(timeout=5)

                self.assertFalse(t_waiter.is_alive(), "waiter thread must not block indefinitely")
                self.assertEqual(errors, [], f"waiter raised unexpected error: {errors}")

    def test_rlock_acquire_succeeds_immediately_for_same_thread(self):
        """RLock.acquire(timeout=N) must return True immediately for the same
        thread that already holds the lock (re-entrancy).  This ensures the
        timed-acquire wrapper does not break re-entrant acquisition of the
        in-process lock — even though public API functions do not nest
        _locked_store() calls, the underlying RLock must still be re-entrant."""
        SHORT_TIMEOUT = 0.05  # seconds

        with patch.object(matter_store, "_LOCK_TIMEOUT_SECONDS", SHORT_TIMEOUT):
            # Acquire the real RLock directly (simulating an outer _locked_store).
            matter_store._MATTERS_LOCK.acquire()
            try:
                # Same thread: re-acquire must succeed immediately, not timeout.
                acquired = matter_store._MATTERS_LOCK.acquire(timeout=SHORT_TIMEOUT)
                if acquired:
                    matter_store._MATTERS_LOCK.release()
                self.assertTrue(acquired, "RLock must allow re-entrant acquisition by the same thread")
            finally:
                matter_store._MATTERS_LOCK.release()


if __name__ == "__main__":
    unittest.main()
