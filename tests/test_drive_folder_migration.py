"""Tests for the legacy Drive folder-name migration (plan + apply).

A self-contained fake Drive models a folder/file tree and evaluates the exact
``q=`` clauses the migration's primitives emit (mimeType / name / parents /
trashed and the ``mimeType!=`` form for files), plus ``get_media`` (summary
download) and ``update`` (rename). No network and no real matter store.
"""

from __future__ import annotations

import json
import re
import unittest

from nda_automation import drive_folder_migration, drive_integration

FOLDER_MIME = drive_integration.FOLDER_MIME


def _unescape(value: str) -> str:
    return value.replace("\\'", "'").replace("\\\\", "\\")


def _query_matches(q: str, rec: dict) -> bool:
    if "trashed=false" in q and rec.get("trashed"):
        return False
    name_match = re.search(r"name='((?:[^'\\]|\\.)*)'", q)
    if name_match and _unescape(name_match.group(1)) != rec["name"]:
        return False
    parent_match = re.search(r"'([^']*)' in parents", q)
    if parent_match and parent_match.group(1) != rec["parent"]:
        return False
    if "mimeType!='" in q:
        if rec["is_folder"]:
            return False
    elif "mimeType='" in q:
        if not rec["is_folder"]:
            return False
    return True


class _Exec:
    def __init__(self, value):
        self._value = value

    def execute(self):
        return self._value() if callable(self._value) else self._value


class _FakeFiles:
    def __init__(self, store, *, update_error=None):
        self._store = store
        self._update_error = update_error

    def list(self, *, q, fields, pageSize=100, spaces="drive", pageToken=None):  # noqa: N803
        self._store["list_calls"].append(q)
        matches = [
            {"id": fid, "name": rec["name"]}
            for fid, rec in self._store["files"].items()
            if _query_matches(q, rec)
        ]
        return _Exec({"files": matches, "nextPageToken": ""})

    def get_media(self, *, fileId):  # noqa: N803
        rec = self._store["files"].get(fileId) or {}
        return _Exec(rec.get("content") or b"")

    def update(self, *, fileId, body, fields):  # noqa: N803
        self._store["update_calls"].append({"fileId": fileId, "name": body.get("name")})
        if self._update_error is not None:
            raise self._update_error

        def _do():
            rec = self._store["files"].get(fileId)
            if rec is not None:
                rec["name"] = body["name"]
            return {"id": fileId, "name": body["name"]}

        return _Exec(_do)


class FakeDrive:
    """A stateful fake Drive supporting list / get_media / update."""

    def __init__(self, *, update_error=None):
        self.store = {"files": {}, "_seq": 0, "list_calls": [], "update_calls": []}
        self._update_error = update_error

    def files(self):
        return _FakeFiles(self.store, update_error=self._update_error)

    # --- builders ---
    def add_folder(self, name, parent):
        return self._add(name, parent, is_folder=True, content=None)

    def add_file(self, name, parent, content):
        return self._add(name, parent, is_folder=False, content=content)

    def _add(self, name, parent, *, is_folder, content):
        self.store["_seq"] += 1
        fid = f"f{self.store['_seq']}"
        self.store["files"][fid] = {
            "name": name,
            "parent": parent,
            "is_folder": is_folder,
            "content": content,
            "trashed": False,
        }
        return fid

    def name_of(self, fid):
        return self.store["files"][fid]["name"]


def _summary_bytes(matter_id):
    return json.dumps({"matter_id": matter_id, "counterparty": "x"}).encode("utf-8")


