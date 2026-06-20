import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nda_automation import entity_authoring, entity_registry, entity_store
from nda_automation.checker import load_playbook
from nda_automation.routes import entities as entity_routes


def _tmp_store() -> Path:
    return Path(tempfile.mkdtemp(prefix="nda-entity-store-")) / "entities.json"


class EntityStoreSeedingTests(unittest.TestCase):
    def test_first_run_seeds_from_defaults_and_persists(self):
        store_path = _tmp_store()
        self.assertFalse(store_path.exists())
        entities = entity_store.load_entities(
            defaults=entity_registry.DEFAULT_SIGNING_ENTITIES, store_path=store_path
        )
        ids = {entity["id"] for entity in entities}
        self.assertEqual(
            ids,
            {entity["id"] for entity in entity_registry.DEFAULT_SIGNING_ENTITIES},
        )
        # The seed is written through so it is durable.
        self.assertTrue(store_path.exists())
        payload = json.loads(store_path.read_text())
        self.assertEqual(payload["version"], entity_store.ENTITY_STORE_VERSION)
        self.assertEqual(len(payload["entities"]), 7)

    def test_persisted_store_is_authoritative_over_defaults(self):
        store_path = _tmp_store()
        custom = [
            {
                "id": "only_co",
                "legal_name": "Only Co",
                "short_name": "Only",
                "addresses": [
                    {"id": "reg", "label": "Reg", "lines": ["1 St"], "country": "UK", "default": True}
                ],
                "governing_law": {"playbook_option_id": "india", "label": "India"},
                "jurisdiction": "courts in India",
                "incorporation_jurisdiction": "India",
                "signatory": {"name": "X", "title": "Y"},
            }
        ]
        entity_store.save_entities(custom, store_path=store_path, actor="t")
        loaded = entity_store.load_entities(
            defaults=entity_registry.DEFAULT_SIGNING_ENTITIES, store_path=store_path
        )
        self.assertEqual([e["id"] for e in loaded], ["only_co"])

    def test_empty_store_is_a_valid_state_not_reseeded(self):
        store_path = _tmp_store()
        entity_store.save_entities([], store_path=store_path, actor="t")
        loaded = entity_store.load_entities(
            defaults=entity_registry.DEFAULT_SIGNING_ENTITIES, store_path=store_path
        )
        self.assertEqual(loaded, [])

    def test_corrupt_store_falls_back_to_defaults(self):
        store_path = _tmp_store()
        store_path.parent.mkdir(parents=True, exist_ok=True)
        store_path.write_text("{ not valid json")
        loaded = entity_store.load_entities(
            defaults=entity_registry.DEFAULT_SIGNING_ENTITIES, store_path=store_path
        )
        self.assertEqual(len(loaded), 7)

    def test_save_returns_independent_copies(self):
        store_path = _tmp_store()
        saved = entity_store.save_entities(
            [
                {
                    "id": "x",
                    "legal_name": "X",
                    "addresses": [{"id": "a", "lines": ["1"], "default": True}],
                    "governing_law": {"playbook_option_id": "india", "label": "India"},
                    "jurisdiction": "c",
                    "incorporation_jurisdiction": "India",
                    "signatory": {"name": "n", "title": "t"},
                }
            ],
            store_path=store_path,
            actor="t",
        )
        saved[0]["legal_name"] = "MUTATED"
        again = entity_store.load_entities(
            defaults=entity_registry.DEFAULT_SIGNING_ENTITIES, store_path=store_path
        )
        self.assertEqual(again[0]["legal_name"], "X")


