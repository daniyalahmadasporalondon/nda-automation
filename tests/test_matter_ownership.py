from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nda_automation import matter_store


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

    def test_ownerless_legacy_matters_remain_visible_to_authenticated_users(self):
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
                fetched_legacy = matter_store.get_matter(legacy["id"], owner_user_id="user_a")
                fetched_owned = matter_store.get_matter(owned["id"], owner_user_id="user_a")
                updated_legacy = matter_store.update_matter_stage(legacy["id"], "in_review", owner_user_id="user_a")

        self.assertEqual([matter["id"] for matter in user_a_matters], [legacy["id"]])
        self.assertEqual({matter["id"] for matter in user_b_matters}, {legacy["id"], owned["id"]})
        self.assertEqual(fetched_legacy["id"], legacy["id"])
        self.assertIsNone(fetched_owned)
        self.assertEqual(updated_legacy["board_column"], "in_review")

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