class FolderMigrationPlanTests(unittest.TestCase):
    def _drive_with_matter_folder(self, *, matter_folder_name, matter_id, with_summary=True, counterparty="Acme Fintech"):
        drive = FakeDrive()
        root = drive.add_folder("NDAs", "root")
        cp = drive.add_folder(counterparty, root)
        mf = drive.add_folder(matter_folder_name, cp)
        if with_summary:
            meta = drive.add_folder("metadata", mf)
            drive.add_file("matter_summary.json", meta, _summary_bytes(matter_id))
        return drive, mf

    def test_summary_match_yields_readable_rename(self):
        drive, mf = self._drive_with_matter_folder(
            matter_folder_name="2026-05-30 - Acme - thr_1",
            matter_id="matter_3a8f2b1c9d0e",
        )
        matters = {
            "matter_3a8f2b1c9d0e": {
                "id": "matter_3a8f2b1c9d0e",
                "created_at": "2026-05-30T09:00:00+00:00",
                "document_title": "Mutual NDA",
            }
        }
        plan = drive_folder_migration.plan_folder_renames(
            root_folder_id="root", service=drive, lookup_matter=matters.get
        )
        self.assertTrue(plan["root_found"])
        self.assertEqual(plan["counts"], {"rename": 1})
        entry = plan["entries"][0]
        self.assertEqual(entry["action"], "rename")
        self.assertEqual(entry["new_name"], "2026-05-30 · Mutual NDA · 9d0e")
        self.assertEqual(entry["match_source"], "summary")
        self.assertEqual(entry["folder_id"], mf)
        # The plan is READ-ONLY: no rename calls happened.
        self.assertEqual(drive.store["update_calls"], [])

    def test_already_current_is_left_alone(self):
        drive, _ = self._drive_with_matter_folder(
            matter_folder_name="2026-05-30 · Mutual NDA · 9d0e",
            matter_id="matter_3a8f2b1c9d0e",
        )
        matters = {
            "matter_3a8f2b1c9d0e": {
                "id": "matter_3a8f2b1c9d0e",
                "created_at": "2026-05-30T09:00:00+00:00",
                "document_title": "Mutual NDA",
            }
        }
        plan = drive_folder_migration.plan_folder_renames(
            root_folder_id="root", service=drive, lookup_matter=matters.get
        )
        self.assertEqual(plan["counts"], {"already_current": 1})

    def test_unmatched_folder_is_reported_not_renamed(self):
        drive, _ = self._drive_with_matter_folder(
            matter_folder_name="2026-05-30-ana_orphan", matter_id="matter_gone", with_summary=False
        )
        plan = drive_folder_migration.plan_folder_renames(
            root_folder_id="root", service=drive, lookup_matter=lambda _mid: None
        )
        self.assertEqual(plan["counts"], {"unmatched": 1})
        self.assertEqual(plan["entries"][0]["action"], "unmatched")

    def test_name_fallback_is_review_only_never_auto_renamed(self):
        # A folder with no summary that only matches by parsed name must NOT be
        # auto-renamed -> it is surfaced for human review.
        drive, _ = self._drive_with_matter_folder(
            matter_folder_name="2026-05-30-ana_legacykey", matter_id="x", with_summary=False
        )
        matters = {
            "matter_legacykey": {
                "id": "matter_legacykey",
                "created_at": "2026-05-30T09:00:00+00:00",
                "document_title": "Old NDA",
            }
        }
        plan = drive_folder_migration.plan_folder_renames(
            root_folder_id="root", service=drive, lookup_matter=matters.get
        )
        entry = plan["entries"][0]
        self.assertEqual(entry["action"], "review")
        self.assertEqual(entry["match_source"], "name")
        self.assertEqual(entry["new_name"], "2026-05-30 · Old NDA · ykey")
        # apply() must skip review entries entirely.
        result = drive_folder_migration.apply_folder_renames(plan["entries"], service=drive)
        self.assertEqual(result["renamed"], 0)
        self.assertEqual(drive.store["update_calls"], [])

    def test_short_name_tail_is_not_guessed(self):
        # A tiny trailing token (e.g. "_1") is too ambiguous to resolve, so even if
        # a matter with that id exists the folder is left unmatched.
        drive, _ = self._drive_with_matter_folder(
            matter_folder_name="Some Folder_1", matter_id="x", with_summary=False
        )
        matters = {"1": {"id": "1", "created_at": "2026-05-30T09:00:00+00:00", "document_title": "X"}}
        plan = drive_folder_migration.plan_folder_renames(
            root_folder_id="root", service=drive, lookup_matter=matters.get
        )
        self.assertEqual(plan["entries"][0]["action"], "unmatched")

    def test_invalid_summary_falls_through_to_unmatched(self):
        drive = FakeDrive()
        root = drive.add_folder("NDAs", "root")
        cp = drive.add_folder("Acme", root)
        mf = drive.add_folder("2026-05-30 - x - y", cp)
        meta = drive.add_folder("metadata", mf)
        drive.add_file("matter_summary.json", meta, b"not json {{{")
        plan = drive_folder_migration.plan_folder_renames(
            root_folder_id="root", service=drive, lookup_matter=lambda _mid: None
        )
        self.assertEqual(plan["entries"][0]["action"], "unmatched")

    def test_distinct_refs_yield_two_clean_renames(self):
        # Same date + title but DIFFERENT ref codes -> no false collision.
        drive = FakeDrive()
        root = drive.add_folder("NDAs", "root")
        cp = drive.add_folder("Acme Fintech", root)
        matters = {}
        for folder_name, mid in (
            ("2026-05-30 - Acme - one", "matter_111111110011"),
            ("2026-05-30 - Acme - two", "matter_222222220022"),
        ):
            mf = drive.add_folder(folder_name, cp)
            meta = drive.add_folder("metadata", mf)
            drive.add_file("matter_summary.json", meta, _summary_bytes(mid))
            matters[mid] = {"id": mid, "created_at": "2026-05-30T09:00:00+00:00", "document_title": "NDA"}
        plan = drive_folder_migration.plan_folder_renames(
            root_folder_id="root", service=drive, lookup_matter=matters.get
        )
        self.assertEqual(sorted(e["action"] for e in plan["entries"]), ["rename", "rename"])
        self.assertEqual({e["new_name"] for e in plan["entries"]}, {"2026-05-30 · NDA · 0011", "2026-05-30 · NDA · 0022"})

    def test_two_folders_colliding_on_ref_mark_second_conflict(self):
        # Two ids ending in the SAME 4 alnum chars -> same ref -> same new name.
        # The second folder to claim that name is a conflict (not renamed).
        drive = FakeDrive()
        root = drive.add_folder("NDAs", "root")
        cp = drive.add_folder("Acme Fintech", root)
        matters = {}
        for folder_name, mid in (
            ("2026-05-30 - Acme - aaa", "matter_1111aaaa9999"),
            ("2026-05-30 - Acme - bbb", "matter_2222bbbb9999"),
        ):
            mf = drive.add_folder(folder_name, cp)
            meta = drive.add_folder("metadata", mf)
            drive.add_file("matter_summary.json", meta, _summary_bytes(mid))
            matters[mid] = {"id": mid, "created_at": "2026-05-30T09:00:00+00:00", "document_title": "NDA"}
        plan = drive_folder_migration.plan_folder_renames(
            root_folder_id="root", service=drive, lookup_matter=matters.get
        )
        actions = sorted(e["action"] for e in plan["entries"])
        self.assertEqual(actions, ["conflict", "rename"])

    def test_true_collision_against_existing_folder(self):
        drive = FakeDrive()
        root = drive.add_folder("NDAs", "root")
        cp = drive.add_folder("Acme Fintech", root)
        # An existing folder ALREADY bears the target name.
        drive.add_folder("2026-05-30 · Mutual NDA · 9d0e", cp)
        mf = drive.add_folder("2026-05-30 - Acme - thr_1", cp)
        meta = drive.add_folder("metadata", mf)
        drive.add_file("matter_summary.json", meta, _summary_bytes("matter_3a8f2b1c9d0e"))
        matters = {
            "matter_3a8f2b1c9d0e": {"id": "matter_3a8f2b1c9d0e", "created_at": "2026-05-30T09:00:00+00:00", "document_title": "Mutual NDA"},
        }
        plan = drive_folder_migration.plan_folder_renames(
            root_folder_id="root", service=drive, lookup_matter=matters.get
        )
        by_action = {e["action"] for e in plan["entries"]}
        self.assertIn("conflict", by_action)
        conflict = next(e for e in plan["entries"] if e["action"] == "conflict")
        self.assertEqual(conflict["old_name"], "2026-05-30 - Acme - thr_1")

    def test_root_not_found(self):
        drive = FakeDrive()  # empty -> no NDAs folder
        plan = drive_folder_migration.plan_folder_renames(
            root_folder_id="root", service=drive, lookup_matter=lambda _mid: None
        )
        self.assertFalse(plan["root_found"])
        self.assertEqual(plan["entries"], [])


