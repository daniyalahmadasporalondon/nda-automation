from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nda_automation import matter_store
from nda_automation.matter_repository import InMemoryMatterRepository


def _matter_kwargs(**overrides):
    kwargs = {
        "source_filename": "Counterparty NDA.docx",
        "document_bytes": b"document bytes",
        "extracted_text": "NDA text",
        "review_result": {"clauses": []},
        "triage": {"triage_status": "pass"},
        "source_type": "gmail_inbound",
        "board_column": "gmail_demo",
        "intake_metadata": {
            "attachment_filename": "Counterparty NDA.docx",
            "gmail_message_id": "msg_123",
            "gmail_attachment_sha256": "hash_a",
        },
    }
    kwargs.update(overrides)
    return kwargs


class _MatterStoreBackend:
    """Adapts the module-level matter_store functions to the repository call
    shape so one assertion body can drive both the shipped store and the
    in-memory double."""

    create_matter = staticmethod(matter_store.create_matter)
    list_matters = staticmethod(matter_store.list_matters)
    get_matter = staticmethod(matter_store.get_matter)
    update_matter_stage = staticmethod(matter_store.update_matter_stage)
    delete_matter = staticmethod(matter_store.delete_matter)
    export_matters_backup = staticmethod(matter_store.export_matters_backup)


