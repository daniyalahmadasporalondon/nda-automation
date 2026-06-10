import json
import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from nda_automation.checker import load_playbook
from nda_automation import export_service, gmail_integration
from nda_automation import playbook_runtime


class DurableIoTests(unittest.TestCase):
    def test_playbook_json_atomic_write_fsyncs_parent_directory(self):
        with tempfile.TemporaryDirectory() as playbook_dir:
            playbook_path = Path(playbook_dir) / "playbook.json"

            with patch.object(playbook_runtime, "fsync_parent_directory") as fsync_parent_directory:
                playbook_runtime.write_json_atomically({"ok": True}, playbook_path)

        fsync_parent_directory.assert_called_once_with(playbook_path)

    def test_playbook_transaction_recovers_interrupted_active_bundle(self):
        original_playbook = deepcopy(load_playbook())
        changed_playbook = deepcopy(original_playbook)
        next(clause for clause in changed_playbook["clauses"] if clause["id"] == "mutuality")[
            "preferred_position"
        ] = "Recovered active Playbook change."

        with tempfile.TemporaryDirectory() as playbook_dir:
            playbook_path = Path(playbook_dir) / "playbook.json"
            playbook_path.write_text(json.dumps(original_playbook), encoding="utf-8")
            runtime = {
                "version": playbook_runtime.PLAYBOOK_RUNTIME_VERSION,
                **playbook_runtime._active_runtime_from_playbook(
                    changed_playbook,
                    actor="legal-admin",
                    source="save",
                ),
            }
            history = [
                playbook_runtime._history_entry(
                    changed_playbook,
                    action="save",
                    actor="legal-admin",
                    previous_playbook=original_playbook,
                )
            ]

            def crash_after_runtime_replace(source, destination):
                os.replace(source, destination)
                if Path(destination).name == "playbook.runtime.json":
                    raise SystemExit("simulated crash")

            with self.assertRaises(SystemExit):
                playbook_runtime.write_active_playbook_bundle_atomically(
                    changed_playbook,
                    runtime,
                    history,
                    playbook_path=playbook_path,
                    replace_file=crash_after_runtime_replace,
                )

            self.assertTrue(playbook_runtime.transaction_path_for(playbook_path).exists())
            self.assertEqual(json.loads(playbook_path.read_text(encoding="utf-8")), original_playbook)

            recovered = playbook_runtime.recover_playbook_transaction(playbook_path=playbook_path)

            saved_runtime = json.loads(playbook_runtime.runtime_path_for(playbook_path).read_text(encoding="utf-8"))
            saved_history = json.loads(playbook_runtime.history_path_for(playbook_path).read_text(encoding="utf-8"))
            self.assertTrue(recovered)
            self.assertFalse(playbook_runtime.transaction_path_for(playbook_path).exists())
            self.assertEqual(json.loads(playbook_path.read_text(encoding="utf-8")), changed_playbook)
            self.assertEqual(saved_runtime["active_hash"], playbook_runtime.playbook_snapshot_hash(changed_playbook))
            self.assertEqual(saved_history["entries"][0]["action"], "save")

    def test_export_persist_fsyncs_parent_directory_after_replace(self):
        with tempfile.TemporaryDirectory() as exports_dir:
            exports_path = Path(exports_dir)
            with (
                patch.object(export_service, "EXPORTS_DIR", exports_path),
                patch.object(export_service, "fsync_parent_directory") as fsync_parent_directory,
            ):
                saved_path = export_service.persist_export(b"docx", "saved.docx")

            self.assertEqual(saved_path.resolve(), (exports_path / "saved.docx").resolve())
            self.assertEqual(fsync_parent_directory.call_count, 1)
            self.assertEqual(fsync_parent_directory.call_args.args[0].resolve(), (exports_path / "saved.docx").resolve())

    def test_gmail_token_write_fsyncs_parent_directory_after_replace(self):
        with tempfile.TemporaryDirectory() as token_dir:
            token_path = Path(token_dir) / "token.json"
            with patch.object(gmail_integration, "fsync_parent_directory") as fsync_parent_directory:
                gmail_integration._write_token_atomically(token_path, '{"token":"ok"}\n')

            self.assertEqual(json.loads(token_path.read_text(encoding="utf-8")), {"token": "ok"})
            fsync_parent_directory.assert_called_once_with(token_path)


if __name__ == "__main__":
    unittest.main()