class FolderMigrationApplyTests(unittest.TestCase):
    def _matter_plan(self):
        drive = FakeDrive()
        root = drive.add_folder("NDAs", "root")
        cp = drive.add_folder("Acme Fintech", root)
        mf = drive.add_folder("2026-05-30 - Acme - thr_1", cp)
        meta = drive.add_folder("metadata", mf)
        drive.add_file("matter_summary.json", meta, _summary_bytes("matter_3a8f2b1c9d0e"))
        matters = {
            "matter_3a8f2b1c9d0e": {"id": "matter_3a8f2b1c9d0e", "created_at": "2026-05-30T09:00:00+00:00", "document_title": "Mutual NDA"},
        }
        plan = drive_folder_migration.plan_folder_renames(
            root_folder_id="root", service=drive, lookup_matter=matters.get
        )
        return drive, mf, plan

    def test_apply_renames_only_rename_entries(self):
        drive, mf, plan = self._matter_plan()
        result = drive_folder_migration.apply_folder_renames(plan["entries"], service=drive)
        self.assertEqual(result["renamed"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(drive.name_of(mf), "2026-05-30 · Mutual NDA · 9d0e")

    def test_apply_skips_non_rename_actions(self):
        drive = FakeDrive()
        entries = [
            {"action": "unmatched", "folder_id": "f1", "new_name": "x", "counterparty": "c", "old_name": "o"},
            {"action": "already_current", "folder_id": "f2", "new_name": "y", "counterparty": "c", "old_name": "o"},
            {"action": "conflict", "folder_id": "f3", "new_name": "z", "counterparty": "c", "old_name": "o"},
        ]
        result = drive_folder_migration.apply_folder_renames(entries, service=drive)
        self.assertEqual(result["renamed"], 0)
        self.assertEqual(drive.store["update_calls"], [])

    def test_apply_captures_per_folder_failure_and_continues(self):
        drive = FakeDrive(update_error=RuntimeError("boom"))
        entries = [
            {"action": "rename", "folder_id": "f1", "new_name": "n1", "counterparty": "c", "old_name": "o1"},
            {"action": "rename", "folder_id": "f2", "new_name": "n2", "counterparty": "c", "old_name": "o2"},
        ]
        result = drive_folder_migration.apply_folder_renames(entries, service=drive)
        self.assertEqual(result["renamed"], 0)
        self.assertEqual(result["failed"], 2)
        self.assertFalse(result["results"][0]["ok"])


class FolderMigrationFormatTests(unittest.TestCase):
    def test_format_plan_handles_empty_and_missing_root(self):
        self.assertIn("nothing to migrate", drive_folder_migration.format_plan({"root_found": False}))
        self.assertIn(
            "nothing to migrate",
            drive_folder_migration.format_plan({"root_found": True, "entries": [], "counts": {}}),
        )

    def test_cli_refuses_empty_owner_without_optin(self):
        # Guards the cross-tenant wildcard: empty --owner must refuse (exit 2)
        # unless --allow-ownerless is given. Returns before any Drive call.
        self.assertEqual(drive_folder_migration.main([]), 2)
        self.assertEqual(drive_folder_migration.main(["--apply"]), 2)

    def test_title_separator_is_stripped(self):
        # A title containing the middle-dot separator is sanitised to a hyphen so
        # it cannot masquerade as a field boundary.
        name = drive_integration.derive_matter_folder_name(
            {"id": "matter_aabbccdd1234", "created_at": "2026-06-07T09:00:00+00:00", "document_title": "Acme · Secret NDA"},
            "matter_aabbccdd1234",
            "Acme",
        )
        self.assertEqual(name, "2026-06-07 · Acme - Secret NDA · 1234")

    def test_format_plan_lists_rename_with_match_source(self):
        plan = {
            "root_found": True,
            "counts": {"rename": 1},
            "entries": [
                {
                    "action": "rename",
                    "counterparty": "Acme Fintech",
                    "old_name": "2026-05-30 - Acme - thr_1",
                    "new_name": "2026-05-30 · Mutual NDA · 9d0e",
                    "match_source": "summary",
                }
            ],
        }
        text = drive_folder_migration.format_plan(plan)
        self.assertIn("2026-05-30 · Mutual NDA · 9d0e", text)
        self.assertIn("matched by summary", text)


if __name__ == "__main__":
    unittest.main()
