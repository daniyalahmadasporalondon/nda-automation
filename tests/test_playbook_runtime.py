import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from nda_automation.checker import load_playbook
from nda_automation.routes import playbook as playbook_routes


class _JsonHandler:
    def __init__(self, payload: dict | None = None):
        self.payload = payload
        self.status = None
        self.response = None
        self.send_body = None

    def _read_json_payload(self):
        return self.payload

    def _send_json(self, payload, *, status=200, send_body=True):
        self.status = status
        self.response = payload
        self.send_body = send_body


class PlaybookRuntimeTests(unittest.TestCase):
    def test_playbook_snapshot_hash_is_stable_for_json_key_order(self):
        first = {"name": "Policy", "clauses": [{"id": "one", "rules": {"b": 2, "a": 1}}]}
        second = {"clauses": [{"rules": {"a": 1, "b": 2}, "id": "one"}], "name": "Policy"}

        self.assertEqual(
            playbook_routes.playbook_snapshot_hash(first),
            playbook_routes.playbook_snapshot_hash(second),
        )
        self.assertRegex(playbook_routes.playbook_snapshot_hash(first), r"^sha256:[a-f0-9]{64}$")

    def test_active_runtime_bootstrap_creates_sidecar_metadata(self):
        playbook = deepcopy(load_playbook())

        with tempfile.TemporaryDirectory() as playbook_dir:
            playbook_path = Path(playbook_dir) / "playbook.json"
            playbook_path.write_text(json.dumps(playbook), encoding="utf-8")

            runtime = playbook_routes.ensure_active_playbook_runtime(playbook_path=playbook_path)

            runtime_path = playbook_routes.runtime_path_for(playbook_path)
            saved_runtime = json.loads(runtime_path.read_text(encoding="utf-8"))

        self.assertEqual(runtime["version"], playbook_routes.PLAYBOOK_RUNTIME_VERSION)
        self.assertEqual(runtime["active_hash"], playbook_routes.playbook_snapshot_hash(playbook))
        self.assertEqual(saved_runtime["active_hash"], runtime["active_hash"])
        self.assertEqual(runtime["published_by"], "system")
        self.assertEqual(runtime["source"], "bootstrap")
        self.assertEqual(runtime["playbook_name"], playbook["name"])
        self.assertEqual(runtime["playbook_version"], playbook["version"])
        self.assertRegex(runtime["active_version_id"], r"^pbv_")

    def test_api_get_returns_backwards_compatible_active_metadata(self):
        playbook = deepcopy(load_playbook())

        with tempfile.TemporaryDirectory() as playbook_dir:
            playbook_path = Path(playbook_dir) / "playbook.json"
            playbook_path.write_text(json.dumps(playbook), encoding="utf-8")
            handler = _JsonHandler()

            playbook_routes.handle_playbook_get(handler, playbook_path=playbook_path)

        self.assertEqual(handler.status, 200)
        self.assertEqual(handler.response["playbook"]["name"], playbook["name"])
        self.assertEqual(handler.response["active"]["playbook"]["name"], playbook["name"])
        self.assertEqual(
            handler.response["active"]["metadata"]["active_hash"],
            playbook_routes.playbook_snapshot_hash(playbook),
        )
        self.assertIsNone(handler.response["draft"])

    def test_playbook_save_updates_active_runtime_sidecar(self):
        original_playbook = deepcopy(load_playbook())
        changed_playbook = deepcopy(original_playbook)
        mutuality = next(clause for clause in changed_playbook["clauses"] if clause["id"] == "mutuality")
        mutuality["preferred_position"] = "Runtime metadata should track this active save."

        with tempfile.TemporaryDirectory() as playbook_dir:
            playbook_path = Path(playbook_dir) / "playbook.json"
            playbook_path.write_text(json.dumps(original_playbook), encoding="utf-8")
            handler = _JsonHandler({"playbook": changed_playbook, "actor": "legal-admin"})

            playbook_routes.handle_playbook_save(handler, playbook_path=playbook_path)

            saved_runtime = json.loads(playbook_routes.runtime_path_for(playbook_path).read_text(encoding="utf-8"))

        self.assertEqual(handler.status, 200)
        self.assertEqual(handler.response["active"]["metadata"]["source"], "save")
        self.assertEqual(handler.response["active"]["metadata"]["published_by"], "legal-admin")
        self.assertEqual(saved_runtime["active_hash"], playbook_routes.playbook_snapshot_hash(changed_playbook))
        self.assertEqual(saved_runtime["source"], "save")


if __name__ == "__main__":
    unittest.main()
