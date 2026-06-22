import copy
import unittest
from unittest.mock import patch

from nda_automation import entity_registry as er
from nda_automation.checker import load_playbook
from nda_automation.routes import entities as entity_routes

EXPECTED_ENTITY_IDS = {
    "aspora_technology",
    "vance_money",
    "real_transfer",
    "vance_techlabs",
    "nesse_technologies",
    "vance_technologies",
    "aspora_financial_services",
}

# The mapping every entity bundle relies on. These ids must exist in the live
# playbook's governing_law approved_options.
EXPECTED_LAW_MAPPING = {
    "aspora_technology": "india",
    "vance_money": "delaware",
    "real_transfer": "england_and_wales",
    "vance_techlabs": "difc",
    "nesse_technologies": "ontario_canada",
    "vance_technologies": "england_and_wales",
    "aspora_financial_services": "india",
}


class EntityRegistryBundleTests(unittest.TestCase):
    def test_registers_the_signing_entities(self):
        ids = {entity["id"] for entity in er.list_entities()}
        self.assertEqual(ids, EXPECTED_ENTITY_IDS)

    def test_registry_is_internally_consistent(self):
        # Required fields + exactly-one-default-address invariant.
        er.validate_registry()

    def test_each_entity_has_exactly_one_default_address(self):
        for entity in er.list_entities():
            defaults = [a for a in entity["addresses"] if a.get("default")]
            self.assertEqual(
                len(defaults),
                1,
                f"{entity['id']} must have exactly one default address",
            )
            self.assertIsNotNone(er.default_address(entity))

    def test_bundle_carries_name_address_and_law_together(self):
        # The whole point of a bundle: picking it gives you name + address + law.
        for entity in er.list_entities():
            self.assertTrue(entity["legal_name"])
            self.assertTrue(entity["addresses"])
            self.assertIn("playbook_option_id", entity["governing_law"])
            self.assertTrue(entity["jurisdiction"])
            self.assertIn("name", entity["signatory"])

    def test_real_transfer_has_two_addresses_default_is_london_corporate(self):
        real_transfer = er.get_entity("real_transfer")
        self.assertEqual(len(real_transfer["addresses"]), 2)
        default = er.default_address(real_transfer)
        self.assertEqual(default["id"], "corporate")
        # Belfast (registered, Northern Ireland) must NOT be the default, because
        # it has no matching playbook governing-law position.
        self.assertTrue(
            any("London" in line for line in default["lines"]),
            "Real Transfer default address should be the London corporate office",
        )
        registered = next(
            a for a in real_transfer["addresses"] if a["id"] == "registered"
        )
        self.assertFalse(registered["default"])
        self.assertTrue(any("Belfast" in line for line in registered["lines"]))

    def test_single_address_entities_have_one_address(self):
        for entity_id in ("aspora_technology", "vance_money", "vance_techlabs"):
            self.assertEqual(len(er.get_entity(entity_id)["addresses"]), 1)

    def test_get_entity_returns_none_for_unknown(self):
        self.assertIsNone(er.get_entity("nope"))

    def test_accessors_return_copies(self):
        # Mutating a returned bundle must not corrupt the module-level registry.
        entity = er.get_entity("aspora_technology")
        entity["legal_name"] = "MUTATED"
        entity["addresses"][0]["default"] = False
        fresh = er.get_entity("aspora_technology")
        self.assertEqual(
            fresh["legal_name"], "Aspora Technology Services Private Limited"
        )
        self.assertTrue(fresh["addresses"][0]["default"])

    def test_legal_names_match_exact_provided_details(self):
        names = {e["id"]: e["legal_name"] for e in er.list_entities()}
        self.assertEqual(
            names["aspora_technology"],
            "Aspora Technology Services Private Limited",
        )
        self.assertEqual(names["vance_money"], "Vance Money Services LLC")
        self.assertEqual(names["real_transfer"], "Real Transfer Limited")
        self.assertEqual(names["vance_techlabs"], "Vance Techlabs Limited")

    def test_incorporation_jurisdiction_is_legal_confirmed_value(self):
        # Fills generation's [JURISDICTION OF INCORPORATION] slot. Legal confirmed
        # these align with the governing-law jurisdiction for all four (Real
        # Transfer = England and Wales, NOT Northern Ireland).
        jurisdictions = {
            e["id"]: e["incorporation_jurisdiction"] for e in er.list_entities()
        }
        self.assertEqual(
            jurisdictions,
            {
                "aspora_technology": "India",
                "vance_money": "Delaware",
                "real_transfer": "England and Wales",
                "vance_techlabs": "DIFC",
                "nesse_technologies": "Ontario, Canada",
                "vance_technologies": "England and Wales",
                "aspora_financial_services": "India",
            },
        )

    def test_actor_slug_is_the_stable_entity_id(self):
        # Generation passes this as the artifact-registry `actor`, which slugs it
        # into the artifact filename. Using the id keeps names short and stable.
        for entity in er.list_entities():
            self.assertEqual(er.actor_slug(entity), entity["id"])
        self.assertEqual(
            er.actor_slug(er.get_entity("aspora_technology")),
            "aspora_technology",
        )


