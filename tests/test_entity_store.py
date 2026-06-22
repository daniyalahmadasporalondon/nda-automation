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


class MigrateSignatoryFillsTests(unittest.TestCase):
    """Regression guards for ``entity_store.migrate_signatory_fills``.

    The signatory names live as DATA in the migration mapping
    (``_SIGNATORY_FILL_BY_ID``), not as code-seed defaults. These tests pin the
    migration's critical guarantees: a placeholder-only fill of the named
    entities, no overwrite of a real value, no revert of an admin edit,
    field-level granularity, idempotency, and safe no-ops on a missing / empty /
    corrupt / unwritable store.
    """

    PLACEHOLDER = {"name": "[Authorised Signatory]", "title": "[Title]"}

    # The 3 entities the live mapping fills, and the values it fills them with.
    EXPECTED_FILLS = {
        "aspora_technology": {"name": "Parth Pramendra Garg", "title": "Authorised Signatory"},
        "aspora_financial_services": {"name": "Rahul Bakshi", "title": "Authorised Signatory"},
        "vance_money": {"name": "Rahul Bakshi", "title": "Director"},
    }

    def _seed_placeholder_store(self, store_path: Path) -> list[dict]:
        """Persist all 7 seed entities with the GENERIC placeholder signatory."""
        entities = []
        for entity in entity_registry.DEFAULT_SIGNING_ENTITIES:
            copied = {k: v for k, v in entity.items()}
            copied["signatory"] = dict(self.PLACEHOLDER)
            entities.append(copied)
        entity_store.save_entities(entities, store_path=store_path, actor="seed")
        return entities

    def _stored(self, store_path: Path) -> dict[str, dict]:
        payload = json.loads(store_path.read_text())["entities"]
        return {e["id"]: e for e in payload}

    def test_fills_the_three_mapped_entities_and_leaves_the_rest(self):
        # Case 1: fresh placeholder store -> the 3 mapped entities fill to their
        # mapped values, the other 4 stay placeholder, returns 3.
        store_path = _tmp_store()
        self._seed_placeholder_store(store_path)

        filled = entity_store.migrate_signatory_fills(store_path=store_path)
        self.assertEqual(filled, 3)

        stored = self._stored(store_path)
        for entity_id, expected in self.EXPECTED_FILLS.items():
            self.assertEqual(stored[entity_id]["signatory"], expected)
        # The 4 unmapped entities are untouched (still placeholder).
        unmapped = set(stored) - set(self.EXPECTED_FILLS)
        self.assertEqual(len(unmapped), 4)
        for entity_id in unmapped:
            self.assertEqual(stored[entity_id]["signatory"], self.PLACEHOLDER)

    def test_does_not_overwrite_a_real_value_on_a_mapped_entity(self):
        # Case 2: a pre-set REAL signatory on a MAPPED entity is left untouched;
        # the other 2 mapped entities still fill.
        store_path = _tmp_store()
        entities = self._seed_placeholder_store(store_path)
        for entity in entities:
            if entity["id"] == "vance_money":
                entity["signatory"] = {"name": "Jane Doe", "title": "CEO"}
        entity_store.save_entities(entities, store_path=store_path, actor="admin")

        filled = entity_store.migrate_signatory_fills(store_path=store_path)
        self.assertEqual(filled, 2)  # vance_money skipped (real value)

        stored = self._stored(store_path)
        self.assertEqual(
            stored["vance_money"]["signatory"], {"name": "Jane Doe", "title": "CEO"}
        )
        self.assertEqual(
            stored["aspora_technology"]["signatory"],
            self.EXPECTED_FILLS["aspora_technology"],
        )
        self.assertEqual(
            stored["aspora_financial_services"]["signatory"],
            self.EXPECTED_FILLS["aspora_financial_services"],
        )

    def test_edit_then_migrate_does_not_revert_the_edit(self):
        # Case 3: fill, then "edit" a filled entity to a new real value, then
        # re-run the migration -> the edit is left intact, returns 0.
        store_path = _tmp_store()
        self._seed_placeholder_store(store_path)
        entity_store.migrate_signatory_fills(store_path=store_path)

        # Admin edits aspora_technology to a different real signatory and saves.
        entities = list(self._stored(store_path).values())
        for entity in entities:
            if entity["id"] == "aspora_technology":
                entity["signatory"] = {"name": "Edited Person", "title": "VP Legal"}
        entity_store.save_entities(entities, store_path=store_path, actor="admin")

        filled = entity_store.migrate_signatory_fills(store_path=store_path)
        self.assertEqual(filled, 0)
        self.assertEqual(
            self._stored(store_path)["aspora_technology"]["signatory"],
            {"name": "Edited Person", "title": "VP Legal"},
        )

    def test_field_level_partial_fill(self):
        # Case 4: real name + placeholder title -> only title fills; and the
        # reverse (placeholder name + real title -> only name fills).
        store_path = _tmp_store()
        entities = self._seed_placeholder_store(store_path)
        for entity in entities:
            if entity["id"] == "aspora_technology":
                # real name, placeholder title
                entity["signatory"] = {"name": "Custom Person", "title": "[Title]"}
            elif entity["id"] == "vance_money":
                # placeholder name, real title
                entity["signatory"] = {"name": "[Authorised Signatory]", "title": "Custom Title"}
        entity_store.save_entities(entities, store_path=store_path, actor="admin")

        entity_store.migrate_signatory_fills(store_path=store_path)
        stored = self._stored(store_path)
        # aspora_technology: name kept, title filled from the mapping.
        self.assertEqual(
            stored["aspora_technology"]["signatory"],
            {"name": "Custom Person", "title": "Authorised Signatory"},
        )
        # vance_money: name filled from the mapping, title kept.
        self.assertEqual(
            stored["vance_money"]["signatory"],
            {"name": "Rahul Bakshi", "title": "Custom Title"},
        )

    def test_idempotent_second_run_is_a_no_op(self):
        # Case 5: 2nd run returns 0 and changes nothing.
        store_path = _tmp_store()
        self._seed_placeholder_store(store_path)
        self.assertEqual(entity_store.migrate_signatory_fills(store_path=store_path), 3)
        first = self._stored(store_path)
        self.assertEqual(entity_store.migrate_signatory_fills(store_path=store_path), 0)
        self.assertEqual(self._stored(store_path), first)

    def test_missing_store_is_a_safe_no_op(self):
        # Case 6a: no store on disk -> returns 0, never raises, nothing written.
        store_path = _tmp_store()
        self.assertFalse(store_path.exists())
        self.assertEqual(entity_store.migrate_signatory_fills(store_path=store_path), 0)
        self.assertFalse(store_path.exists())

    def test_empty_store_is_a_safe_no_op(self):
        # Case 6b: an explicitly-empty operator state -> returns 0, store unharmed.
        store_path = _tmp_store()
        entity_store.save_entities([], store_path=store_path, actor="admin")
        self.assertEqual(entity_store.migrate_signatory_fills(store_path=store_path), 0)
        self.assertEqual(json.loads(store_path.read_text())["entities"], [])

    def test_corrupt_store_is_a_safe_no_op(self):
        # Case 6c: corrupt JSON reads as "no usable store" -> returns 0, never
        # raises, and the corrupt bytes are left as-is (not silently overwritten).
        store_path = _tmp_store()
        store_path.parent.mkdir(parents=True, exist_ok=True)
        store_path.write_text("{ not valid json")
        self.assertEqual(entity_store.migrate_signatory_fills(store_path=store_path), 0)
        self.assertEqual(store_path.read_text(), "{ not valid json")

    def test_disk_write_error_is_swallowed_and_returns_zero(self):
        # Case 6d: a write failure during the snapshot must never crash boot --
        # the error is swallowed and 0 returned. The store has fillable
        # placeholders, so the migration WOULD write if it could.
        store_path = _tmp_store()
        self._seed_placeholder_store(store_path)
        original = store_path.read_text()
        with patch.object(
            entity_store, "_write_snapshot", side_effect=OSError("disk full")
        ):
            # Must not raise.
            result = entity_store.migrate_signatory_fills(store_path=store_path)
        self.assertEqual(result, 0)
        # The on-disk store is unharmed (the failed write was atomic / swallowed).
        self.assertEqual(store_path.read_text(), original)


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

    def test_save_rejects_law_change_when_playbook_unreadable(self):
        # BUG C: when the playbook can't be read the orphan-approval join cannot be
        # proven, so a save that CHANGES an entity's governing law (or court) must be
        # REJECTED (503) -- we cannot validate the new law against the missing
        # playbook. The stored law value is left untouched.
        store_path = _tmp_store()
        entity_store.save_entities(self._seed(), store_path=store_path, actor="seed")
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
        # The stored law was NOT changed (the rejected law edit is a no-op).
        stored = entity_store.load_entities(
            defaults=entity_registry.DEFAULT_SIGNING_ENTITIES, store_path=store_path
        )
        self.assertEqual(stored[0]["governing_law"]["playbook_option_id"], "india")

    def test_save_rejects_court_change_when_playbook_unreadable(self):
        # BUG C: a court (jurisdiction) change is law-validated (forum
        # reconciliation), so it is equally un-provable when the playbook is missing
        # -> rejected 503, court unchanged.
        store_path = _tmp_store()
        entity_store.save_entities(self._seed(), store_path=store_path, actor="seed")
        entities = self._seed()
        entities[0]["jurisdiction"] = "courts in Delaware, USA"
        with patch.object(
            entity_authoring, "_read_playbook_or_none", return_value=None
        ):
            with self.assertRaises(entity_authoring.EntityAuthoringError) as ctx:
                entity_authoring.save_entities_registry(
                    {"entities": entities}, store_path=store_path
                )
        self.assertEqual(ctx.exception.status, 503)

    def test_save_persists_non_law_edit_when_playbook_unreadable(self):
        # BUG C regression: the console promises "Saving is still possible but law
        # validation is skipped" when the playbook is unavailable. A NON-LAW edit
        # (signatory, address, names) -- leaving law + court untouched -- must
        # PERSIST, matching that promise, instead of fail-closing on every save.
        store_path = _tmp_store()
        entity_store.save_entities(self._seed(), store_path=store_path, actor="seed")
        entities = self._seed()
        entities[0]["signatory"] = {"name": "Brand New Signer", "title": "Director"}
        with patch.object(
            entity_authoring, "_read_playbook_or_none", return_value=None
        ):
            workspace = entity_authoring.save_entities_registry(
                {"entities": entities}, store_path=store_path
            )
        self.assertTrue(workspace.get("saved"))
        stored = entity_store.load_entities(
            defaults=entity_registry.DEFAULT_SIGNING_ENTITIES, store_path=store_path
        )
        self.assertEqual(stored[0]["signatory"]["name"], "Brand New Signer")
        # Law + court were left exactly as stored.
        self.assertEqual(stored[0]["governing_law"]["playbook_option_id"], "india")

    def test_save_rejects_incorporation_jurisdiction_change_when_playbook_unreadable(
        self,
    ):
        # P1: incorporation_jurisdiction flows verbatim into the signed NDA's
        # "incorporated under the laws of X" recital. It is jurisdiction-bearing and
        # so is un-provable when the playbook is missing -- a change to it (law +
        # court left untouched) must be REFUSED (503), not waved through as a
        # "non-law edit". Otherwise an outage lets an unsanctioned jurisdiction
        # ("Cayman Islands") reach a signed legal document.
        store_path = _tmp_store()
        entity_store.save_entities(self._seed(), store_path=store_path, actor="seed")
        entities = self._seed()
        india_idx = next(
            i
            for i, e in enumerate(entities)
            if e["governing_law"]["playbook_option_id"] == "india"
        )
        original_incorp = entities[india_idx]["incorporation_jurisdiction"]
        entities[india_idx]["incorporation_jurisdiction"] = "Cayman Islands"
        with patch.object(
            entity_authoring, "_read_playbook_or_none", return_value=None
        ):
            with self.assertRaises(entity_authoring.EntityAuthoringError) as ctx:
                entity_authoring.save_entities_registry(
                    {"entities": entities}, store_path=store_path
                )
        self.assertEqual(ctx.exception.status, 503)
        # The stored incorporation jurisdiction was NOT changed (rejected = no-op).
        stored = entity_store.load_entities(
            defaults=entity_registry.DEFAULT_SIGNING_ENTITIES, store_path=store_path
        )
        self.assertEqual(
            stored[india_idx]["incorporation_jurisdiction"], original_incorp
        )
        self.assertNotEqual(
            stored[india_idx]["incorporation_jurisdiction"], "Cayman Islands"
        )

    def test_save_persists_signatory_edit_even_when_incorporation_unchanged(self):
        # P1 companion: the fix must not over-refuse. A genuine non-law edit
        # (signatory name) with law + court + incorporation_jurisdiction all
        # unchanged must still PERSIST during a playbook outage.
        store_path = _tmp_store()
        entity_store.save_entities(self._seed(), store_path=store_path, actor="seed")
        entities = self._seed()
        entities[0]["signatory"] = {"name": "Outage Signer", "title": "Director"}
        with patch.object(
            entity_authoring, "_read_playbook_or_none", return_value=None
        ):
            workspace = entity_authoring.save_entities_registry(
                {"entities": entities}, store_path=store_path
            )
        self.assertTrue(workspace.get("saved"))
        stored = entity_store.load_entities(
            defaults=entity_registry.DEFAULT_SIGNING_ENTITIES, store_path=store_path
        )
        self.assertEqual(stored[0]["signatory"]["name"], "Outage Signer")
        # incorporation_jurisdiction (and law) untouched -> the edit was allowed
        # because it touched no jurisdiction-bearing field.
        self.assertEqual(stored[0]["governing_law"]["playbook_option_id"], "india")

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
        self.assertEqual(
            [{"id": o["id"], "label": o["label"]} for o in workspace["governing_law_options"]],
            expected,
        )
        # The workspace carries the optimistic-concurrency token (BUG B): a
        # non-empty etag the editor echoes back on save.
        self.assertIn("etag", workspace)
        self.assertTrue(workspace["etag"])


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
        #
        # ``entity_authoring.entity_store`` is the SAME module object as
        # ``entity_store``, so both former patch targets aliased the one
        # attribute ``entity_store.ENTITY_STORE_PATH``. Patching it twice left
        # the second patch capturing the ALREADY-patched tmp value as its
        # "original", so its ``stop()`` re-installed the tmp path after the
        # first patch restored the real one -- leaking the tmp (polluted) store
        # path into every later test in the process. A single patch on the
        # canonical attribute is sufficient and cannot leak.
        self._patch = patch.object(entity_store, "ENTITY_STORE_PATH", self.store_path)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

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
        # Use a court the forum bucketer recognizes (same india bucket as the
        # entity's india governing law) -- the save's candidate-forum reconciliation
        # now validates the entity being saved, so the city must resolve to a bucket.
        entities[0]["jurisdiction"] = "courts in Mumbai, Maharashtra"
        handler = _FakeHandler(admin=True, body={"entities": entities})
        with patch("nda_automation.routes.common.request_is_admin", return_value=True):
            entity_routes.handle_admin_signing_entities_save(handler)
        self.assertEqual(handler.status, 200)
        self.assertTrue(handler.response["saved"])
        stored = json.loads(self.store_path.read_text())["entities"]
        first = next(e for e in stored if e["id"] == entities[0]["id"])
        self.assertEqual(first["jurisdiction"], "courts in Mumbai, Maharashtra")

    def test_admin_save_invalid_law_is_400(self):
        entities = [dict(e) for e in entity_registry.list_entities()]
        entities[0]["governing_law"] = {"playbook_option_id": "narnia", "label": "Narnia"}
        handler = _FakeHandler(admin=True, body={"entities": entities})
        with patch("nda_automation.routes.common.request_is_admin", return_value=True):
            entity_routes.handle_admin_signing_entities_save(handler)
        self.assertEqual(handler.status, 400)

    def test_stale_etag_save_is_409_and_does_not_revert(self):
        # BUG B: two editors load the same etag. Editor A saves a COURT change;
        # Editor B (holding the now-stale etag) then saves a SIGNATORY-only change
        # with the OLD court. Without optimistic concurrency, B's whole-file replace
        # would silently revert A's court. With it, B is rejected 409 and A's court
        # survives.
        with patch("nda_automation.routes.common.request_is_admin", return_value=True):
            # Both editors load the same snapshot + etag.
            get = _FakeHandler(admin=True)
            entity_routes.handle_admin_signing_entities(get)
            etag0 = get.response["etag"]

            # Editor A changes real_transfer's court to a different england court.
            ents_a = [dict(e) for e in entity_registry.list_entities()]
            rt_a = next(e for e in ents_a if e["id"] == "real_transfer")
            rt_a["jurisdiction"] = "the English courts"
            a = _FakeHandler(admin=True, body={"entities": ents_a, "etag": etag0})
            entity_routes.handle_admin_signing_entities_save(a)
            self.assertEqual(a.status, 200)

            # Editor B (stale etag0) saves a signatory-only edit with the OLD court.
            ents_b = [dict(e) for e in entity_registry.list_entities()]
            rt_b = next(e for e in ents_b if e["id"] == "real_transfer")
            rt_b["signatory"] = {"name": "Someone B", "title": "Director"}
            b = _FakeHandler(admin=True, body={"entities": ents_b, "etag": etag0})
            entity_routes.handle_admin_signing_entities_save(b)
            self.assertEqual(b.status, 409)
            self.assertIn("etag", b.response)

        # Editor A's court change survived (B did not clobber it).
        stored = json.loads(self.store_path.read_text())["entities"]
        rt_stored = next(e for e in stored if e["id"] == "real_transfer")
        self.assertEqual(rt_stored["jurisdiction"], "the English courts")


if __name__ == "__main__":
    unittest.main()
