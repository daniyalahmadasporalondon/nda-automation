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

                    # Fail ONLY the prune source-archive write (under pruned-matters/),
                    # not the new matter's own live source-doc write into UPLOADS_DIR --
                    # both now flow through _write_bytes_atomic, so the simulated failure
                    # has to be scoped to the archive path to model "source archive fails".
                    real_write_bytes_atomic = matter_store._write_bytes_atomic

                    def fail_only_archive_write(path, payload):
                        if Path(path).parent != matter_store.UPLOADS_DIR:
                            raise OSError("boom")
                        return real_write_bytes_atomic(path, payload)

                    with (
                        patch.object(matter_store, "_write_bytes_atomic", side_effect=fail_only_archive_write),
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

    def test_create_matter_stages_source_document_atomically(self):
        # The stored source doc must go through the same tmp+fsync+replace helper as
        # every other byte payload, so a crash/OOM mid-write can never leave a
        # TRUNCATED file at the live source path that the matter record points at.
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                repo = DiskMatterRepository()
                captured: dict[str, Path] = {}
                real_write_bytes_atomic = matter_store._write_bytes_atomic

                def recording_write(path, payload):
                    if Path(path).parent == matter_store.UPLOADS_DIR:
                        captured["source_path"] = Path(path)
                    return real_write_bytes_atomic(path, payload)

                with patch.object(matter_store, "_write_bytes_atomic", side_effect=recording_write):
                    matter = repo.create_matter(**_create_kwargs(
                        source_filename="Atomic NDA.docx",
                        document_bytes=b"atomic source bytes",
                    ))

                stored_path = matter_store.UPLOADS_DIR / matter["stored_filename"]
                captured_source_path = captured.get("source_path")
                stored_bytes = stored_path.read_bytes()

        # The source doc was staged through the atomic helper (not a bare write_bytes).
        self.assertEqual(captured_source_path, stored_path)
        self.assertEqual(stored_bytes, b"atomic source bytes")

    def test_create_matter_leaves_no_truncated_source_on_mid_write_crash(self):
        # Simulate a hard kill *during* the source-doc write. With a bare
        # write_bytes the partially-written bytes would persist at the live source
        # path; with the atomic helper the failure hits a .tmp file that is unlinked,
        # so the live path stays absent (no truncated/orphaned source doc).
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                repo = DiskMatterRepository()

                # The real helper opens a temp file in UPLOADS_DIR and writes into it.
                # Make the write blow up so we model a crash mid-write of the source doc.
                original_open = Path.open

                def exploding_open(self, *args, **kwargs):
                    handle = original_open(self, *args, **kwargs)
                    if self.parent == matter_store.UPLOADS_DIR and "w" in (args[0] if args else kwargs.get("mode", "")):
                        original_write = handle.write

                        def boom(_data):
                            # Write a truncated prefix first, then crash — this is the
                            # exact failure the atomic helper must contain.
                            original_write(b"trunc")
                            raise OSError("simulated OOM kill mid-write")

                        handle.write = boom  # type: ignore[method-assign]
                    return handle

                stored_filename = None
                with patch.object(Path, "open", exploding_open):
                    with self.assertRaises(OSError):
                        repo.create_matter(**_create_kwargs(
                            source_filename="Crash NDA.docx",
                            document_bytes=b"full intended source bytes",
                        ))

                upload_files = sorted(p.name for p in matter_store.UPLOADS_DIR.glob("*"))

        # No live source doc and no leftover .tmp staging file survived the crash.
        self.assertEqual(upload_files, [])


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


class UpdateMatterCounterpartyTests(unittest.TestCase):
    def matter_store_patches(self, data_dir: str):
        root = Path(data_dir)
        return (
            patch.object(matter_store, "DATA_DIR", root),
            patch.object(matter_store, "MATTERS_PATH", root / "matters.json"),
            patch.object(matter_store, "UPLOADS_DIR", root / "uploads"),
        )

    def test_override_persists_and_round_trips_after_reload(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                repo = DiskMatterRepository()
                matter = repo.create_matter(**_create_kwargs())
                override = {
                    "name": "Globex Industries Ltd",
                    "confidence": 0.95,
                    "verified": True,
                    "first_party": "Aspora",
                    "second_party": "Globex Industries Ltd",
                    "source": "human_override",
                }
                updated = matter_store.update_matter_counterparty(matter["id"], override)
                self.assertIsNotNone(updated)
                self.assertEqual(
                    updated["intake_metadata"]["counterparty"]["name"],
                    "Globex Industries Ltd",
                )

                # Round-trip: a fresh load from disk must carry the override.
                reloaded = repo.get_matter(matter["id"])
                stored = reloaded["intake_metadata"]["counterparty"]
                self.assertEqual(stored["name"], "Globex Industries Ltd")
                self.assertTrue(stored["verified"])
                self.assertEqual(stored["confidence"], 0.95)
                self.assertEqual(stored["second_party"], "Globex Industries Ltd")
                self.assertEqual(stored["source"], "human_override")

    def test_malformed_override_is_coerced_and_empty_name_never_verified(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                repo = DiskMatterRepository()
                matter = repo.create_matter(**_create_kwargs())
                # Hostile/partial override: empty name but verified=True, junk confidence.
                updated = matter_store.update_matter_counterparty(
                    matter["id"],
                    {"name": "", "verified": True, "confidence": "not-a-number", "extra": "drop me"},
                )
                stored = updated["intake_metadata"]["counterparty"]
                self.assertEqual(stored["name"], "")
                self.assertFalse(stored["verified"])
                self.assertEqual(stored["confidence"], 0.0)
                # Coerced to exactly the canonical shape (no junk keys leak through).
                self.assertEqual(
                    set(stored.keys()),
                    {"name", "confidence", "verified", "first_party", "second_party", "source"},
                )

    def test_owner_scoping_wrong_owner_returns_none_and_does_not_write(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                repo = DiskMatterRepository()
                matter = repo.create_matter(**_create_kwargs(owner_user_id="user-a"))
                # Seed a known counterparty as the rightful owner.
                matter_store.update_matter_counterparty(
                    matter["id"],
                    {"name": "Rightful Co", "verified": True, "confidence": 0.9},
                    owner_user_id="user-a",
                )

                # A different authenticated tenant must not be able to overwrite it.
                result = matter_store.update_matter_counterparty(
                    matter["id"],
                    {"name": "Attacker Co", "verified": True, "confidence": 1.0},
                    owner_user_id="user-b",
                )
                self.assertIsNone(result)

                # The stored value is untouched (no cross-tenant write happened).
                stored = repo.get_matter(matter["id"], owner_user_id="user-a")[
                    "intake_metadata"
                ]["counterparty"]
                self.assertEqual(stored["name"], "Rightful Co")

    def test_missing_matter_returns_none(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                DiskMatterRepository()  # initialize the records dir
                result = matter_store.update_matter_counterparty(
                    "matter_does_not_exist",
                    {"name": "Nobody", "confidence": 0.5},
                )
                self.assertIsNone(result)

    def test_override_preserves_existing_nested_intake_metadata(self):
        # When a matter already carries a nested intake_metadata dict (e.g. the AI
        # extraction wrote one at intake), the counterparty override must merge into
        # it rather than clobbering sibling keys.
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                repo = DiskMatterRepository()
                # Seed a review_result that carries a counterparty so _attach_intake_
                # counterparty creates matter["intake_metadata"] at create time, then
                # add a sibling key to the stored record to prove it is preserved.
                matter = repo.create_matter(**_create_kwargs(
                    review_result={
                        "clauses": [],
                        "counterparty": {
                            "name": "Original Co",
                            "confidence": 0.4,
                            "verified": False,
                            "first_party": "",
                            "second_party": "",
                            "source": "ai_review_preamble",
                        },
                    },
                ))
                # Sanity: the nested intake_metadata exists from intake.
                self.assertIn("intake_metadata", matter)
                record = matter_store._load_matter_record_by_id(matter["id"])
                record["intake_metadata"]["custom_marker"] = "keep-me"
                matter_store._save_matter_record(record)

                matter_store.update_matter_counterparty(
                    matter["id"],
                    {"name": "Acme Corp", "verified": True, "confidence": 0.9},
                )
                reloaded = repo.get_matter(matter["id"])
                intake = reloaded["intake_metadata"]
                # The sibling key survives; the counterparty is replaced with the override.
                self.assertEqual(intake["custom_marker"], "keep-me")
                self.assertEqual(intake["counterparty"]["name"], "Acme Corp")
                self.assertTrue(intake["counterparty"]["verified"])


class GmailInboundCursorTests(unittest.TestCase):
    """The persistent per-owner Gmail inbound drain cursor (Option B for the
    drain-stall fix): a low-water-mark on internalDate that only ever moves to an
    OLDER message and survives across polls."""

    def cursor_patches(self, data_dir: str):
        root = Path(data_dir)
        return (
            patch.object(matter_store, "DATA_DIR", root),
            patch.object(matter_store, "GMAIL_INBOUND_CURSORS_PATH", root / "gmail_inbound_cursors.json"),
        )

    def test_cursor_defaults_to_zero_and_persists_across_reads(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.cursor_patches(data_dir)
            for p in patches:
                p.start()
            try:
                # No file yet -> 0 ("no cursor; scan newest-first un-bounded").
                self.assertEqual(matter_store.gmail_inbound_cursor("owner_1"), 0)
                # First advance writes the file and is readable back.
                matter_store.advance_gmail_inbound_cursor("owner_1", 1_700_000_000_000)
                self.assertTrue((Path(data_dir) / "gmail_inbound_cursors.json").is_file())
                self.assertEqual(matter_store.gmail_inbound_cursor("owner_1"), 1_700_000_000_000)
            finally:
                for p in patches:
                    p.stop()

    def test_cursor_only_descends_and_is_per_owner(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.cursor_patches(data_dir)
            for p in patches:
                p.start()
            try:
                matter_store.advance_gmail_inbound_cursor("owner_1", 5000)
                # A LOWER (older) frontier is accepted (the drain reached deeper).
                self.assertEqual(matter_store.advance_gmail_inbound_cursor("owner_1", 3000), 3000)
                # A HIGHER (newer) value never pushes the cursor back up (newly-arrived
                # mail above the frontier must not re-expose a drained region).
                self.assertEqual(matter_store.advance_gmail_inbound_cursor("owner_1", 9000), 3000)
                self.assertEqual(matter_store.gmail_inbound_cursor("owner_1"), 3000)
                # Non-positive dates are ignored (we never learned a real date).
                self.assertEqual(matter_store.advance_gmail_inbound_cursor("owner_1", 0), 3000)
                # A different owner keeps an independent cursor.
                self.assertEqual(matter_store.gmail_inbound_cursor("owner_2"), 0)
                matter_store.advance_gmail_inbound_cursor("owner_2", 7000)
                self.assertEqual(matter_store.gmail_inbound_cursor("owner_2"), 7000)
                self.assertEqual(matter_store.gmail_inbound_cursor("owner_1"), 3000)
            finally:
                for p in patches:
                    p.stop()

    def test_cursor_reset_clears_only_that_owner(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.cursor_patches(data_dir)
            for p in patches:
                p.start()
            try:
                matter_store.advance_gmail_inbound_cursor("owner_1", 3000)
                matter_store.advance_gmail_inbound_cursor("owner_2", 7000)
                matter_store.reset_gmail_inbound_cursor("owner_1")
                self.assertEqual(matter_store.gmail_inbound_cursor("owner_1"), 0)
                self.assertEqual(matter_store.gmail_inbound_cursor("owner_2"), 7000)
                # Reset is idempotent / safe on an unknown owner.
                matter_store.reset_gmail_inbound_cursor("owner_missing")
                self.assertEqual(matter_store.gmail_inbound_cursor("owner_2"), 7000)
            finally:
                for p in patches:
                    p.stop()

    def test_cursor_survives_corrupt_store_file(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.cursor_patches(data_dir)
            for p in patches:
                p.start()
            try:
                cursor_path = Path(data_dir) / "gmail_inbound_cursors.json"
                cursor_path.write_text("{ not valid json", encoding="utf-8")
                # A corrupt store reads as empty (0) rather than raising into the poll.
                self.assertEqual(matter_store.gmail_inbound_cursor("owner_1"), 0)
                # And a subsequent advance heals the file.
                matter_store.advance_gmail_inbound_cursor("owner_1", 4000)
                self.assertEqual(matter_store.gmail_inbound_cursor("owner_1"), 4000)
            finally:
                for p in patches:
                    p.stop()


class ConcurrentArtifactRegistrationTests(unittest.TestCase):
    """#17 — concurrent artifact registration must not drop an artifact.

    The old path (get_matter -> compute existing+[new] in Python ->
    update_matter_artifacts(whole_list)) read and wrote under two separate locks,
    so a concurrent registration's list overwrote the first. The atomic
    ``mutate_matter_artifacts`` read-modify-write closes that lost-update window.
    """

    def matter_store_patches(self, data_dir: str):
        root = Path(data_dir)
        return (
            patch.object(matter_store, "DATA_DIR", root),
            patch.object(matter_store, "MATTERS_PATH", root / "matters.json"),
            patch.object(matter_store, "UPLOADS_DIR", root / "uploads"),
        )

    def test_concurrent_artifact_registration_loses_no_artifact(self):
        from nda_automation import artifact_service
        from nda_automation.artifact_registry import (
            ACTOR_AI,
            ROLE_REDLINE,
            SOURCE_GENERATED,
        )

        thread_count = 12
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                repo = DiskMatterRepository()
                base = repo.create_matter(**_create_kwargs())
                matter_id = base["id"]

                barrier = threading.Barrier(thread_count)
                errors: list[BaseException] = []
                lock = threading.Lock()

                def worker(index: int):
                    try:
                        barrier.wait()
                        artifact_service.add_artifact(
                            matter_id,
                            source=SOURCE_GENERATED,
                            actor=ACTOR_AI,
                            role=ROLE_REDLINE,
                            document_bytes=f"redline-{index}".encode(),
                            make_current=False,
                        )
                    except BaseException as error:  # noqa: BLE001 - surfaced below
                        with lock:
                            errors.append(error)

                threads = [
                    threading.Thread(target=worker, args=(index,))
                    for index in range(thread_count)
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

                self.assertEqual(errors, [])
                final = repo.get_matter(matter_id)
                artifacts = final.get("artifacts") or []
                # Every concurrent registration must survive — no lost update.
                self.assertEqual(
                    len(artifacts),
                    thread_count,
                    "a concurrent artifact registration was dropped (lost update)",
                )
                # Versions must be unique and contiguous 1..N (no two writers
                # collided on the same version because of a stale read).
                versions = sorted(int(a.get("version") or 0) for a in artifacts)
                self.assertEqual(versions, list(range(1, thread_count + 1)))


class RefreshReviewRaceTests(unittest.TestCase):
    """#19 — a human edit that lands during the AI window must survive."""

    def matter_store_patches(self, data_dir: str):
        root = Path(data_dir)
        return (
            patch.object(matter_store, "DATA_DIR", root),
            patch.object(matter_store, "MATTERS_PATH", root / "matters.json"),
            patch.object(matter_store, "UPLOADS_DIR", root / "uploads"),
        )

    def test_human_reviewed_set_during_window_is_not_reverted(self):
        # Simulate: refresh captured updated_at, then a human marked the matter
        # reviewed (updated_at moved), THEN the refresh's guarded write lands.
        # Because expected_updated_at no longer matches, human_reviewed is preserved.
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                repo = DiskMatterRepository()
                base = repo.create_matter(**_create_kwargs())
                matter_id = base["id"]
                expected_updated_at = base["updated_at"]

                # Human marks reviewed + saves a redline draft DURING the window.
                repo.update_matter_fields(matter_id, {"human_reviewed": True})
                repo.update_redline_draft(matter_id, {"edits": ["keep me"]})

                # The refresh's late write, guarded by the stale expected_updated_at.
                refreshed = repo.refresh_matter_review(
                    matter_id,
                    {"clauses": [], "source": "fresh-ai"},
                    {"triage_status": "review"},
                    expected_updated_at=expected_updated_at,
                )
                self.assertIsNotNone(refreshed)
                # The fresh review IS stored...
                self.assertEqual(refreshed["review_result"]["source"], "fresh-ai")
                # ...but the human edits that raced the AI window SURVIVE.
                self.assertTrue(
                    refreshed["human_reviewed"],
                    "mark-reviewed landing during the AI window was reverted",
                )
                self.assertEqual(
                    refreshed.get("redline_draft"),
                    {"edits": ["keep me"]},
                    "redline draft saved during the AI window was dropped",
                )

    def test_uncontended_refresh_resets_human_reviewed_and_drops_draft(self):
        # No write during the window: updated_at still matches, so the normal
        # refresh semantics apply (fresh review supersedes the prior sign-off).
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                repo = DiskMatterRepository()
                base = repo.create_matter(**_create_kwargs())
                matter_id = base["id"]
                # Pre-existing sign-off + draft, then refresh with a MATCHING
                # expected_updated_at (nothing raced the window).
                repo.update_matter_fields(matter_id, {"human_reviewed": True})
                marked = repo.update_redline_draft(matter_id, {"edits": ["stale"]})
                expected_updated_at = marked["updated_at"]

                refreshed = repo.refresh_matter_review(
                    matter_id,
                    {"clauses": [], "source": "fresh-ai"},
                    {"triage_status": "review"},
                    expected_updated_at=expected_updated_at,
                )
                self.assertFalse(refreshed["human_reviewed"])
                self.assertNotIn("redline_draft", refreshed)


class ListMattersCacheTests(unittest.TestCase):
    """#25 — list_matters cache: fresh-after-write, faster on repeat, never stale."""

    def setUp(self):
        # Each test gets a pristine module-global cache.
        matter_store._invalidate_list_cache()
        self.addCleanup(matter_store._invalidate_list_cache)

    def matter_store_patches(self, data_dir: str):
        root = Path(data_dir)
        return (
            patch.object(matter_store, "DATA_DIR", root),
            patch.object(matter_store, "MATTERS_PATH", root / "matters.json"),
            patch.object(matter_store, "UPLOADS_DIR", root / "uploads"),
        )

    def test_repeat_call_does_not_reparse_records(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                repo = DiskMatterRepository()
                for index in range(10):
                    repo.create_matter(**_create_kwargs(
                        source_filename=f"NDA {index}.docx",
                        document_bytes=f"bytes-{index}".encode(),
                    ))

                # Prime the cache.
                first = repo.list_matters()
                self.assertEqual(len(first), 10)

                # A second call with NOTHING changed must not re-parse any record
                # file (the perf win). Count _load_matter_record_path invocations.
                parse_calls = {"count": 0}
                real_parse = matter_store._load_matter_record_path

                def counting_parse(path):
                    parse_calls["count"] += 1
                    return real_parse(path)

                with patch.object(
                    matter_store, "_load_matter_record_path", side_effect=counting_parse
                ):
                    second = repo.list_matters()

                self.assertEqual(len(second), 10)
                self.assertEqual(
                    parse_calls["count"],
                    0,
                    "an unchanged repeat list_matters re-parsed record files (cache miss)",
                )

    def test_write_invalidates_cache_immediately(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                repo = DiskMatterRepository()
                repo.create_matter(**_create_kwargs(source_filename="First.docx"))

                primed = repo.list_matters()
                self.assertEqual(len(primed), 1)

                # A write must make the NEXT list_matters reflect it immediately
                # (write-through invalidation), with no mtime-granularity wait.
                created = repo.create_matter(**_create_kwargs(
                    source_filename="Second.docx", document_bytes=b"second"
                ))
                after_create = repo.list_matters()
                self.assertEqual(len(after_create), 2)
                self.assertIn(created["id"], {m["id"] for m in after_create})

                # A field update is reflected immediately too.
                repo.update_matter_fields(created["id"], {"last_outbound_subject": "X"})
                after_update = repo.list_matters()
                observed = next(m for m in after_update if m["id"] == created["id"])
                self.assertEqual(observed.get("last_outbound_subject"), "X")

                # A delete is reflected immediately.
                repo.delete_matter(created["id"])
                after_delete = repo.list_matters()
                self.assertEqual(len(after_delete), 1)
                self.assertNotIn(created["id"], {m["id"] for m in after_delete})

    def test_external_record_change_is_detected_by_fingerprint(self):
        # Cross-process correctness proxy: mutate a record file directly (as
        # another process would), bypassing the in-process write-through. The
        # fingerprint (mtime_ns + size) must catch it on the next read.
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                repo = DiskMatterRepository()
                created = repo.create_matter(**_create_kwargs(source_filename="Ext.docx"))
                matter_id = created["id"]
                primed = repo.list_matters()
                self.assertEqual(primed[0].get("last_outbound_subject"), None)

                # Rewrite the record file out-of-band (simulating another process),
                # then bump its mtime so the fingerprint definitely advances even on
                # coarse-resolution filesystems.
                record_path = matter_store._matter_records_dir() / f"{matter_id}.json"
                payload = json.loads(record_path.read_text(encoding="utf-8"))
                payload["last_outbound_subject"] = "external-edit"
                record_path.write_text(json.dumps(payload), encoding="utf-8")
                stat = record_path.stat()
                os.utime(record_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

                refreshed = repo.list_matters()
                self.assertEqual(
                    refreshed[0].get("last_outbound_subject"),
                    "external-edit",
                    "an out-of-band record change was served stale from the cache",
                )

    def test_cache_never_serves_cross_tenant_data(self):
        # The cache holds the UNFILTERED list; owner scoping is applied per call
        # AFTER the cache. So priming with one owner must never leak to another.
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                repo = DiskMatterRepository()
                repo.create_matter(**_create_kwargs(
                    source_filename="A.docx",
                    intake_metadata={"owner_user_id": "alice"},
                    owner_user_id="alice",
                ))
                repo.create_matter(**_create_kwargs(
                    source_filename="B.docx",
                    document_bytes=b"b",
                    intake_metadata={"owner_user_id": "bob"},
                    owner_user_id="bob",
                ))

                # Prime via alice, then read as bob: bob must see only bob's matter.
                alice_view = repo.list_matters(owner_user_id="alice")
                self.assertEqual({m.get("owner_user_id") for m in alice_view}, {"alice"})
                bob_view = repo.list_matters(owner_user_id="bob")
                self.assertEqual({m.get("owner_user_id") for m in bob_view}, {"bob"})
                # And the unscoped (single-tenant) view sees both.
                self.assertEqual(len(repo.list_matters()), 2)

    def test_cached_result_is_isolated_from_caller_mutation(self):
        # A caller mutating a returned matter must not corrupt the shared cache.
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                repo = DiskMatterRepository()
                repo.create_matter(**_create_kwargs(source_filename="Iso.docx"))
                first = repo.list_matters()
                first[0]["review_result"] = {"poisoned": True}
                second = repo.list_matters()
                self.assertNotEqual(second[0].get("review_result"), {"poisoned": True})


if __name__ == "__main__":
    unittest.main()