class GoverningLawMappingTests(unittest.TestCase):
    def test_mapping_matches_expected_positions(self):
        mapping = {
            row["entity_id"]: row["playbook_option_id"]
            for row in er.entity_law_mapping()
        }
        self.assertEqual(mapping, EXPECTED_LAW_MAPPING)

    def test_every_entity_maps_to_a_live_playbook_position(self):
        # This is the contract that keeps generation honest: if the playbook
        # renames or drops a governing_law option, this fails loudly.
        playbook = load_playbook()
        er.validate_registry_against_playbook(playbook)

    def test_mapping_against_playbook_marks_matches(self):
        playbook = load_playbook()
        for row in er.entity_law_mapping(playbook):
            self.assertTrue(
                row["matches_playbook"],
                f"{row['entity_id']} -> {row['playbook_option_id']} not in playbook",
            )

    def test_flags_are_present_for_ambiguous_entities(self):
        # Real Transfer (NI vs England) and Vance Techlabs (DIFC vs UAE federal)
        # are the two flagged-for-review cases.
        notes = er.ENTITY_LAW_MAPPING_NOTES
        self.assertIn("real_transfer", notes)
        self.assertIn("vance_techlabs", notes)
        self.assertIn("Northern Ireland", notes["real_transfer"])
        self.assertIn("DIFC", notes["vance_techlabs"])

        flagged = {
            row["entity_id"]
            for row in er.entity_law_mapping()
            if row["flag"]
        }
        self.assertEqual(flagged, {"real_transfer", "vance_techlabs"})

    def test_clean_entities_are_not_flagged(self):
        flags = {row["entity_id"]: row["flag"] for row in er.entity_law_mapping()}
        self.assertIsNone(flags["aspora_technology"])
        self.assertIsNone(flags["vance_money"])

    def test_validate_against_playbook_rejects_missing_position(self):
        # Simulate playbook drift: drop england_and_wales and confirm we fail.
        playbook = load_playbook()
        for clause in playbook["clauses"]:
            if clause["id"] == "governing_law":
                clause["rules"]["approved_options"] = [
                    opt
                    for opt in clause["rules"]["approved_options"]
                    if opt["id"] != "england_and_wales"
                ]
        with self.assertRaises(ValueError):
            er.validate_registry_against_playbook(playbook)

    def test_validate_against_playbook_rejects_empty_options(self):
        with self.assertRaises(ValueError):
            er.validate_registry_against_playbook({"clauses": []})


