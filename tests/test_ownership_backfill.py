"""Tests for the one-time ownerless-matter ownership backfill (task #6, Option A).

Exercises the SHIPPED matter_store path directly (not a double): resolvable
gmail_account -> that user; unresolvable -> admin fallback; already-owned matters
untouched; idempotent on re-run. Plus the orchestration layer that builds the
email->id map and resolves the admin identity.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nda_automation import matter_store, ownership_backfill


def _matter_kwargs(**overrides):
    kwargs = {
        "source_filename": "Counterparty NDA.docx",
        "document_bytes": b"document bytes",
        "extracted_text": "NDA text",
        "review_result": {"clauses": []},
        "triage": {"triage_status": "pass"},
        "source_type": "gmail_inbound",
        "board_column": "gmail_demo",
    }
    kwargs.update(overrides)
    return kwargs


class OwnerlessBackfillStoreTests(unittest.TestCase):
    def _patches(self, data_dir: str):
        root = Path(data_dir)
        return (
            patch.object(matter_store, "DATA_DIR", root),
            patch.object(matter_store, "MATTERS_PATH", root / "matters.json"),
            patch.object(matter_store, "UPLOADS_DIR", root / "uploads"),
        )

    def test_backfill_resolves_gmail_then_admin_and_leaves_owned_untouched(self):
        with tempfile.TemporaryDirectory() as data_dir:
            p = self._patches(data_dir)
            with p[0], p[1], p[2]:
                # ownerless, gmail_account matches a known user (case-insensitive)
                gmail_matter = matter_store.create_matter(
                    **_matter_kwargs(intake_metadata={"gmail_account": "Alice@Example.com", "gmail_message_id": "m1", "gmail_attachment_sha256": "h1"})
                )
                # ownerless, gmail_account matches nobody -> admin fallback
                admin_matter = matter_store.create_matter(
                    **_matter_kwargs(intake_metadata={"gmail_account": "stranger@elsewhere.com", "gmail_message_id": "m2", "gmail_attachment_sha256": "h2"})
                )
                # already owned -> must be untouched
                owned_matter = matter_store.create_matter(
                    **_matter_kwargs(owner_user_id="user_b", intake_metadata={"gmail_account": "Alice@Example.com", "gmail_message_id": "m3", "gmail_attachment_sha256": "h3"})
                )

                summary = matter_store.migrate_ownerless_matter_ownership(
                    user_email_to_id={"alice@example.com": "google:alice"},
                    admin_user_id="admin_user",
                )

                self.assertEqual(summary["assigned_by_gmail"], 1)
                self.assertEqual(summary["assigned_to_admin"], 1)
                self.assertEqual(summary["already_owned"], 1)
                self.assertEqual(summary["skipped_unresolved"], 0)
                self.assertEqual(matter_store.get_matter(gmail_matter["id"])["owner_user_id"], "google:alice")
                self.assertEqual(matter_store.get_matter(admin_matter["id"])["owner_user_id"], "admin_user")
                self.assertEqual(matter_store.get_matter(owned_matter["id"])["owner_user_id"], "user_b")

    def test_backfill_is_idempotent_on_rerun(self):
        with tempfile.TemporaryDirectory() as data_dir:
            p = self._patches(data_dir)
            with p[0], p[1], p[2]:
                ownerless = matter_store.create_matter(
                    **_matter_kwargs(intake_metadata={"gmail_account": "alice@example.com", "gmail_message_id": "m1", "gmail_attachment_sha256": "h1"})
                )
                first = matter_store.migrate_ownerless_matter_ownership(
                    user_email_to_id={"alice@example.com": "google:alice"}, admin_user_id="admin_user"
                )
                self.assertEqual(first["assigned_by_gmail"], 1)
                # Re-run: nothing left ownerless -> all already_owned, zero new assignments.
                second = matter_store.migrate_ownerless_matter_ownership(
                    user_email_to_id={"alice@example.com": "google:alice"}, admin_user_id="admin_user"
                )
                self.assertEqual(second["assigned_by_gmail"], 0)
                self.assertEqual(second["assigned_to_admin"], 0)
                self.assertEqual(second["already_owned"], 1)
                self.assertEqual(matter_store.get_matter(ownerless["id"])["owner_user_id"], "google:alice")

    def test_backfill_leaves_ownerless_when_no_mapping_and_no_admin(self):
        with tempfile.TemporaryDirectory() as data_dir:
            p = self._patches(data_dir)
            with p[0], p[1], p[2]:
                ownerless = matter_store.create_matter(
                    **_matter_kwargs(intake_metadata={"gmail_account": "stranger@elsewhere.com", "gmail_message_id": "m1", "gmail_attachment_sha256": "h1"})
                )
                summary = matter_store.migrate_ownerless_matter_ownership(
                    user_email_to_id={}, admin_user_id=""
                )
                # No gmail match, no admin fallback -> left ownerless rather than guessed.
                self.assertEqual(summary["skipped_unresolved"], 1)
                self.assertEqual(summary["assigned_by_gmail"], 0)
                self.assertEqual(summary["assigned_to_admin"], 0)
                self.assertNotIn("owner_user_id", {k: v for k, v in matter_store.get_matter(ownerless["id"]).items() if v})

    def test_backfilled_matter_becomes_owner_scoped(self):
        # End-to-end: after backfill the matter is scoped to its new owner and
        # denied to others (the security fix + backfill working together).
        with tempfile.TemporaryDirectory() as data_dir:
            p = self._patches(data_dir)
            with p[0], p[1], p[2]:
                ownerless = matter_store.create_matter(
                    **_matter_kwargs(intake_metadata={"gmail_account": "alice@example.com", "gmail_message_id": "m1", "gmail_attachment_sha256": "h1"})
                )
                matter_store.migrate_ownerless_matter_ownership(
                    user_email_to_id={"alice@example.com": "google:alice"}, admin_user_id="admin_user"
                )
                self.assertEqual(matter_store.get_matter(ownerless["id"], owner_user_id="google:alice")["id"], ownerless["id"])
                self.assertIsNone(matter_store.get_matter(ownerless["id"], owner_user_id="someone_else"))


class OwnershipBackfillOrchestrationTests(unittest.TestCase):
    def test_build_user_email_to_id_casefolds_and_drops_ambiguous(self):
        users = [
            {"id": "google:alice", "email": "Alice@Example.com"},
            {"id": "google:bob", "email": "bob@example.com"},
            # Same email, two ids -> ambiguous, dropped.
            {"id": "google:carol1", "email": "shared@example.com"},
            {"id": "google:carol2", "email": "shared@example.com"},
            {"id": "", "email": "noid@example.com"},  # no id -> skipped
        ]
        with patch.object(ownership_backfill.user_store, "list_users", return_value=users):
            mapping = ownership_backfill.build_user_email_to_id()
        self.assertEqual(mapping["alice@example.com"], "google:alice")
        self.assertEqual(mapping["bob@example.com"], "google:bob")
        self.assertNotIn("shared@example.com", mapping)
        self.assertNotIn("noid@example.com", mapping)

    def test_resolve_admin_prefers_env_then_sole_user(self):
        with patch.dict("os.environ", {ownership_backfill.ADMIN_USERNAME_ENV: "admin@corp.com"}):
            self.assertEqual(ownership_backfill.resolve_admin_user_id(), "admin@corp.com")
        with patch.dict("os.environ", {ownership_backfill.ADMIN_USERNAME_ENV: ""}, clear=False):
            with patch.object(ownership_backfill.user_store, "list_users", return_value=[{"id": "google:solo", "email": "solo@x.com"}]):
                self.assertEqual(ownership_backfill.resolve_admin_user_id(), "google:solo")
            # Multiple users, no env admin -> ambiguous -> empty (caller leaves ownerless).
            with patch.object(ownership_backfill.user_store, "list_users", return_value=[{"id": "a"}, {"id": "b"}]):
                self.assertEqual(ownership_backfill.resolve_admin_user_id(), "")


if __name__ == "__main__":
    unittest.main()
