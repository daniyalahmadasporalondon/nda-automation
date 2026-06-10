import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from nda_automation.checker import load_playbook
from nda_automation.routes import playbook as playbook_routes
from nda_automation import playbook_runtime
from nda_automation import server as server_module


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
            playbook_runtime.playbook_snapshot_hash(first),
            playbook_runtime.playbook_snapshot_hash(second),
        )
        self.assertRegex(playbook_runtime.playbook_snapshot_hash(first), r"^sha256:[a-f0-9]{64}$")

    def test_active_runtime_bootstrap_creates_sidecar_metadata(self):
        playbook = deepcopy(load_playbook())

        with tempfile.TemporaryDirectory() as playbook_dir:
            playbook_path = Path(playbook_dir) / "playbook.json"
            playbook_path.write_text(json.dumps(playbook), encoding="utf-8")

            runtime = playbook_runtime.ensure_active_playbook_runtime(playbook_path=playbook_path)

            runtime_path = playbook_runtime.runtime_path_for(playbook_path)
            saved_runtime = json.loads(runtime_path.read_text(encoding="utf-8"))

        self.assertEqual(runtime["version"], playbook_runtime.PLAYBOOK_RUNTIME_VERSION)
        self.assertEqual(runtime["active_hash"], playbook_runtime.playbook_snapshot_hash(playbook))
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
            playbook_runtime.playbook_snapshot_hash(playbook),
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

            saved_runtime = json.loads(playbook_runtime.runtime_path_for(playbook_path).read_text(encoding="utf-8"))

        self.assertEqual(handler.status, 200)
        self.assertEqual(handler.response["active"]["metadata"]["source"], "save")
        self.assertEqual(handler.response["active"]["metadata"]["published_by"], "legal-admin")
        self.assertEqual(saved_runtime["active_hash"], playbook_runtime.playbook_snapshot_hash(changed_playbook))
        self.assertEqual(saved_runtime["source"], "save")

    def test_playbook_draft_save_validates_without_changing_active_playbook(self):
        original_playbook = deepcopy(load_playbook())
        draft_playbook = deepcopy(original_playbook)
        mutuality = next(clause for clause in draft_playbook["clauses"] if clause["id"] == "mutuality")
        mutuality["preferred_position"] = "Draft-only Mutuality policy."

        with tempfile.TemporaryDirectory() as playbook_dir:
            playbook_path = Path(playbook_dir) / "playbook.json"
            playbook_path.write_text(json.dumps(original_playbook), encoding="utf-8")
            runtime = playbook_runtime.ensure_active_playbook_runtime(playbook_path=playbook_path)
            handler = _JsonHandler({
                "playbook": draft_playbook,
                "actor": "legal-admin",
                "summary": "Draft Mutuality change.",
                "expected_base_active_hash": runtime["active_hash"],
            })

            playbook_routes.handle_playbook_draft_save(handler, playbook_path=playbook_path)

            active_after_save = json.loads(playbook_path.read_text(encoding="utf-8"))
            saved_draft = json.loads(playbook_runtime.draft_path_for(playbook_path).read_text(encoding="utf-8"))
            saved_runtime = json.loads(playbook_runtime.runtime_path_for(playbook_path).read_text(encoding="utf-8"))

        self.assertEqual(handler.status, 200)
        self.assertEqual(active_after_save, original_playbook)
        self.assertEqual(saved_draft["snapshot"], draft_playbook)
        self.assertEqual(saved_draft["summary"], "Draft Mutuality change.")
        self.assertEqual(saved_draft["changed_clause_ids"], ["mutuality"])
        self.assertEqual(saved_runtime["draft_id"], saved_draft["draft_id"])
        self.assertEqual(handler.response["draft"]["metadata"]["draft_id"], saved_draft["draft_id"])
        self.assertEqual(handler.response["draft"]["playbook"], draft_playbook)
        self.assertEqual(handler.response["history"][0]["action"], "draft_save")
        self.assertEqual(handler.response["history"][0]["draft_id"], saved_draft["draft_id"])

    def test_playbook_api_get_returns_saved_draft_snapshot(self):
        original_playbook = deepcopy(load_playbook())
        draft_playbook = deepcopy(original_playbook)
        term = next(clause for clause in draft_playbook["clauses"] if clause["id"] == "term_and_survival")
        term["preferred_position"] = "Draft-only survival policy."

        with tempfile.TemporaryDirectory() as playbook_dir:
            playbook_path = Path(playbook_dir) / "playbook.json"
            playbook_path.write_text(json.dumps(original_playbook), encoding="utf-8")
            save_handler = _JsonHandler({"playbook": draft_playbook, "actor": "legal-admin"})
            get_handler = _JsonHandler()

            playbook_routes.handle_playbook_draft_save(save_handler, playbook_path=playbook_path)
            playbook_routes.handle_playbook_get(get_handler, playbook_path=playbook_path)

        self.assertEqual(get_handler.status, 200)
        self.assertEqual(get_handler.response["playbook"], original_playbook)
        self.assertEqual(get_handler.response["active"]["playbook"], original_playbook)
        self.assertEqual(get_handler.response["draft"]["playbook"], draft_playbook)
        self.assertEqual(get_handler.response["draft"]["metadata"]["draft_id"], save_handler.response["draft"]["metadata"]["draft_id"])

    def test_playbook_draft_save_rejects_active_base_conflict(self):
        original_playbook = deepcopy(load_playbook())
        draft_playbook = deepcopy(original_playbook)
        mutuality = next(clause for clause in draft_playbook["clauses"] if clause["id"] == "mutuality")
        mutuality["preferred_position"] = "This should not be written."

        with tempfile.TemporaryDirectory() as playbook_dir:
            playbook_path = Path(playbook_dir) / "playbook.json"
            playbook_path.write_text(json.dumps(original_playbook), encoding="utf-8")
            handler = _JsonHandler({
                "playbook": draft_playbook,
                "expected_base_active_hash": "sha256:" + "0" * 64,
            })

            playbook_routes.handle_playbook_draft_save(handler, playbook_path=playbook_path)

        self.assertEqual(handler.status, 409)
        self.assertEqual(handler.response["code"], "playbook_conflict")
        self.assertFalse(playbook_runtime.draft_path_for(playbook_path).exists())

    def test_playbook_draft_discard_removes_draft_sidecar_and_runtime_fields(self):
        original_playbook = deepcopy(load_playbook())
        draft_playbook = deepcopy(original_playbook)
        signatures = next(clause for clause in draft_playbook["clauses"] if clause["id"] == "signatures")
        signatures["preferred_position"] = "Draft-only signature policy."

        with tempfile.TemporaryDirectory() as playbook_dir:
            playbook_path = Path(playbook_dir) / "playbook.json"
            playbook_path.write_text(json.dumps(original_playbook), encoding="utf-8")
            save_handler = _JsonHandler({"playbook": draft_playbook, "actor": "legal-admin"})
            playbook_routes.handle_playbook_draft_save(save_handler, playbook_path=playbook_path)
            draft_id = save_handler.response["draft"]["metadata"]["draft_id"]
            discard_handler = _JsonHandler({"draft_id": draft_id, "actor": "legal-admin"})

            playbook_routes.handle_playbook_draft_discard(discard_handler, playbook_path=playbook_path)

            saved_runtime = json.loads(playbook_runtime.runtime_path_for(playbook_path).read_text(encoding="utf-8"))

        self.assertEqual(discard_handler.status, 200)
        self.assertFalse(playbook_runtime.draft_path_for(playbook_path).exists())
        self.assertNotIn("draft_id", saved_runtime)
        self.assertIsNone(discard_handler.response["draft"])
        self.assertEqual(discard_handler.response["history"][0]["action"], "draft_discard")
        self.assertEqual(discard_handler.response["history"][0]["draft_id"], draft_id)

    def test_playbook_draft_discard_rejects_draft_id_conflict(self):
        original_playbook = deepcopy(load_playbook())
        draft_playbook = deepcopy(original_playbook)
        mutuality = next(clause for clause in draft_playbook["clauses"] if clause["id"] == "mutuality")
        mutuality["preferred_position"] = "Draft-only Mutuality policy."

        with tempfile.TemporaryDirectory() as playbook_dir:
            playbook_path = Path(playbook_dir) / "playbook.json"
            playbook_path.write_text(json.dumps(original_playbook), encoding="utf-8")
            save_handler = _JsonHandler({"playbook": draft_playbook, "actor": "legal-admin"})
            conflict_handler = _JsonHandler({"draft_id": "pbd_wrong"})

            playbook_routes.handle_playbook_draft_save(save_handler, playbook_path=playbook_path)
            playbook_routes.handle_playbook_draft_discard(conflict_handler, playbook_path=playbook_path)
            draft_still_exists = playbook_runtime.draft_path_for(playbook_path).exists()

        self.assertEqual(conflict_handler.status, 409)
        self.assertEqual(conflict_handler.response["code"], "playbook_draft_conflict")
        self.assertTrue(draft_still_exists)

    def test_playbook_publish_promotes_draft_and_clears_draft_state(self):
        original_playbook = deepcopy(load_playbook())
        draft_playbook = deepcopy(original_playbook)
        mutuality = next(clause for clause in draft_playbook["clauses"] if clause["id"] == "mutuality")
        mutuality["preferred_position"] = "Published Mutuality draft."

        with tempfile.TemporaryDirectory() as playbook_dir:
            playbook_path = Path(playbook_dir) / "playbook.json"
            playbook_path.write_text(json.dumps(original_playbook), encoding="utf-8")
            runtime = playbook_runtime.ensure_active_playbook_runtime(playbook_path=playbook_path)
            save_handler = _JsonHandler({
                "playbook": draft_playbook,
                "actor": "legal-admin",
                "expected_base_active_hash": runtime["active_hash"],
            })
            playbook_routes.handle_playbook_draft_save(save_handler, playbook_path=playbook_path)
            draft_id = save_handler.response["draft"]["metadata"]["draft_id"]
            publish_handler = _JsonHandler({
                "draft_id": draft_id,
                "actor": "legal-admin",
                "expected_active_hash": runtime["active_hash"],
            })

            playbook_routes.handle_playbook_publish(publish_handler, playbook_path=playbook_path)

            active_after_publish = json.loads(playbook_path.read_text(encoding="utf-8"))
            saved_runtime = json.loads(playbook_runtime.runtime_path_for(playbook_path).read_text(encoding="utf-8"))
            draft_exists = playbook_runtime.draft_path_for(playbook_path).exists()

        self.assertEqual(publish_handler.status, 200)
        self.assertEqual(active_after_publish, draft_playbook)
        self.assertFalse(draft_exists)
        self.assertEqual(saved_runtime["source"], "publish")
        self.assertEqual(saved_runtime["published_by"], "legal-admin")
        self.assertEqual(saved_runtime["active_hash"], playbook_runtime.playbook_snapshot_hash(draft_playbook))
        self.assertNotIn("draft_id", saved_runtime)
        self.assertEqual(publish_handler.response["draft"], None)
        self.assertEqual(publish_handler.response["history"][0]["action"], "publish")
        self.assertEqual(publish_handler.response["history"][0]["draft_id"], draft_id)
        self.assertEqual(publish_handler.response["history"][0]["changed_clause_ids"], ["mutuality"])

    def test_playbook_publish_rejects_active_base_conflict(self):
        original_playbook = deepcopy(load_playbook())
        draft_playbook = deepcopy(original_playbook)
        mutuality = next(clause for clause in draft_playbook["clauses"] if clause["id"] == "mutuality")
        mutuality["preferred_position"] = "Should not publish."

        with tempfile.TemporaryDirectory() as playbook_dir:
            playbook_path = Path(playbook_dir) / "playbook.json"
            playbook_path.write_text(json.dumps(original_playbook), encoding="utf-8")
            save_handler = _JsonHandler({"playbook": draft_playbook, "actor": "legal-admin"})
            playbook_routes.handle_playbook_draft_save(save_handler, playbook_path=playbook_path)
            draft_id = save_handler.response["draft"]["metadata"]["draft_id"]
            publish_handler = _JsonHandler({
                "draft_id": draft_id,
                "expected_active_hash": "sha256:" + "0" * 64,
            })

            playbook_routes.handle_playbook_publish(publish_handler, playbook_path=playbook_path)

            active_after_publish_attempt = json.loads(playbook_path.read_text(encoding="utf-8"))
            draft_still_exists = playbook_runtime.draft_path_for(playbook_path).exists()

        self.assertEqual(publish_handler.status, 409)
        self.assertEqual(publish_handler.response["code"], "playbook_conflict")
        self.assertEqual(active_after_publish_attempt, original_playbook)
        self.assertTrue(draft_still_exists)

    def test_playbook_publish_rejects_draft_when_active_changed_after_draft_save(self):
        original_playbook = deepcopy(load_playbook())
        draft_playbook = deepcopy(original_playbook)
        later_active_playbook = deepcopy(original_playbook)
        next(clause for clause in draft_playbook["clauses"] if clause["id"] == "mutuality")[
            "preferred_position"
        ] = "Stale draft policy."
        next(clause for clause in later_active_playbook["clauses"] if clause["id"] == "signatures")[
            "preferred_position"
        ] = "New active signature policy."

        with tempfile.TemporaryDirectory() as playbook_dir:
            playbook_path = Path(playbook_dir) / "playbook.json"
            playbook_path.write_text(json.dumps(original_playbook), encoding="utf-8")
            save_draft_handler = _JsonHandler({"playbook": draft_playbook, "actor": "legal-admin"})
            playbook_routes.handle_playbook_draft_save(save_draft_handler, playbook_path=playbook_path)
            draft_id = save_draft_handler.response["draft"]["metadata"]["draft_id"]
            save_active_handler = _JsonHandler({"playbook": later_active_playbook, "actor": "other-admin"})
            playbook_routes.handle_playbook_save(save_active_handler, playbook_path=playbook_path)
            publish_handler = _JsonHandler({"draft_id": draft_id, "actor": "legal-admin"})

            playbook_routes.handle_playbook_publish(publish_handler, playbook_path=playbook_path)

            active_after_publish_attempt = json.loads(playbook_path.read_text(encoding="utf-8"))
            draft_still_exists = playbook_runtime.draft_path_for(playbook_path).exists()

        self.assertEqual(publish_handler.status, 409)
        self.assertEqual(publish_handler.response["code"], "playbook_draft_base_conflict")
        self.assertEqual(active_after_publish_attempt, later_active_playbook)
        self.assertTrue(draft_still_exists)

    def test_playbook_publish_rejects_draft_id_conflict(self):
        original_playbook = deepcopy(load_playbook())
        draft_playbook = deepcopy(original_playbook)
        mutuality = next(clause for clause in draft_playbook["clauses"] if clause["id"] == "mutuality")
        mutuality["preferred_position"] = "Should not publish."

        with tempfile.TemporaryDirectory() as playbook_dir:
            playbook_path = Path(playbook_dir) / "playbook.json"
            playbook_path.write_text(json.dumps(original_playbook), encoding="utf-8")
            save_handler = _JsonHandler({"playbook": draft_playbook, "actor": "legal-admin"})
            publish_handler = _JsonHandler({"draft_id": "pbd_wrong"})

            playbook_routes.handle_playbook_draft_save(save_handler, playbook_path=playbook_path)
            playbook_routes.handle_playbook_publish(publish_handler, playbook_path=playbook_path)

            active_after_publish_attempt = json.loads(playbook_path.read_text(encoding="utf-8"))
            draft_still_exists = playbook_runtime.draft_path_for(playbook_path).exists()

        self.assertEqual(publish_handler.status, 409)
        self.assertEqual(publish_handler.response["code"], "playbook_draft_conflict")
        self.assertEqual(active_after_publish_attempt, original_playbook)
        self.assertTrue(draft_still_exists)

    def test_playbook_publish_allows_direct_playbook_when_no_draft_exists(self):
        original_playbook = deepcopy(load_playbook())
        publish_playbook = deepcopy(original_playbook)
        signatures = next(clause for clause in publish_playbook["clauses"] if clause["id"] == "signatures")
        signatures["preferred_position"] = "Directly published signature policy."

        with tempfile.TemporaryDirectory() as playbook_dir:
            playbook_path = Path(playbook_dir) / "playbook.json"
            playbook_path.write_text(json.dumps(original_playbook), encoding="utf-8")
            handler = _JsonHandler({"playbook": publish_playbook, "actor": "legal-admin"})

            playbook_routes.handle_playbook_publish(handler, playbook_path=playbook_path)

            active_after_publish = json.loads(playbook_path.read_text(encoding="utf-8"))
            saved_runtime = json.loads(playbook_runtime.runtime_path_for(playbook_path).read_text(encoding="utf-8"))

        self.assertEqual(handler.status, 200)
        self.assertEqual(active_after_publish, publish_playbook)
        self.assertEqual(saved_runtime["source"], "publish")
        self.assertEqual(handler.response["history"][0]["action"], "publish")
        self.assertEqual(handler.response["history"][0]["changed_clause_ids"], ["signatures"])

    def test_playbook_publish_rejects_direct_playbook_when_draft_exists_without_id(self):
        original_playbook = deepcopy(load_playbook())
        draft_playbook = deepcopy(original_playbook)
        direct_playbook = deepcopy(original_playbook)
        next(clause for clause in draft_playbook["clauses"] if clause["id"] == "mutuality")[
            "preferred_position"
        ] = "Draft-only Mutuality policy."
        next(clause for clause in direct_playbook["clauses"] if clause["id"] == "signatures")[
            "preferred_position"
        ] = "Direct publish should wait."

        with tempfile.TemporaryDirectory() as playbook_dir:
            playbook_path = Path(playbook_dir) / "playbook.json"
            playbook_path.write_text(json.dumps(original_playbook), encoding="utf-8")
            save_handler = _JsonHandler({"playbook": draft_playbook, "actor": "legal-admin"})
            publish_handler = _JsonHandler({"playbook": direct_playbook, "actor": "legal-admin"})

            playbook_routes.handle_playbook_draft_save(save_handler, playbook_path=playbook_path)
            playbook_routes.handle_playbook_publish(publish_handler, playbook_path=playbook_path)

            active_after_publish_attempt = json.loads(playbook_path.read_text(encoding="utf-8"))

        self.assertEqual(publish_handler.status, 409)
        self.assertEqual(publish_handler.response["code"], "playbook_draft_exists")
        self.assertEqual(active_after_publish_attempt, original_playbook)

    def test_playbook_draft_routes_are_registered(self):
        self.assertIn("/api/playbook/draft", server_module._GET_EXACT_ROUTES)
        self.assertIn("/api/playbook/draft", server_module._POST_EXACT_ROUTES)
        self.assertIn("/api/playbook/validate-draft", server_module._POST_EXACT_ROUTES)
        self.assertIn("/api/playbook/discard-draft", server_module._POST_EXACT_ROUTES)
        self.assertIn("/api/playbook/publish", server_module._POST_EXACT_ROUTES)

    def test_playbook_validate_draft_accepts_valid_playbook_without_persisting(self):
        original_playbook = deepcopy(load_playbook())
        candidate_playbook = deepcopy(original_playbook)
        mutuality = next(clause for clause in candidate_playbook["clauses"] if clause["id"] == "mutuality")
        mutuality["preferred_position"] = "Validate-only Mutuality policy."

        with tempfile.TemporaryDirectory() as playbook_dir:
            playbook_path = Path(playbook_dir) / "playbook.json"
            playbook_path.write_text(json.dumps(original_playbook), encoding="utf-8")
            handler = _JsonHandler({"playbook": candidate_playbook})

            playbook_routes.handle_playbook_validate_draft(handler, playbook_path=playbook_path)

            active_after_validate = json.loads(playbook_path.read_text(encoding="utf-8"))
            draft_exists = playbook_runtime.draft_path_for(playbook_path).exists()

        self.assertEqual(handler.status, 200)
        self.assertTrue(handler.response["valid"])
        self.assertEqual(handler.response["errors"], [])
        # Validation must never touch the active Playbook or create a draft sidecar.
        self.assertEqual(active_after_validate, original_playbook)
        self.assertFalse(draft_exists)

    def test_playbook_validate_draft_returns_structured_errors_for_invalid_playbook(self):
        original_playbook = deepcopy(load_playbook())
        invalid_playbook = deepcopy(original_playbook)
        governing_law = next(clause for clause in invalid_playbook["clauses"] if clause["id"] == "governing_law")
        # An unapproved preferred_law is a rule-level error the editor should see before publish.
        governing_law["preferred_law"] = "Atlantis"

        with tempfile.TemporaryDirectory() as playbook_dir:
            playbook_path = Path(playbook_dir) / "playbook.json"
            playbook_path.write_text(json.dumps(original_playbook), encoding="utf-8")
            handler = _JsonHandler({"playbook": invalid_playbook})

            playbook_routes.handle_playbook_validate_draft(handler, playbook_path=playbook_path)

        self.assertEqual(handler.status, 200)
        self.assertFalse(handler.response["valid"])
        errors = handler.response["errors"]
        self.assertTrue(errors)
        # Every error is a structured record the editor can render per clause/field.
        for error in errors:
            self.assertEqual(set(error.keys()), {"location", "clause", "field", "message", "severity"})
            self.assertIsInstance(error["message"], str)
            self.assertTrue(error["message"])
            self.assertEqual(error["severity"], "error")
        # The preferred_law error is attributed to the governing_law clause + field.
        preferred_law_error = next(
            (error for error in errors if "preferred_law must be approved" in error["message"]),
            None,
        )
        self.assertIsNotNone(preferred_law_error, errors)
        self.assertEqual(preferred_law_error["clause"], "governing_law")
        self.assertEqual(preferred_law_error["field"], "preferred_law")
        self.assertEqual(preferred_law_error["location"], "governing_law.preferred_law")

    def test_playbook_validate_draft_deduplicates_and_locates_errors_across_clauses(self):
        original_playbook = deepcopy(load_playbook())
        invalid_playbook = deepcopy(original_playbook)
        # Two independent problems in two clauses, plus a missing top-level field.
        next(clause for clause in invalid_playbook["clauses"] if clause["id"] == "term_and_survival")[
            "max_term_years"
        ] = 99
        next(clause for clause in invalid_playbook["clauses"] if clause["id"] == "mutuality")["requirement"] = ""
        invalid_playbook.pop("version")

        with tempfile.TemporaryDirectory() as playbook_dir:
            playbook_path = Path(playbook_dir) / "playbook.json"
            playbook_path.write_text(json.dumps(original_playbook), encoding="utf-8")
            handler = _JsonHandler({"playbook": invalid_playbook})

            playbook_routes.handle_playbook_validate_draft(handler, playbook_path=playbook_path)

        self.assertEqual(handler.status, 200)
        self.assertFalse(handler.response["valid"])
        errors = handler.response["errors"]
        # No duplicate messages even though contract + rule validators both fire.
        messages = [error["message"] for error in errors]
        self.assertEqual(len(messages), len(set(messages)))

        by_message = {error["message"]: error for error in errors}
        term_error = next(error for message, error in by_message.items() if "max_term_years" in message)
        self.assertEqual(term_error["clause"], "term_and_survival")
        self.assertEqual(term_error["field"], "max_term_years")
        mutuality_error = next(
            error for message, error in by_message.items()
            if "mutuality must include requirement" in message
        )
        self.assertEqual(mutuality_error["clause"], "mutuality")
        self.assertEqual(mutuality_error["field"], "requirement")
        version_error = next(error for message, error in by_message.items() if message.startswith("Playbook version"))
        self.assertIsNone(version_error["clause"])
        self.assertEqual(version_error["field"], "version")
        self.assertEqual(version_error["location"], "version")

    def test_playbook_validate_draft_requires_playbook_object(self):
        with tempfile.TemporaryDirectory() as playbook_dir:
            playbook_path = Path(playbook_dir) / "playbook.json"
            playbook_path.write_text(json.dumps(load_playbook()), encoding="utf-8")
            handler = _JsonHandler({"summary": "no playbook"})

            playbook_routes.handle_playbook_validate_draft(handler, playbook_path=playbook_path)

        self.assertEqual(handler.status, 400)
        self.assertIn("playbook object", handler.response["error"])


if __name__ == "__main__":
    unittest.main()