class ForumReconciliationTests(unittest.TestCase):
    """Per-entity governing-law/court restructure: each entity's per-entity
    ``jurisdiction`` (what generation writes) must bucket to the SAME jurisdiction
    as the governing law that entity defaults to. The expected bucket is derived
    from the LAW's own id/label (``law_forum_check.expected_forum_bucket``); the
    playbook no longer carries a per-option ``forum_jurisdiction`` -- the court is
    edited per signing entity, and the law is its own forum source."""

    def test_live_registry_and_playbook_forums_reconcile(self):
        # The shipped playbook + registry are forum-consistent at bucket granularity.
        er.validate_forum_reconciliation(load_playbook())

    def test_two_india_cities_reconcile_to_the_same_bucket(self):
        # The load-bearing case: two India entities sit in different CITIES
        # (Bengaluru vs Gandhinagar) but the same jurisdiction BUCKET (india), so
        # reconciliation must pass even though no per-law forum is authored.
        er.validate_forum_reconciliation(load_playbook())  # both india entities present
        self.assertEqual(er._forum_bucket("courts in Bengaluru, Karnataka"), "india")
        self.assertEqual(er._forum_bucket("courts in Gandhinagar, Gujarat"), "india")
        self.assertEqual(er._forum_bucket("Mumbai, India"), "india")

    def test_reconciliation_passes_without_per_law_forum_jurisdiction(self):
        # RESTRUCTURE INVARIANT: stripping every per-option forum_jurisdiction (the
        # field is gone after the restructure) must NOT break reconciliation -- the
        # expected bucket is derived from the law's own id. Previously this raised
        # "missing forum_jurisdiction"; now it is the normal, supported state.
        playbook = load_playbook()
        for clause in playbook["clauses"]:
            if clause["id"] == "governing_law":
                for option in clause["rules"]["approved_options"]:
                    option.pop("forum_jurisdiction", None)
        er.validate_forum_reconciliation(playbook)  # must not raise

    def test_entity_court_unresolvable_to_a_bucket_is_flagged(self):
        # An entity whose COURT does not resolve to any known jurisdiction bucket is
        # rejected (the generator would have no jurisdiction to anchor the venue to).
        playbook = load_playbook()
        entities = copy.deepcopy(er.DEFAULT_SIGNING_ENTITIES)
        entities[0]["jurisdiction"] = "Atlantis"
        with self.assertRaises(ValueError) as ctx:
            er.validate_forum_reconciliation(playbook, entities)
        self.assertIn("forum bucket", str(ctx.exception))

    def test_cross_bucket_forum_drift_is_flagged(self):
        # Point an India-law entity's COURT at England -> the entity (england_and_wales
        # bucket) no longer matches its india law's bucket. This must be caught.
        playbook = load_playbook()
        entities = copy.deepcopy(er.DEFAULT_SIGNING_ENTITIES)
        india = next(
            e for e in entities if e["governing_law"]["playbook_option_id"] == "india"
        )
        india["jurisdiction"] = "Courts of England and Wales"
        with self.assertRaises(ValueError) as ctx:
            er.validate_forum_reconciliation(playbook, entities)
        self.assertIn("Forum drift", str(ctx.exception))

    def test_reconciliation_runs_inside_validate_against_playbook(self):
        # validate_registry_against_playbook now also reconciles forums, so an entity
        # court bucket drift is caught by the broader validator too (the one the
        # runtime guard / route invoke).
        playbook = load_playbook()
        entities = copy.deepcopy(er.DEFAULT_SIGNING_ENTITIES)
        difc = next(
            e for e in entities if e["governing_law"]["playbook_option_id"] == "difc"
        )
        difc["jurisdiction"] = "courts of the Province of Ontario, Canada"
        with self.assertRaises(ValueError):
            er.validate_registry_against_playbook(playbook, entities)

    def test_candidate_entity_law_court_mismatch_is_caught(self):
        # FIX B (P1): validate_forum_reconciliation must reconcile the CANDIDATE
        # entities threaded through validate_registry_against_playbook -- not the
        # seed SIGNING_ENTITIES. Previously it iterated the (consistent) seed, so an
        # admin could save an entity whose law and court are in DIFFERENT
        # jurisdiction buckets and slip past the guard. Build a structurally-valid
        # India-law entity but point its court at England (cross-bucket mismatch).
        playbook = load_playbook()
        india = next(
            e
            for e in er.DEFAULT_SIGNING_ENTITIES
            if e["governing_law"]["playbook_option_id"] == "india"
        )
        rogue = copy.deepcopy(india)
        rogue["id"] = "rogue_india_england_court"
        rogue["jurisdiction"] = "Courts of England and Wales"

        # AFTER the fix: the candidate list is reconciled -> the mismatch is caught.
        with self.assertRaises(ValueError) as ctx:
            er.validate_registry_against_playbook(playbook, [rogue])
        message = str(ctx.exception)
        self.assertIn("Forum drift", message)
        self.assertIn("rogue_india_england_court", message)

        # Non-vacuity / base behaviour: the OLD code iterated only the seed entities
        # (all internally consistent), so the rogue candidate was never seen and the
        # guard passed. Reconciling the seed alone still passes here -- proving the
        # rogue was only caught because the candidate list is now threaded through.
        er.validate_forum_reconciliation(playbook, er.DEFAULT_SIGNING_ENTITIES)