class EntityAuthoringTests(unittest.TestCase):
    def _seed(self) -> list[dict]:
        return [dict(e) for e in entity_registry.list_entities()]

    def test_save_persists_and_normalises_law_label(self):
        store_path = _tmp_store()
        entities = self._seed()
        entities.append(
            {
                "id": "new_co",
                "legal_name": "New Co Ltd",
                "short_name": "New Co",
                "addresses": [
                    {"id": "reg", "label": "Registered office", "lines": ["1 Test St", "London"], "country": "UK", "default": True}
                ],
                # Deliberately wrong cached label -- the authoring layer must
                # normalise it to the playbook's label before validating.
                "governing_law": {"playbook_option_id": "england_and_wales", "label": "WRONG"},
                "jurisdiction": "courts in England and Wales",
                "incorporation_jurisdiction": "England and Wales",
                "signatory": {"name": "X", "title": "Y"},
            }
        )
        result = entity_authoring.save_entities_registry(
            {"entities": entities}, actor="tester", store_path=store_path
        )
        self.assertTrue(result["saved"])
        stored = json.loads(store_path.read_text())["entities"]
        new_co = next(e for e in stored if e["id"] == "new_co")
        self.assertEqual(new_co["governing_law"]["label"], "England and Wales")

    def test_orphan_guard_rejects_unapproved_law(self):
        store_path = _tmp_store()
        entities = self._seed()
        entities[0]["governing_law"] = {"playbook_option_id": "narnia", "label": "Narnia"}
        with self.assertRaises(entity_authoring.EntityAuthoringError) as ctx:
            entity_authoring.save_entities_registry(
                {"entities": entities}, store_path=store_path
            )
        self.assertEqual(ctx.exception.status, 400)
        # The store was NOT written (a rejected save is a no-op).
        self.assertFalse(store_path.exists())

    def test_save_fails_closed_when_playbook_unreadable(self):
        # C1: when the playbook can't be read the orphan-approval join cannot be
        # proven, so a save carrying an orphan governing-law id must be REJECTED
        # (fail-closed) and NOT persisted -- never silently skip the guard.
        store_path = _tmp_store()
        entities = self._seed()
        entities[0]["governing_law"] = {"playbook_option_id": "narnia", "label": "Narnia"}
        with patch.object(
            entity_authoring, "_read_playbook_or_none", return_value=None
        ):
            with self.assertRaises(entity_authoring.EntityAuthoringError) as ctx:
                entity_authoring.save_entities_registry(
                    {"entities": entities}, store_path=store_path
                )
        self.assertEqual(ctx.exception.status, 503)
        # The store was NOT written (a rejected save is a no-op).
        self.assertFalse(store_path.exists())

    def test_save_fails_closed_when_playbook_unreadable_even_if_law_clean(self):
        # C1: fail-closed is unconditional on the save path -- even a registry whose
        # laws WOULD be approved is rejected when the playbook is unreadable, because
        # the single-source-of-truth join cannot be evaluated. Nothing is persisted.
        store_path = _tmp_store()
        with patch.object(
            entity_authoring, "_read_playbook_or_none", return_value=None
        ):
            with self.assertRaises(entity_authoring.EntityAuthoringError) as ctx:
                entity_authoring.save_entities_registry(
                    {"entities": self._seed()}, store_path=store_path
                )
        self.assertEqual(ctx.exception.status, 503)
        self.assertFalse(store_path.exists())

    def test_save_rejects_bracket_in_legal_name(self):
        # C2: a template-token-shaped legal_name (e.g. "[GOVERNING LAW]") collides
        # with the engine fill markers (DoSes generation) -- reject, do not persist.
        store_path = _tmp_store()
        entities = self._seed()
        entities[0]["legal_name"] = "Aspora [GOVERNING LAW] Ltd"
        with self.assertRaises(entity_authoring.EntityAuthoringError) as ctx:
            entity_authoring.save_entities_registry(
                {"entities": entities}, store_path=store_path
            )
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("bracket", str(ctx.exception.payload["error"]).lower())
        self.assertFalse(store_path.exists())

    def test_save_rejects_bracket_in_address_line(self):
        # C2: a "[FORUM]"-style token in an address line would be silently rewritten
        # to the resolved forum (address corruption) -- reject, do not persist.
        store_path = _tmp_store()
        entities = self._seed()
        entities[0]["addresses"][0]["lines"] = ["1 Test Street", "[FORUM]"]
        with self.assertRaises(entity_authoring.EntityAuthoringError) as ctx:
            entity_authoring.save_entities_registry(
                {"entities": entities}, store_path=store_path
            )
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("bracket", str(ctx.exception.payload["error"]).lower())
        self.assertFalse(store_path.exists())

    def test_validate_payload_flags_bracket_identity_field(self):
        # C2: the preview gate surfaces the bracket rejection too (before a save).
        entities = self._seed()
        entities[0]["legal_name"] = "Bad [COMPANY NAME] Co"
        result = entity_authoring.validate_entities_payload({"entities": entities})
        self.assertFalse(result["valid"])
        self.assertTrue(any("bracket" in e.lower() for e in result["errors"]))

    def test_rejects_missing_required_fields(self):
        store_path = _tmp_store()
        bad = [
            {
                "id": "no_law",
                "legal_name": "No Law Co",
                "addresses": [{"id": "a", "lines": ["1"], "default": True}],
                "governing_law": {"playbook_option_id": "", "label": ""},
                "jurisdiction": "court",
                "incorporation_jurisdiction": "X",
                "signatory": {"name": "n", "title": "t"},
            }
        ]
        with self.assertRaises(entity_authoring.EntityAuthoringError):
            entity_authoring.save_entities_registry({"entities": bad}, store_path=store_path)

    def test_rejects_missing_court(self):
        store_path = _tmp_store()
        bad = [
            {
                "id": "no_court",
                "legal_name": "No Court Co",
                "addresses": [{"id": "a", "lines": ["1"], "default": True}],
                "governing_law": {"playbook_option_id": "india", "label": "India"},
                "jurisdiction": "",
                "incorporation_jurisdiction": "India",
                "signatory": {"name": "n", "title": "t"},
            }
        ]
        with self.assertRaises(entity_authoring.EntityAuthoringError):
            entity_authoring.save_entities_registry({"entities": bad}, store_path=store_path)

    def test_rejects_more_than_one_default_address(self):
        store_path = _tmp_store()
        bad = [
            {
                "id": "two_defaults",
                "legal_name": "Two Defaults Co",
                "addresses": [
                    {"id": "a", "lines": ["1"], "default": True},
                    {"id": "b", "lines": ["2"], "default": True},
                ],
                "governing_law": {"playbook_option_id": "india", "label": "India"},
                "jurisdiction": "court",
                "incorporation_jurisdiction": "India",
                "signatory": {"name": "n", "title": "t"},
            }
        ]
        with self.assertRaises(entity_authoring.EntityAuthoringError):
            entity_authoring.save_entities_registry({"entities": bad}, store_path=store_path)

    def test_rejects_non_list_payload(self):
        with self.assertRaises(entity_authoring.EntityAuthoringError):
            entity_authoring.save_entities_registry({"entities": "nope"}, store_path=_tmp_store())

    def test_validate_payload_reports_errors_without_writing(self):
        entities = self._seed()
        entities[0]["governing_law"] = {"playbook_option_id": "narnia", "label": "Narnia"}
        result = entity_authoring.validate_entities_payload({"entities": entities})
        self.assertFalse(result["valid"])
        self.assertTrue(result["errors"])

    def test_validate_payload_accepts_clean_registry(self):
        result = entity_authoring.validate_entities_payload({"entities": self._seed()})
        self.assertTrue(result["valid"], result.get("errors"))

    def test_workspace_carries_playbook_law_options(self):
        workspace = entity_authoring.load_entities_workspace()
        self.assertTrue(workspace["playbook_available"])
        playbook = load_playbook()
        governing_law = next(
            c for c in playbook["clauses"] if c["id"] == "governing_law"
        )
        expected = [
            {"id": o["id"], "label": o["label"]}
            for o in governing_law["rules"]["approved_options"]
        ]
        self.assertEqual(workspace["governing_law_options"], expected)


