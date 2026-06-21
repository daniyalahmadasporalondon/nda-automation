"""FULL-HTTP governing-law override smoke through POST /api/generate-nda.

This is the most faithful route-level smoke: it POSTs the REAL draft-ui
buildDraftPayload shape (override nested under
signing_entity.governing_law.playbook_option_id) to the actual /api/generate-nda
HTTP handler on a threaded test server, then FETCHES the generated .docx over the
returned download_url (the matter-source route) and runs the rendered NDA through
the full gen-verify gate. This exercises the entire FE -> endpoint -> parser ->
engine -> persistence -> download round-trip — the exact path the nesting bug lived
on, where an internal-API smoke is blind.

For each sampled entity overridden to a DIFFERENT approved law it asserts the
fetched NDA NAMES the override law, the gate is CLEAR, and the manifest provenance
is right. It also confirms a NON-override request still renders the entity default,
so the parser fix did not break the default path.
"""
from __future__ import annotations

import base64
import http.client
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from nda_automation import matter_store, telemetry
from nda_automation import server as server_module
from nda_automation.checker import load_playbook
from nda_automation.docx_text import extract_docx_text
from nda_automation.review_engine import ACTIVE_REVIEW_ENGINE_ENV
from nda_automation.server import NdaAutomationHandler

from tests.gen_verify_harness import (
    EntityExpectation,
    VerificationReport,
    check_governing_law,
    check_structural,
    expectations_from_registry,
)


class _QuietHandler(NdaAutomationHandler):
    def log_message(self, *args, **kwargs):
        return


PLAYBOOK = load_playbook()

# Each entity overridden to a DIFFERENT approved law than its registry default.
_OVERRIDE_TARGET = {
    "aspora_technology": ("india", "england_and_wales"),
    "vance_money": ("delaware", "india"),
    "real_transfer": ("england_and_wales", "difc"),
    "vance_techlabs": ("difc", "delaware"),
}

_ENTITY_LABEL = {
    "aspora_technology": "Aspora Technology Services Private Limited",
    "vance_money": "Vance Money Services LLC",
    "real_transfer": "Real Transfer Limited",
    "vance_techlabs": "Vance Techlabs Limited",
}


def _law_value(option_id: str) -> str:
    for clause in PLAYBOOK.get("clauses", []):
        if clause.get("id") != "governing_law":
            continue
        for opt in clause.get("rules", {}).get("approved_options", []):
            if opt.get("id") == option_id:
                return str(opt.get("value"))
    raise KeyError(option_id)


def _law_phrase(option_id: str) -> str:
    """The Playbook's legally-correct PHRASING for an option's law value.

    The clause renders ``governing_law.law_phrases[value]`` (DIFC -> "the DIFC"),
    falling back to the raw value when no phrase is mapped -- so prose assertions
    must look for the phrase, not the bare value.
    """
    value = _law_value(option_id)
    for clause in PLAYBOOK.get("clauses", []):
        if clause.get("id") == "governing_law":
            phrases = clause.get("law_phrases") or {}
            phrase = str(phrases.get(value, "")).strip()
            return phrase or value
    return value


# ENTITY-FORUM (corrected): the forum is the SIGNING entity's OWN court, regardless
# of any governing-law override. Each sampled entity below is seated such that its
# own court equals its DEFAULT option's representative court here -- so an override
# changes only the LAW, while the forum stays the entity's own (default) court. This
# map is the per-entity OWN court keyed by the entity's default option.
_OPTION_FORUM = {
    "india": "courts in Bengaluru, Karnataka",  # aspora_technology's own seat
    "delaware": "courts in Delaware, USA",
    "england_and_wales": "courts in England and Wales",
    "difc": "the DIFC Courts",
}


def _fe_payload(entity_id: str, option_id: str, *, overridden: bool) -> dict:
    """The exact nested buildDraftPayload shape, with the law under signing_entity."""
    return {
        "counterparty": {"name": "Counterparty Holdings Limited", "email": "legal@counterparty.example"},
        "project_purpose": "evaluating a potential commercial relationship",
        "term": "3 years",
        "nda_type": "mutual",
        "notes": "financial technology services",
        "signing_entity": {
            "id": entity_id,
            "legal_name": _ENTITY_LABEL[entity_id],
            "governing_law": {"playbook_option_id": option_id, "label": _law_value(option_id)},
            "governing_law_overridden": overridden,
        },
    }


class OverrideHttpSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _QuietHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.host, cls.port = cls.server.server_address

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        server_module._reset_rate_limits()
        telemetry.reset()

    def _auth(self):
        token = base64.b64encode(b"nda-admin:secret").decode("ascii")
        return {"Authorization": f"Basic {token}"}

    def _env(self):
        return {
            "NDA_REQUIRE_AUTH": "true",
            "NDA_AUTH_USERNAME": "nda-admin",
            "NDA_AUTH_PASSWORD": "secret",
            ACTIVE_REVIEW_ENGINE_ENV: "deterministic",
        }

    def _store_patches(self, data_dir):
        data_path = server_module.Path(data_dir)
        return (
            patch.object(matter_store, "DATA_DIR", data_path),
            patch.object(matter_store, "MATTERS_PATH", data_path / "matters.json"),
            patch.object(matter_store, "UPLOADS_DIR", data_path / "uploads"),
        )

    def _request(self, method, path, body=None, headers=None):
        request_headers = dict(headers or {})
        request_body = body
        if isinstance(body, dict):
            request_body = json.dumps(body).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        conn = http.client.HTTPConnection(self.host, self.port, timeout=15)
        try:
            conn.request(method, path, body=request_body, headers=request_headers)
            resp = conn.getresponse()
            raw = resp.read()
            ctype = resp.getheader("Content-Type", "")
            payload = json.loads(raw.decode("utf-8")) if "application/json" in ctype else raw
            return resp.status, payload
        finally:
            conn.close()

    def _generate_and_fetch(self, fe_payload):
        """POST the FE payload to /api/generate-nda, then GET the generated docx
        bytes over the returned download_url. Returns (response_payload, docx_bytes)."""
        status, payload = self._request("POST", "/api/generate-nda", fe_payload, headers=self._auth())
        self.assertEqual(status, 201, payload)
        download_url = payload["download_url"]
        self.assertTrue(download_url, payload)
        status2, docx = self._request("GET", download_url, headers=self._auth())
        self.assertEqual(status2, 200)
        self.assertIsInstance(docx, (bytes, bytearray))
        self.assertTrue(docx[:2] == b"PK", "download is not a .docx (no PK zip signature)")
        return payload, bytes(docx)

    def _expect(self, entity_id) -> EntityExpectation:
        return expectations_from_registry()[entity_id]

    def test_override_renders_through_full_http_round_trip(self):
        for entity_id, (default_opt, override_opt) in _OVERRIDE_TARGET.items():
            with self.subTest(entity_id=entity_id, override=override_opt):
                with tempfile.TemporaryDirectory() as data_dir:
                    p = self._store_patches(data_dir)
                    with p[0], p[1], p[2], patch.dict(os.environ, self._env()):
                        payload, docx = self._generate_and_fetch(
                            _fe_payload(entity_id, override_opt, overridden=True)
                        )
                    override_value = _law_value(override_opt)
                    default_value = _law_value(default_opt)
                    override_phrase = _law_phrase(override_opt)
                    text = extract_docx_text(docx)

                    # Manifest provenance from the FE-shaped payload, via the route.
                    m = payload["manifest"]
                    self.assertTrue(m["governing_law_overridden"], m)
                    self.assertEqual(m["governing_law_value"], override_value)
                    self.assertEqual(m["entity_default_governing_law_value"], default_value)
                    # The FETCHED, rendered NDA NAMES the override law (as its legally
                    # correct PHRASE). (This is the parser-fix differential: FAILS on the
                    # buggy tip where the nested override is dropped, PASSES once read.)
                    self.assertIn(override_phrase, text, f"{entity_id}: override law not rendered")

                    # Independent gate on the fetched bytes: governing-law correct,
                    # structural complete, no false entity-mismatch.
                    expect = self._expect(entity_id)
                    report = VerificationReport(label=f"http-override {entity_id}->{override_opt}")
                    check_structural(text, report)
                    from tests.gen_verify_harness import GovLawOverride

                    check_governing_law(
                        text, expect, report,
                        override=GovLawOverride(
                            effective_law=override_value, overridden=True, entity_default_law=default_value
                        ),
                    )
                    defects = [(f.check, f.detail) for f in report.findings if f.severity == "DEFECT"]
                    self.assertEqual(defects, [], defects)

    def test_non_override_renders_entity_default(self):
        # A NON-override request (signing_entity.governing_law == entity default)
        # must still render the entity's default law — proves the parser fix did not
        # break the default path.
        for entity_id, (default_opt, _override_opt) in _OVERRIDE_TARGET.items():
            with self.subTest(entity_id=entity_id):
                with tempfile.TemporaryDirectory() as data_dir:
                    p = self._store_patches(data_dir)
                    with p[0], p[1], p[2], patch.dict(os.environ, self._env()):
                        payload, docx = self._generate_and_fetch(
                            _fe_payload(entity_id, default_opt, overridden=False)
                        )
                    default_value = _law_value(default_opt)
                    text = extract_docx_text(docx)
                    m = payload["manifest"]
                    self.assertFalse(m["governing_law_overridden"], m)
                    self.assertEqual(m["governing_law_value"], default_value)
                    self.assertIn(default_value, text, f"{entity_id}: default law not rendered")

    def test_override_forum_follows_the_chosen_option(self):
        """Override decouples LAW from FORUM: the forum is the SIGNING entity's OWN court.

        An override to e.g. DIFC yields DIFC LAW, but the FORUM stays the signing
        entity's own court (the fixed entity litigates in its own seat). Through the
        full HTTP round-trip the manifest forum and the rendered clause must both be
        the SIGNING ENTITY's own (default-seat) court -- NEVER another entity's court
        pulled from the overridden option -- while the override LAW appears in the
        rendered prose and the default law does not leak into the clause.
        """
        for entity_id, (default_opt, override_opt) in _OVERRIDE_TARGET.items():
            with self.subTest(entity_id=entity_id, override=override_opt):
                with tempfile.TemporaryDirectory() as data_dir:
                    p = self._store_patches(data_dir)
                    with p[0], p[1], p[2], patch.dict(os.environ, self._env()):
                        payload, docx = self._generate_and_fetch(
                            _fe_payload(entity_id, override_opt, overridden=True)
                        )
                    text = extract_docx_text(docx)
                    # The forum is the SIGNING entity's OWN court (its default seat),
                    # not the overridden option's court.
                    expected_forum = _OPTION_FORUM[default_opt]
                    override_forum = _OPTION_FORUM[override_opt]
                    override_value = _law_value(override_opt)
                    default_value = _law_value(default_opt)
                    # The clause renders the legally-correct PHRASE, not the raw value.
                    override_phrase = _law_phrase(override_opt)
                    default_phrase = _law_phrase(default_opt)
                    # Forum provenance is the entity's own court on the manifest.
                    self.assertEqual(
                        payload["manifest"]["forum"], expected_forum,
                        f"{entity_id}: forum is not the signing entity's own court (got "
                        f"{payload['manifest']['forum']!r}, expected {expected_forum!r})",
                    )
                    # It must NOT have followed the override to another entity's court.
                    self.assertNotEqual(
                        payload["manifest"]["forum"], override_forum,
                        f"{entity_id}: forum wrongly followed the override option",
                    )
                    # The entity's own court IS rendered into the governing-law clause
                    # with exclusive-jurisdiction wording.
                    gov_line = next(
                        (
                            line
                            for line in text.splitlines()
                            if line.startswith("GOVERNING LAW AND JURISDICTION:")
                        ),
                        "",
                    )
                    self.assertIn(
                        f"{expected_forum} shall have exclusive jurisdiction", gov_line,
                        f"{entity_id}: signing entity's own forum not rendered in the clause",
                    )
                    # The override LAW still tracks into the prose; default does not leak.
                    self.assertIn(override_phrase, text, f"{entity_id}: override law not in prose")
                    if override_value != default_value:
                        # Scope the leak check to the GOVERNING LAW clause sentence.
                        # The whole document legitimately contains "the laws of
                        # <country>" in each party's INCORPORATION recital (e.g. an
                        # India-incorporated entity reads "incorporated under the laws
                        # of India"), which is not the governing law and must not be
                        # mistaken for a default-law leak.
                        gov_clause = next(
                            (
                                line
                                for line in text.splitlines()
                                if line.startswith("GOVERNING LAW AND JURISDICTION:")
                            ),
                            "",
                        )
                        self.assertIn(
                            f"the laws of {override_phrase}", gov_clause,
                            f"{entity_id}: override law not in the governing-law clause",
                        )
                        self.assertNotIn(
                            f"the laws of {default_phrase}", gov_clause,
                            f"{entity_id}: default law {default_value!r} leaked into the "
                            "overridden draft's governing-law clause",
                        )


if __name__ == "__main__":
    unittest.main()