class SigningEntitiesPayloadTests(unittest.TestCase):
    def test_payload_has_entities_mapping_and_option_ids(self):
        playbook = load_playbook()
        payload = er.signing_entities_payload(playbook)
        self.assertEqual(
            {row["entity_id"] for row in payload["law_mapping"]},
            EXPECTED_ENTITY_IDS,
        )
        self.assertEqual(
            {entity["id"] for entity in payload["entities"]},
            EXPECTED_ENTITY_IDS,
        )
        # The override dropdown is built from these, so they must be the live
        # playbook positions.
        self.assertEqual(
            set(payload["playbook_option_ids"]),
            {"india", "delaware", "england_and_wales", "difc", "ontario_canada"},
        )
        # The governing-law dropdown choices ({id,label}) are sourced from the
        # playbook's governing_law approved_options (single source of truth) so the
        # frontend dropdown is playbook-driven, not derived from the entity mirror.
        governing_law = next(
            clause for clause in playbook["clauses"] if clause["id"] == "governing_law"
        )
        expected_options = [
            {"id": option["id"], "label": option["label"]}
            for option in governing_law["rules"]["approved_options"]
        ]
        # id+label match the playbook positions; each option ALSO carries the
        # canonical forum pairing (court_name/forum_jurisdiction) so the Entities
        # & Courts editor can suggest the matching court on a law change. Assert the
        # id/label projection equals the playbook and that the forum fields exist.
        self.assertEqual(
            [{"id": o["id"], "label": o["label"]} for o in payload["governing_law_options"]],
            expected_options,
        )
        for option in payload["governing_law_options"]:
            self.assertIn("court_name", option)
            self.assertIn("forum_jurisdiction", option)
        self.assertTrue(
            all(row["matches_playbook"] for row in payload["law_mapping"])
        )

    def test_payload_without_playbook_still_returns_entities(self):
        payload = er.signing_entities_payload(None)
        self.assertEqual(len(payload["entities"]), 7)
        self.assertEqual(payload["playbook_option_ids"], [])
        # No playbook means no playbook-sourced law options; the frontend falls
        # back to deriving them from the entity mirror.
        self.assertEqual(payload["governing_law_options"], [])
        # No playbook means no drift verdict per row.
        self.assertNotIn("matches_playbook", payload["law_mapping"][0])
        # No playbook means no live term cap either.
        self.assertNotIn("playbook_meta", payload)

    def test_payload_exposes_live_term_cap_from_playbook(self):
        # The Generator reads playbook_meta.max_term_years (falling back to a
        # hardcoded 5 only when absent). It must be sourced LIVE from the
        # playbook's term_and_survival clause, not duplicated as a literal.
        playbook = load_playbook()
        expected = next(
            clause["max_term_years"]
            for clause in playbook["clauses"]
            if clause["id"] == "term_and_survival"
        )
        payload = er.signing_entities_payload(playbook)
        self.assertIn("playbook_meta", payload)
        self.assertEqual(payload["playbook_meta"]["max_term_years"], expected)

    def test_payload_omits_term_cap_when_malformed(self):
        # A missing/malformed cap is omitted (not invented) so the frontend's own
        # fallback handles it.
        playbook = load_playbook()
        for clause in playbook["clauses"]:
            if clause["id"] == "term_and_survival":
                clause["max_term_years"] = "five"  # malformed
        payload = er.signing_entities_payload(playbook)
        self.assertNotIn("playbook_meta", payload)