class _FakeHandler:
    def __init__(self, *, admin: bool, body=None):
        self._admin = admin
        self._body = body
        self.current_user_id = "user-1"
        self.current_user = {"email": "admin@example.com", "provider": "google"}
        self.status = 200
        self.response = None

        class _Server:
            server_address = ("127.0.0.1", 0)

        self.server = _Server()

    def _send_json(self, payload, *, status=200, send_body=True):
        self.status = status
        self.response = payload

    def _read_json_payload(self):
        return self._body


class AdminEntityRouteTests(unittest.TestCase):
    def setUp(self):
        self.store_path = _tmp_store()
        # Point the authoring save at an isolated store for write tests.
        self._patches = [
            patch.object(entity_store, "ENTITY_STORE_PATH", self.store_path),
            patch(
                "nda_automation.entity_authoring.entity_store.ENTITY_STORE_PATH",
                self.store_path,
            ),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def test_admin_get_returns_workspace(self):
        handler = _FakeHandler(admin=True)
        with patch("nda_automation.routes.common.request_is_admin", return_value=True):
            entity_routes.handle_admin_signing_entities(handler)
        self.assertEqual(handler.status, 200)
        self.assertIn("entities", handler.response)
        self.assertIn("governing_law_options", handler.response)

    def test_non_admin_get_is_403(self):
        handler = _FakeHandler(admin=False)
        with patch("nda_automation.routes.common.request_is_admin", return_value=False):
            entity_routes.handle_admin_signing_entities(handler)
        self.assertEqual(handler.status, 403)

    def test_non_admin_save_is_403(self):
        handler = _FakeHandler(admin=False, body={"entities": []})
        with patch("nda_automation.routes.common.request_is_admin", return_value=False):
            entity_routes.handle_admin_signing_entities_save(handler)
        self.assertEqual(handler.status, 403)

    def test_admin_save_persists(self):
        entities = [dict(e) for e in entity_registry.list_entities()]
        entities[0]["jurisdiction"] = "courts in Mysuru, Karnataka"
        handler = _FakeHandler(admin=True, body={"entities": entities})
        with patch("nda_automation.routes.common.request_is_admin", return_value=True):
            entity_routes.handle_admin_signing_entities_save(handler)
        self.assertEqual(handler.status, 200)
        self.assertTrue(handler.response["saved"])
        stored = json.loads(self.store_path.read_text())["entities"]
        first = next(e for e in stored if e["id"] == entities[0]["id"])
        self.assertEqual(first["jurisdiction"], "courts in Mysuru, Karnataka")

    def test_admin_save_invalid_law_is_400(self):
        entities = [dict(e) for e in entity_registry.list_entities()]
        entities[0]["governing_law"] = {"playbook_option_id": "narnia", "label": "Narnia"}
        handler = _FakeHandler(admin=True, body={"entities": entities})
        with patch("nda_automation.routes.common.request_is_admin", return_value=True):
            entity_routes.handle_admin_signing_entities_save(handler)
        self.assertEqual(handler.status, 400)


if __name__ == "__main__":
    unittest.main()
