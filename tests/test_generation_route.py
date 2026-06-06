"""End-to-end tests for POST /api/generate-nda.

These drive the real HTTP handler (a threaded test server) the way draft-ui will:
post the intake + signing entity, get back a generated NDA that is a tracked
matter + a role=generated artifact, with a manifest and a passing self-check. The
generated .docx is downloadable over the matter-source route the response points
at. The route is exercised against the deterministic generation path (no API key),
so the assertions are repeatable and network-free.
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

from nda_automation import matter_store
from nda_automation import server as server_module
from nda_automation import telemetry
from nda_automation.review_engine import ACTIVE_REVIEW_ENGINE_ENV
from nda_automation.server import NdaAutomationHandler


class QuietHandler(NdaAutomationHandler):
    def log_message(self, *args, **kwargs):
        return


# Every signing entity in the registry should generate a Playbook-passing NDA.
ENTITY_IDS = ("aspora_technology", "vance_money", "real_transfer", "vance_techlabs")


class GenerateNdaRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
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

    # --- request helpers ------------------------------------------------- #
    def request(self, method, path, body=None, headers=None):
        request_headers = dict(headers or {})
        request_body = body
        if isinstance(body, dict):
            request_body = json.dumps(body).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        connection = http.client.HTTPConnection(self.host, self.port, timeout=10)
        try:
            connection.request(method, path, body=request_body, headers=request_headers)
            response = connection.getresponse()
            raw = response.read()
            content_type = response.getheader("Content-Type", "")
            payload = json.loads(raw.decode("utf-8")) if "application/json" in content_type else raw
            return response.status, payload, dict(response.getheaders())
        finally:
            connection.close()

    def basic_auth_headers(self, username="nda-admin", password="secret"):
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {token}"}

    def auth_env(self):
        return {
            "NDA_REQUIRE_AUTH": "true",
            "NDA_AUTH_USERNAME": "nda-admin",
            "NDA_AUTH_PASSWORD": "secret",
            ACTIVE_REVIEW_ENGINE_ENV: "deterministic",
        }

    def matter_store_patches(self, data_dir):
        data_path = server_module.Path(data_dir)
        return (
            patch.object(matter_store, "DATA_DIR", data_path),
            patch.object(matter_store, "MATTERS_PATH", data_path / "matters.json"),
            patch.object(matter_store, "UPLOADS_DIR", data_path / "uploads"),
        )

    def generate(self, body, *, headers=None):
        return self.request("POST", "/api/generate-nda", body, headers=headers)

    # --- tests ----------------------------------------------------------- #
    def test_requires_auth(self):
        with patch.dict(os.environ, self.auth_env()):
            status, payload, _ = self.generate(
                {"signing_entity_id": "aspora_technology", "intake": {"counterparty_name": "Acme"}}
            )
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"], server_module.AUTH_REQUIRED_MESSAGE)

    def test_generates_passing_nda_for_each_entity(self):
        for entity_id in ENTITY_IDS:
            with self.subTest(entity_id=entity_id):
                with tempfile.TemporaryDirectory() as data_dir:
                    p = self.matter_store_patches(data_dir)
                    with p[0], p[1], p[2], patch.dict(os.environ, self.auth_env()):
                        status, payload, _ = self.generate(
                            {
                                "signing_entity_id": entity_id,
                                "intake": {
                                    "counterparty_name": "Counterparty Holdings Limited",
                                    "project": "evaluating a potential commercial relationship",
                                    "term_years": 3,
                                    "nda_type": "mutual",
                                },
                            },
                            headers=self.basic_auth_headers(),
                        )
                        self.assertEqual(status, 201, payload)
                        self.assertEqual(payload["status"], "generated")
                        self.assertTrue(payload["matter_id"])
                        self.assertTrue(payload["artifact_id"])
                        self.assertEqual(
                            payload["download_url"],
                            f"/api/matters/{payload['matter_id']}/source",
                        )
                        # The whole point of the gate: the generated NDA passes
                        # its own Playbook with zero native + zero dynamic fails.
                        self.assertTrue(payload["self_check"]["passed"], payload["self_check"])
                        self.assertEqual(payload["self_check"]["native_failures"], [])
                        self.assertEqual(payload["self_check"]["dynamic_failures"], [])
                        # The manifest names the entity + counterparty it filled.
                        self.assertEqual(payload["manifest"]["entity_id"], entity_id)
                        self.assertEqual(
                            payload["manifest"]["counterparty_name"],
                            "Counterparty Holdings Limited",
                        )

    def test_download_url_serves_the_generated_docx(self):
        with tempfile.TemporaryDirectory() as data_dir:
            p = self.matter_store_patches(data_dir)
            with p[0], p[1], p[2], patch.dict(os.environ, self.auth_env()):
                status, payload, _ = self.generate(
                    {
                        "signing_entity_id": "aspora_technology",
                        "intake": {"counterparty_name": "Acme Corp Ltd"},
                    },
                    headers=self.basic_auth_headers(),
                )
                self.assertEqual(status, 201, payload)
                dl_status, dl_body, dl_headers = self.request(
                    "GET", payload["download_url"], headers=self.basic_auth_headers()
                )
        self.assertEqual(dl_status, 200)
        self.assertIn("wordprocessingml.document", dl_headers["Content-Type"])
        # A .docx is a zip: it starts with the PK signature.
        self.assertTrue(bytes(dl_body).startswith(b"PK"))

    def test_accepts_nested_signing_entity_and_flat_intake_aliases(self):
        with tempfile.TemporaryDirectory() as data_dir:
            p = self.matter_store_patches(data_dir)
            with p[0], p[1], p[2], patch.dict(os.environ, self.auth_env()):
                status, payload, _ = self.generate(
                    {
                        "signing_entity": {"id": "aspora_technology"},
                        "counterparty": {"name": "Nested Acme Ltd"},
                        "project_purpose": "a pilot integration",
                        "term": "3",
                    },
                    headers=self.basic_auth_headers(),
                )
        self.assertEqual(status, 201, payload)
        self.assertEqual(payload["manifest"]["counterparty_name"], "Nested Acme Ltd")

    def test_accepts_committed_fe_build_draft_payload_shape(self):
        # The EXACT shape static/js/modules/draft-intake.mjs:buildDraftPayload emits.
        fe_payload = {
            "counterparty": {"name": "Globex International Ltd", "email": "legal@globex.example"},
            "project_purpose": "evaluating a data-sharing integration",
            "term": "3 years",  # FREE TEXT — parsed to term_years server-side
            "nda_type": "mutual",
            "notes": "introduced via the partnerships team",
            "signing_entity": {
                "id": "aspora_technology",
                "legal_name": "Aspora Technology Services Private Limited",
                "address": {"id": "bengaluru", "label": "Bengaluru", "lines": ["MG Road"]},
                # aspora default is India; the FE picked England (overridden).
                "governing_law": {"playbook_option_id": "england_and_wales", "label": "England and Wales"},
                "governing_law_overridden": True,
            },
        }
        with tempfile.TemporaryDirectory() as data_dir:
            p = self.matter_store_patches(data_dir)
            with p[0], p[1], p[2], patch.dict(os.environ, self.auth_env()):
                status, payload, _ = self.generate(fe_payload, headers=self.basic_auth_headers())
        self.assertEqual(status, 201, payload)
        m = payload["manifest"]
        self.assertEqual(m["counterparty_name"], "Globex International Ltd")
        self.assertEqual(m["term_years"], 3)  # parsed from "3 years"
        # The FE-chosen governing law was applied + flagged with full provenance.
        self.assertEqual(m["governing_law_value"], "England and Wales")
        self.assertEqual(m["governing_law_option_id"], "england_and_wales")
        self.assertTrue(m["governing_law_overridden"])
        self.assertEqual(m["entity_default_governing_law_value"], "India")
        self.assertEqual(m["forum"], "Courts of England and Wales")
        self.assertTrue(payload["self_check"]["passed"], payload["self_check"])
        self.assertEqual(payload["download_url"], f"/api/matters/{payload['matter_id']}/source")

    def test_nested_fe_override_RENDERS_into_the_downloaded_doc(self):
        # Regression for the silent-drop class draft-ui caught: the override is
        # nested ONLY at signing_entity.governing_law.playbook_option_id (the real FE
        # shape — NO top-level field). A manifest-only assertion can pass even if the
        # parser read the wrong level, so this drives the REAL FE payload through the
        # route AND downloads the rendered .docx to assert the GOVERNING LAW clause
        # actually NAMES the override (aspora default India -> Delaware).
        from io import BytesIO

        from nda_automation.docx_text import extract_docx_text

        fe_payload = {
            "counterparty": {"name": "Acme Ltd", "email": None},
            "project_purpose": "a pilot",
            "term": "3",
            "nda_type": "mutual",
            "notes": "",
            "signing_entity": {
                "id": "aspora_technology",  # registry default = India
                "legal_name": "Aspora Technology Services Private Limited",
                "address": {"id": "b", "label": "B", "lines": ["MG Road"]},
                "governing_law": {"playbook_option_id": "delaware", "label": "Delaware"},
                "governing_law_overridden": True,
            },
        }
        # Sanity: the real FE payload carries NO top-level override field.
        self.assertNotIn("governing_law_override", fe_payload)
        self.assertNotIn("governing_law", fe_payload)
        with tempfile.TemporaryDirectory() as data_dir:
            p = self.matter_store_patches(data_dir)
            with p[0], p[1], p[2], patch.dict(os.environ, self.auth_env()):
                status, payload, _ = self.generate(fe_payload, headers=self.basic_auth_headers())
                self.assertEqual(status, 201, payload)
                self.assertEqual(payload["manifest"]["governing_law_value"], "Delaware")
                # Download the actual rendered .docx and assert the CLAUSE names the override.
                dl_status, dl_body, _ = self.request(
                    "GET", payload["download_url"], headers=self.basic_auth_headers()
                )
        self.assertEqual(dl_status, 200)
        text = extract_docx_text(BytesIO(bytes(dl_body)).getvalue())
        gov_line = next(line for line in text.split("\n") if "GOVERNING LAW" in line.upper())
        self.assertIn("laws of Delaware", gov_line)
        self.assertNotIn("laws of India", gov_line)  # did NOT silently snap to the entity default

    def test_fe_payload_without_override_keeps_entity_default(self):
        # signing_entity.governing_law present but matching the entity default ->
        # no-op, governing_law_overridden False.
        fe_payload = {
            "counterparty": {"name": "Initech Ltd", "email": None},
            "project_purpose": "a pilot",
            "term": "2",
            "nda_type": "mutual",
            "notes": "",
            "signing_entity": {
                "id": "aspora_technology",
                "legal_name": "Aspora Technology Services Private Limited",
                "address": None,
                "governing_law": {"playbook_option_id": "india", "label": "India"},
                "governing_law_overridden": False,
            },
        }
        with tempfile.TemporaryDirectory() as data_dir:
            p = self.matter_store_patches(data_dir)
            with p[0], p[1], p[2], patch.dict(os.environ, self.auth_env()):
                status, payload, _ = self.generate(fe_payload, headers=self.basic_auth_headers())
        self.assertEqual(status, 201, payload)
        self.assertEqual(payload["manifest"]["governing_law_value"], "India")
        self.assertFalse(payload["manifest"]["governing_law_overridden"])

    def test_missing_counterparty_name_is_rejected(self):
        with patch.dict(os.environ, self.auth_env()):
            status, payload, _ = self.generate(
                {"signing_entity_id": "aspora_technology", "intake": {}},
                headers=self.basic_auth_headers(),
            )
        self.assertEqual(status, 400)
        self.assertIn("counterparty", payload["error"].lower())

    def test_missing_signing_entity_is_rejected(self):
        with patch.dict(os.environ, self.auth_env()):
            status, payload, _ = self.generate(
                {"intake": {"counterparty_name": "Acme"}},
                headers=self.basic_auth_headers(),
            )
        self.assertEqual(status, 400)
        self.assertIn("signing entity", payload["error"].lower())

    def test_unknown_entity_is_rejected(self):
        with patch.dict(os.environ, self.auth_env()):
            status, payload, _ = self.generate(
                {"signing_entity_id": "not_a_real_entity", "intake": {"counterparty_name": "Acme"}},
                headers=self.basic_auth_headers(),
            )
        self.assertEqual(status, 400)
        self.assertIn("entity", payload["error"].lower())

    def test_one_way_nda_type_is_rejected(self):
        with patch.dict(os.environ, self.auth_env()):
            status, payload, _ = self.generate(
                {
                    "signing_entity_id": "aspora_technology",
                    "intake": {"counterparty_name": "Acme", "nda_type": "one_way"},
                },
                headers=self.basic_auth_headers(),
            )
        self.assertEqual(status, 400)
        self.assertIn("one_way", payload["error"])

    def test_governing_law_override_to_approved_law_is_applied(self):
        with tempfile.TemporaryDirectory() as data_dir:
            p = self.matter_store_patches(data_dir)
            with p[0], p[1], p[2], patch.dict(os.environ, self.auth_env()):
                # The FE carries the chosen law INSIDE signing_entity.governing_law —
                # there is no top-level governing_law_override string.
                status, payload, _ = self.generate(
                    {
                        "signing_entity": {
                            "id": "aspora_technology",  # default India; picked England
                            "governing_law": {"playbook_option_id": "england_and_wales"},
                            "governing_law_overridden": True,
                        },
                        "counterparty": {"name": "Acme Corp Ltd"},
                    },
                    headers=self.basic_auth_headers(),
                )
        self.assertEqual(status, 201, payload)
        self.assertEqual(payload["manifest"]["governing_law_value"], "England and Wales")
        self.assertTrue(payload["manifest"]["governing_law_overridden"])
        self.assertEqual(payload["manifest"]["entity_default_governing_law_value"], "India")
        self.assertTrue(payload["self_check"]["passed"], payload["self_check"])

    def test_unapproved_governing_law_option_is_rejected(self):
        with patch.dict(os.environ, self.auth_env()):
            status, payload, _ = self.generate(
                {
                    "signing_entity": {
                        "id": "aspora_technology",
                        "governing_law": {"playbook_option_id": "new_york"},
                        "governing_law_overridden": True,
                    },
                    "counterparty": {"name": "Acme"},
                },
                headers=self.basic_auth_headers(),
            )
        self.assertEqual(status, 400)
        self.assertIn("not an approved", payload["error"].lower())

    def test_off_position_draft_is_blocked_by_the_safety_gate_before_save(self):
        # DEFECT-2 regression: the hard safety gate must run on the ACTUAL endpoint
        # ship path (the route builds the matter+artifact itself, not via
        # generate_and_save_nda). If a generated draft ever carries a prohibited
        # position, the endpoint must 400 with a safety-gate message AND persist
        # NOTHING. We force an off-position draft by tampering the generated bytes.
        from io import BytesIO

        from docx import Document

        from nda_automation import nda_generation

        # Capture the REAL generator before patching, so the wrapper doesn't
        # re-enter its own mock (infinite recursion).
        real_generate = nda_generation.generate_nda_for_entity

        def tampered_generate(entity_id, intake, **kwargs):
            result = real_generate(entity_id, intake, **kwargs)
            document = Document(BytesIO(result.docx_bytes))
            document.add_paragraph("The Receiving Party shall not compete with the Disclosing Party.")
            with BytesIO() as buffer:
                document.save(buffer)
                return nda_generation.GenerationResult(docx_bytes=buffer.getvalue(), manifest=result.manifest)

        with tempfile.TemporaryDirectory() as data_dir:
            p = self.matter_store_patches(data_dir)
            with p[0], p[1], p[2], patch.dict(os.environ, self.auth_env()), patch(
                "nda_automation.routes.generation.nda_generation.generate_nda_for_entity",
                side_effect=tampered_generate,
            ):
                status, payload, _ = self.generate(
                    {"signing_entity_id": "aspora_technology", "intake": {"counterparty_name": "Acme"}},
                    headers=self.basic_auth_headers(),
                )
                # 4xx with a clear safety-gate message; the document is never returned.
                self.assertEqual(status, 400)
                self.assertIn("safety gate", payload["error"].lower())
                self.assertNotIn("matter_id", payload)
                # Nothing was persisted — no generated matter leaked into the store.
                # (Basic-auth owner id is the username verbatim.)
                matters = matter_store.list_matters(owner_user_id="nda-admin")
                self.assertEqual(
                    [m for m in matters if m.get("source_type") == "generated"], []
                )


if __name__ == "__main__":
    unittest.main()