class MatterOwnershipTest(unittest.TestCase):
    def matter_store_patches(self, data_dir: str):
        root = Path(data_dir)
        return (
            patch.object(matter_store, "DATA_DIR", root),
            patch.object(matter_store, "MATTERS_PATH", root / "matters.json"),
            patch.object(matter_store, "UPLOADS_DIR", root / "uploads"),
        )

    def test_owner_scoped_list_get_update_and_delete(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                user_a = matter_store.create_matter(**_matter_kwargs(owner_user_id="user_a"))
                user_b = matter_store.create_matter(**_matter_kwargs(
                    owner_user_id="user_b",
                    intake_metadata={
                        "attachment_filename": "Other NDA.docx",
                        "gmail_message_id": "msg_456",
                        "gmail_attachment_sha256": "hash_b",
                    },
                ))

                self.assertEqual([matter["id"] for matter in matter_store.list_matters("user_a")], [user_a["id"]])
                self.assertEqual([matter["id"] for matter in matter_store.list_matters("user_b")], [user_b["id"]])
                self.assertIsNone(matter_store.get_matter(user_b["id"], owner_user_id="user_a"))
                self.assertIsNone(matter_store.update_matter_stage(user_b["id"], "in_review", owner_user_id="user_a"))

                updated = matter_store.update_matter_stage(user_a["id"], "in_review", owner_user_id="user_a")
                self.assertEqual(updated["board_column"], "in_review")
                self.assertIsNone(matter_store.delete_matter(user_b["id"], owner_user_id="user_a"))

                deleted = matter_store.delete_matter(user_b["id"], owner_user_id="user_b")
                self.assertEqual(deleted["id"], user_b["id"])
                self.assertEqual([matter["id"] for matter in matter_store.list_matters()], [user_a["id"]])

    def test_ownerless_legacy_matters_are_not_served_to_authenticated_users(self):
        # SECURITY (fail-closed): a matter with no owner_user_id (legacy import,
        # Gmail shared-sync before ownership assignment) must NOT leak to an
        # arbitrary authenticated user. It is only reachable in the single-tenant
        # / auth-disabled path (empty requester id), so the data is preserved but
        # never served cross-tenant.
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                legacy = matter_store.create_matter(**_matter_kwargs())
                owned = matter_store.create_matter(**_matter_kwargs(
                    owner_user_id="user_b",
                    intake_metadata={
                        "attachment_filename": "Other NDA.docx",
                        "gmail_message_id": "msg_456",
                        "gmail_attachment_sha256": "hash_b",
                    },
                ))

                user_a_matters = matter_store.list_matters("user_a")
                user_b_matters = matter_store.list_matters("user_b")
                single_tenant_matters = matter_store.list_matters()
                fetched_legacy = matter_store.get_matter(legacy["id"], owner_user_id="user_a")
                fetched_owned = matter_store.get_matter(owned["id"], owner_user_id="user_a")
                updated_legacy = matter_store.update_matter_stage(legacy["id"], "in_review", owner_user_id="user_a")
                deleted_legacy = matter_store.delete_matter(legacy["id"], owner_user_id="user_a")
                # The single-tenant (no-auth) path still reaches the legacy matter.
                fetched_single_tenant = matter_store.get_matter(legacy["id"])

        # Authenticated users see ONLY their own matters; the ownerless legacy
        # matter is invisible to both of them.
        self.assertEqual([matter["id"] for matter in user_a_matters], [])
        self.assertEqual([matter["id"] for matter in user_b_matters], [owned["id"]])
        self.assertIsNone(fetched_legacy)
        self.assertIsNone(fetched_owned)
        self.assertIsNone(updated_legacy)
        self.assertIsNone(deleted_legacy)
        # Data is not orphaned: still present and reachable single-tenant.
        self.assertEqual({matter["id"] for matter in single_tenant_matters}, {legacy["id"], owned["id"]})
        self.assertEqual(fetched_single_tenant["id"], legacy["id"])

    def test_ownerless_matter_is_not_readable_editable_deletable_or_exportable_cross_tenant(self):
        # Regression for the cross-tenant leak: an ownerless matter must be
        # denied to an authenticated user across EVERY access path.
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                ownerless = matter_store.create_matter(**_matter_kwargs())
                attacker = "user_attacker"

                # read
                self.assertEqual(matter_store.list_matters(attacker), [])
                self.assertIsNone(matter_store.get_matter(ownerless["id"], owner_user_id=attacker))
                # edit (stage + arbitrary fields + redline draft + review)
                self.assertIsNone(matter_store.update_matter_stage(ownerless["id"], "in_review", owner_user_id=attacker))
                self.assertIsNone(matter_store.update_matter_fields(ownerless["id"], {"human_reviewed": True}, owner_user_id=attacker))
                self.assertIsNone(matter_store.update_redline_draft(ownerless["id"], {"manual_redline_edits": []}, owner_user_id=attacker))
                self.assertIsNone(matter_store.update_matter_review(ownerless["id"], {"clauses": []}, {"triage_status": "pass"}, owner_user_id=attacker))
                # export
                backup = matter_store.export_matters_backup(owner_user_id=attacker)
                self.assertEqual(backup["matters"], [])
                self.assertEqual(backup["matter_count"], 0)
                # delete
                self.assertIsNone(matter_store.delete_matter(ownerless["id"], owner_user_id=attacker))

                # The matter survives every denied attempt and is unchanged.
                survivor = matter_store.get_matter(ownerless["id"])
                self.assertEqual(survivor["id"], ownerless["id"])
                self.assertEqual(survivor["board_column"], "gmail_demo")
                self.assertFalse(survivor.get("human_reviewed"))

    def _assert_ownerless_denied_cross_tenant(self, backend):
        # Shared assertion body so the SHIPPED matter_store and the in-memory
        # double are proven to agree — a fix that only held in the double would
        # be a false green (prod ships matter_store).
        ownerless = backend.create_matter(**_matter_kwargs())
        attacker = "user_attacker"
        self.assertEqual(backend.list_matters(attacker), [])
        self.assertIsNone(backend.get_matter(ownerless["id"], owner_user_id=attacker))
        self.assertIsNone(backend.update_matter_stage(ownerless["id"], "in_review", owner_user_id=attacker))
        self.assertIsNone(backend.delete_matter(ownerless["id"], owner_user_id=attacker))
        self.assertEqual(backend.export_matters_backup(owner_user_id=attacker)["matters"], [])
        # Data is not orphaned: the single-tenant (no-auth) path still reaches it.
        self.assertEqual(backend.get_matter(ownerless["id"])["id"], ownerless["id"])

    def test_ownerless_leak_fix_holds_on_shipped_and_in_memory_backends(self):
        # SHIPPED disk-backed matter_store (the production path).
        with self.subTest(backend="matter_store"):
            with tempfile.TemporaryDirectory() as data_dir:
                patches = self.matter_store_patches(data_dir)
                with patches[0], patches[1], patches[2]:
                    self._assert_ownerless_denied_cross_tenant(_MatterStoreBackend())
        # In-memory double used elsewhere in the suite — must match shipped.
        with self.subTest(backend="InMemoryMatterRepository"):
            self._assert_ownerless_denied_cross_tenant(InMemoryMatterRepository())

    def test_gmail_duplicate_lookup_is_owner_scoped(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                user_a = matter_store.create_matter(**_matter_kwargs(owner_user_id="user_a", dedupe_gmail=True))
                user_b = matter_store.create_matter(**_matter_kwargs(owner_user_id="user_b", dedupe_gmail=True))
                duplicate_a = matter_store.create_matter(**_matter_kwargs(owner_user_id="user_a", dedupe_gmail=True))

                found_a = matter_store.find_gmail_attachment(
                    "msg_123",
                    "",
                    attachment_sha256="hash_a",
                    owner_user_id="user_a",
                )
                found_b = matter_store.find_gmail_attachment(
                    "msg_123",
                    "",
                    attachment_sha256="hash_a",
                    owner_user_id="user_b",
                )

        self.assertEqual(user_a["id"], duplicate_a["id"])
        self.assertEqual(duplicate_a["_existing_gmail_duplicate"], True)
        self.assertNotEqual(user_a["id"], user_b["id"])
        self.assertEqual(found_a["id"], user_a["id"])
        self.assertEqual(found_b["id"], user_b["id"])

    def test_owner_scoped_dedupe_does_not_drop_other_users_matters(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                first_a = matter_store.create_matter(**_matter_kwargs(owner_user_id="user_a"))
                second_a = matter_store.create_matter(**_matter_kwargs(owner_user_id="user_a"))
                first_b = matter_store.create_matter(**_matter_kwargs(owner_user_id="user_b"))
                second_b = matter_store.create_matter(**_matter_kwargs(owner_user_id="user_b"))

                removed = matter_store.deduplicate_gmail_matters(owner_user_id="user_a")
                remaining = matter_store.list_matters()
                remaining_a = matter_store.list_matters("user_a")
                remaining_b = matter_store.list_matters("user_b")

        self.assertEqual(removed, 1)
        self.assertEqual(len(remaining), 3)
        self.assertEqual(len(remaining_a), 1)
        self.assertIn(remaining_a[0]["id"], {first_a["id"], second_a["id"]})
        self.assertEqual({matter["id"] for matter in remaining_b}, {first_b["id"], second_b["id"]})


if __name__ == "__main__":
    unittest.main()