class _FakeHandler:
    current_user_id = ""
    current_user = None

    def __init__(self):
        self.status = 200
        self.response = None

    def _send_json(self, payload, *, status=200, send_body=True):
        self.status = status
        self.response = payload


class SigningEntitiesRouteTests(unittest.TestCase):
    def test_route_returns_payload_from_live_playbook(self):
        handler = _FakeHandler()
        entity_routes.handle_signing_entities(handler)
        self.assertEqual(handler.status, 200)
        self.assertEqual(len(handler.response["entities"]), 7)
        self.assertTrue(
            all(row["matches_playbook"] for row in handler.response["law_mapping"])
        )
        # The live route must surface the playbook term cap for the Generator.
        playbook = load_playbook()
        expected = next(
            clause["max_term_years"]
            for clause in playbook["clauses"]
            if clause["id"] == "term_and_survival"
        )
        self.assertEqual(
            handler.response["playbook_meta"]["max_term_years"], expected
        )

    def test_route_degrades_gracefully_when_playbook_unreadable(self):
        # If the playbook can't be read, the registry is still self-contained,
        # so the endpoint must still return the entities (without drift data).
        handler = _FakeHandler()
        with patch.object(
            entity_routes,
            "read_playbook_from_path",
            side_effect=OSError("boom"),
        ):
            entity_routes.handle_signing_entities(handler)
        self.assertEqual(handler.status, 200)
        self.assertEqual(len(handler.response["entities"]), 7)
        self.assertEqual(handler.response["playbook_option_ids"], [])

    def test_route_clean_registry_has_no_drift_diagnostic(self):
        # FIX 2: the live playbook/registry reconcile, so the route surfaces NO
        # registry_drift key.
        handler = _FakeHandler()
        entity_routes.handle_signing_entities(handler)
        self.assertEqual(handler.status, 200)
        self.assertNotIn("registry_drift", handler.response)

    def test_route_surfaces_drift_without_failing(self):
        # FIX 2: a drifted playbook (renamed approved option) is caught proactively
        # at the entities route and surfaced as a registry_drift diagnostic, WITHOUT
        # a 500 that would break the draft UI.
        playbook = load_playbook()
        for clause in playbook["clauses"]:
            if clause["id"] == "governing_law":
                for option in clause["rules"]["approved_options"]:
                    if option["id"] == "england_and_wales":
                        option["id"] = "england_renamed"
        handler = _FakeHandler()
        with patch.object(
            entity_routes, "read_playbook_from_path", return_value=playbook
        ):
            entity_routes.handle_signing_entities(handler)
        # Route still succeeds (no exception, 200) and still returns the entities.
        self.assertEqual(handler.status, 200)
        self.assertEqual(len(handler.response["entities"]), 7)
        # But the drift is surfaced for the UI to warn on.
        self.assertIn("registry_drift", handler.response)
        self.assertIn("england_and_wales", handler.response["registry_drift"])


if __name__ == "__main__":
    unittest.main()
