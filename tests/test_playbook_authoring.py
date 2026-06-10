from __future__ import annotations

import ast
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from nda_automation import playbook_authoring, playbook_runtime
from nda_automation.checker import load_playbook


class PlaybookAuthoringTests(unittest.TestCase):
    def test_authoring_module_saves_draft_without_http_handler(self):
        original_playbook = deepcopy(load_playbook())
        draft_playbook = deepcopy(original_playbook)
        mutuality = next(clause for clause in draft_playbook["clauses"] if clause["id"] == "mutuality")
        mutuality["preferred_position"] = "Authoring module owns this draft."

        with tempfile.TemporaryDirectory() as playbook_dir:
            playbook_path = Path(playbook_dir) / "playbook.json"
            playbook_path.write_text(json.dumps(original_playbook), encoding="utf-8")

            response = playbook_authoring.save_playbook_draft(
                {"playbook": draft_playbook, "actor": "legal-admin"},
                playbook_path=playbook_path,
            )

            active_after_save = json.loads(playbook_path.read_text(encoding="utf-8"))
            saved_draft = json.loads(playbook_runtime.draft_path_for(playbook_path).read_text(encoding="utf-8"))

        self.assertEqual(active_after_save, original_playbook)
        self.assertEqual(saved_draft["snapshot"], draft_playbook)
        self.assertEqual(response["draft"]["playbook"], draft_playbook)
        self.assertEqual(response["history"][0]["action"], "draft_save")

    def test_playbook_route_imports_authoring_module_not_private_runtime_helpers(self):
        route_path = Path("nda_automation/routes/playbook.py")
        tree = ast.parse(route_path.read_text(encoding="utf-8"))

        imported_private_runtime_helpers = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.module != "playbook_runtime" and node.module != "..playbook_runtime":
                continue
            imported_private_runtime_helpers.extend(alias.name for alias in node.names if alias.name.startswith("_"))

        self.assertEqual(imported_private_runtime_helpers, [])


if __name__ == "__main__":
    unittest.main()
