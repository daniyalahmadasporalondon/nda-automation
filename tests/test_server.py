import base64
import http.client
import json
import os
import socket
import tempfile
import threading
import unittest
from copy import deepcopy
from http.server import ThreadingHTTPServer
from io import BytesIO
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile
import xml.etree.ElementTree as ET

from nda_automation.checker import ParagraphAlignmentError, PlaybookTemplateError, load_playbook
from nda_automation import document_limits
from nda_automation import docx_text
from nda_automation.docx_export import DOCX_MIME
from nda_automation import export_service
from nda_automation import gmail_integration
from nda_automation import matter_store
from nda_automation import matter_view
from nda_automation import server as server_module
from nda_automation.server import NdaAutomationHandler
from tests.docx_redline_contract import assert_docx_redline_contract

W_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
SOURCE_EXPORT_REPORT_LEAKAGE_PHRASES = [
    "NDA Redline",
    "Review Notes",
    "Clause Findings",
    "Proposed Redline",
    "Overall status:",
    "Requirements passed:",
    "Requirements failed:",
    "Checked at:",
    "The Redlined NDA section contains native Word tracked changes.",
    "source paragraph",
]


class QuietNdaAutomationHandler(NdaAutomationHandler):
    def log_message(self, format, *args):
        return


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), QuietNdaAutomationHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.host, cls.port = cls.server.server_address

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def request(self, method, path, body=None, headers=None):
        status, payload, _response_headers = self.request_with_headers(method, path, body=body, headers=headers)
        return status, payload

    def request_with_headers(self, method, path, body=None, headers=None):
        request_headers = headers or {}
        request_body = body
        if isinstance(body, dict):
            request_body = json.dumps(body).encode("utf-8")
            request_headers = {"Content-Type": "application/json", **request_headers}
        elif isinstance(body, str):
            request_body = body.encode("utf-8")

        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            connection.request(method, path, body=request_body, headers=request_headers)
            response = connection.getresponse()
            raw_body = response.read()
            content_type = response.getheader("Content-Type", "")
            if "application/json" in content_type:
                payload = json.loads(raw_body.decode("utf-8"))
            else:
                payload = raw_body
            return response.status, payload, dict(response.getheaders())
        finally:
            connection.close()

    def raw_http_request(self, request_text):
        with socket.create_connection((self.host, self.port), timeout=5) as connection:
            connection.sendall(request_text.encode("utf-8"))
            connection.shutdown(socket.SHUT_WR)
            chunks = []
            while True:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        raw_response = b"".join(chunks)
        header_bytes, _, body = raw_response.partition(b"\r\n\r\n")
        status_line = header_bytes.splitlines()[0].decode("iso-8859-1")
        status = int(status_line.split()[1])
        return status, json.loads(body.decode("utf-8"))

    def assert_saved_export_url_matches_response(self, headers, payload):
        self.assertEqual(headers["X-Export-Verified"], "word-package; track-revisions")
        self.assertIn("X-Export-URL", headers)
        route_status, route_payload, route_headers = self.request_with_headers("GET", headers["X-Export-URL"])

        self.assertEqual(route_status, 200)
        self.assertEqual(route_headers["Content-Type"], DOCX_MIME)
        self.assertEqual(route_headers["Content-Disposition"], headers["Content-Disposition"])
        self.assertEqual(route_payload, payload)
        self.assertEqual(server_module.Path(headers["X-Export-Path"]).read_bytes(), payload)

    def matter_store_patches(self, data_dir):
        data_path = server_module.Path(data_dir)
        return (
            patch.object(matter_store, "DATA_DIR", data_path),
            patch.object(matter_store, "MATTERS_PATH", data_path / "matters.json"),
            patch.object(matter_store, "UPLOADS_DIR", data_path / "uploads"),
        )

    def assert_review_payload_contract(self, payload, *, expected_source_type=None):
        for key in ["overall_status", "checked_at", "requirements_passed", "requirements_failed", "paragraphs", "clauses", "redline_edits"]:
            self.assertIn(key, payload)
        self.assertEqual(payload.get("evidence_trust"), {"status": "verified", "errors": []})
        self.assertIn(payload["overall_status"], {"meets_requirements", "does_not_meet_requirements"})
        self.assertIsInstance(payload["requirements_passed"], int)
        self.assertIsInstance(payload["requirements_failed"], int)
        self.assertIsInstance(payload["paragraphs"], list)
        self.assertIsInstance(payload["clauses"], list)
        self.assertIsInstance(payload["redline_edits"], list)

        paragraphs_by_id = {}
        for paragraph in payload["paragraphs"]:
            for key in ["id", "index", "text", "start", "end"]:
                self.assertIn(key, paragraph)
            self.assertIsInstance(paragraph["id"], str)
            self.assertIsInstance(paragraph["index"], int)
            self.assertIsInstance(paragraph["text"], str)
            self.assertIsInstance(paragraph["start"], int)
            self.assertIsInstance(paragraph["end"], int)
            paragraphs_by_id[paragraph["id"]] = paragraph

        for clause in payload["clauses"]:
            for key in [
                "id",
                "name",
                "requirement",
                "status",
                "passes",
                "issue_type",
                "issue_label",
                "what_to_fix",
                "reason",
                "finding",
                "matched_paragraph_ids",
                "matched_text",
                "evidence",
                "evidence_paragraphs",
            ]:
                self.assertIn(key, clause, f"{clause.get('id', 'unknown')} missing {key}")
            self.assertIn(clause["status"], {"match", "check", "not_present"})
            self.assertIsInstance(clause["passes"], bool)
            self.assertEqual(clause["reason"], clause["finding"])
            matched_ids = clause["matched_paragraph_ids"]
            self.assertIsInstance(matched_ids, list)
            expected_paragraphs = [paragraphs_by_id[paragraph_id] for paragraph_id in matched_ids]
            self.assertEqual(clause["matched_text"], "\n\n".join(paragraph["text"] for paragraph in expected_paragraphs))
            self.assertEqual(clause["evidence"], [paragraph["text"] for paragraph in expected_paragraphs])
            self.assertEqual(clause["evidence_paragraphs"], expected_paragraphs)

        for redline in payload["redline_edits"]:
            for key in ["id", "clause_id", "clause_name", "paragraph_id", "paragraph_index", "action", "action_label", "status", "original_text", "replacement_text", "reason"]:
                self.assertIn(key, redline)
            self.assertNotIn("target_position", redline)
            self.assertNotIn("selected_template_id", redline)
            self.assertIn(redline["paragraph_id"], paragraphs_by_id)
            self.assertEqual(redline["paragraph_index"], paragraphs_by_id[redline["paragraph_id"]]["index"])
            self.assertIn(redline["action"], {"replace_paragraph", "insert_after_paragraph", "delete_paragraph"})
            if redline["action"] == "insert_after_paragraph":
                self.assertEqual(redline["original_text"], "")
                self.assertIn("anchor_text", redline)
                self.assertIn("insert_text", redline)
                self.assertTrue(redline["insert_text"].strip())
            elif redline["action"] == "delete_paragraph":
                self.assertEqual(redline["replacement_text"], "")
                self.assertTrue(redline["original_text"].strip())
            else:
                self.assertTrue(redline["original_text"].strip())
                self.assertTrue(redline["replacement_text"].strip())
                self.assertIn("inline_diff_operations", redline)
                self.assertTrue(redline["inline_diff_operations"])
                for operation in redline["inline_diff_operations"]:
                    self.assertIn(operation["type"], {"same", "delete", "insert"})
                    self.assertIsInstance(operation["token"], str)
                for option in redline.get("template_options", []):
                    self.assertIn("inline_diff_operations", option)
                    self.assertTrue(option["inline_diff_operations"])

        if expected_source_type:
            self.assertEqual(payload["source"]["type"], expected_source_type)
            self.assertIn("extracted_text", payload)
            for paragraph in payload["paragraphs"]:
                self.assertIn("source_index", paragraph)

    def malformed_template_playbook(self):
        playbook = deepcopy(load_playbook())
        term = next(clause for clause in playbook["clauses"] if clause["id"] == "term_and_survival")
        term["redline_template"] = "Custom survival language capped at {unknown_placeholder}."
        return playbook

    def test_text_review_rejects_bad_json(self):
        status, payload = self.request(
            "POST",
            "/api/review",
            "{not json",
            {"Content-Type": "application/json"},
        )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "Request body must be valid JSON.")

    def test_text_review_rejects_non_object_json(self):
        status, payload = self.request(
            "POST",
            "/api/review",
            "[]",
            {"Content-Type": "application/json"},
        )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "Request body must be a JSON object.")

    def test_text_review_rejects_empty_content_length(self):
        status, payload = self.raw_http_request(
            "POST /api/review HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: \r\n"
            "Connection: close\r\n"
            "\r\n"
        )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "Content-Length must be a non-negative integer.")

    def test_text_review_rejects_malformed_content_length(self):
        status, payload = self.raw_http_request(
            "POST /api/review HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: nope\r\n"
            "Connection: close\r\n"
            "\r\n"
            "{}"
        )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "Content-Length must be a non-negative integer.")

    def test_text_review_rejects_negative_content_length(self):
        status, payload = self.raw_http_request(
            "POST /api/review HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: -1\r\n"
            "Connection: close\r\n"
            "\r\n"
            "{}"
        )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "Content-Length must be a non-negative integer.")

    def test_text_review_rejects_empty_text(self):
        status, payload = self.request("POST", "/api/review", {"text": "   "})

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "Provide NDA text to review.")

    def test_text_review_returns_clause_results(self):
        status, payload = self.request(
            "POST",
            "/api/review",
            {"text": "Each party agrees this NDA is governed by the laws of the DIFC for two years."},
        )

        self.assertEqual(status, 200)
        self.assertIn("clauses", payload)
        self.assertIn("redline_edits", payload)
        self.assert_review_payload_contract(payload)

    def test_review_payload_contract_covers_pass_check_and_missing_flows(self):
        scenarios = [
            ("pass", "Each party may disclose Confidential Information and this Agreement is governed by the laws of the DIFC for two years.\n\nFor Aspora Ltd\nBy: A. Signatory\nTitle: Director\nDate: 2026-05-30\n\nFor Counterparty Ltd\nBy: B. Signatory\nTitle: CEO\nDate: 2026-05-30"),
            ("check", "The confidentiality obligations survive for seven years.\n\nThe Recipient must not circumvent the Company."),
            ("missing", "The parties will discuss a possible transaction."),
        ]

        for name, text in scenarios:
            with self.subTest(name=name):
                status, payload = self.request("POST", "/api/review", {"text": text})

                self.assertEqual(status, 200)
                self.assert_review_payload_contract(payload)

    def test_review_payload_contract_covers_uploaded_docx_flow(self):
        source_docx = make_docx([
            "Intro paragraph.",
            "This Agreement shall be governed by the laws of California.",
            "The Recipient must not circumvent the Company.",
        ])

        status, payload = self.request(
            "POST",
            "/api/review-document",
            {
                "filename": "uploaded.docx",
                "content_base64": base64.b64encode(source_docx).decode("ascii"),
            },
        )

        self.assertEqual(status, 200)
        self.assert_review_payload_contract(payload, expected_source_type="docx")

    def test_review_payload_contract_covers_uploaded_pdf_flow(self):
        source_pdf = make_pdf("This Agreement shall be governed by the laws of California.")

        status, payload = self.request(
            "POST",
            "/api/review-document",
            {
                "filename": "uploaded.pdf",
                "content_base64": base64.b64encode(source_pdf).decode("ascii"),
            },
        )

        self.assertEqual(status, 200)
        self.assert_review_payload_contract(payload, expected_source_type="pdf")
        self.assertIn("California", payload["extracted_text"])
        self.assertEqual(payload["source"]["extraction_quality"]["page_count"], 1)
        self.assertEqual(payload["source"]["extraction_quality"]["pages_with_text"], 1)
        self.assertIn("warnings", payload["source"]["extraction_quality"])

    def test_matter_upload_creates_persisted_gmail_demo_matter(self):
        source_docx = make_docx([
            "Intro paragraph.",
            "This Agreement shall be governed by the laws of California.",
            "The Recipient must not circumvent the Company.",
        ])

        with tempfile.TemporaryDirectory() as data_dir:
            with self.matter_store_patches(data_dir)[0], self.matter_store_patches(data_dir)[1], self.matter_store_patches(data_dir)[2]:
                status, payload = self.request(
                    "POST",
                    "/api/matters",
                    {
                        "filename": "Acme NDA.docx",
                        "content_base64": base64.b64encode(source_docx).decode("ascii"),
                        "source_type": "gmail_demo",
                    },
                )
                list_status, list_payload = self.request("GET", "/api/matters")
                matter = payload["matter"]
                fetch_status, fetch_payload = self.request("GET", f"/api/matters/{matter['id']}")
                stored_matter = matter_store.get_matter(matter["id"])
                stored_path = matter_store.UPLOADS_DIR / stored_matter["stored_filename"]
                stored_bytes = stored_path.read_bytes()

        self.assertEqual(status, 201)
        self.assertEqual(matter["source_type"], "gmail_demo")
        self.assertEqual(matter["board_column"], "gmail_demo")
        self.assertEqual(matter["source_filename"], "Acme NDA.docx")
        self.assertEqual(matter["document_title"], "Acme NDA")
        self.assertEqual(matter["sender"], "Manual upload")
        self.assertEqual(matter["recipient_email"], "")
        self.assertEqual(matter["can_send_redline"], False)
        self.assertEqual(matter["subject"], "Acme NDA")
        self.assertEqual(matter["attachment_filename"], "Acme NDA.docx")
        self.assertEqual(matter["message_snippet"], "Manual upload of Acme NDA.docx.")
        self.assertIn("received_at", matter)
        self.assertEqual(matter["triage_status"], "legal_review")
        self.assertEqual(matter["next_action"], "Needs legal review")
        self.assertGreaterEqual(matter["issue_count"], 1)
        self.assertIn("review_result", matter)
        self.assertIn("extracted_text", matter)
        self.assertNotIn("stored_filename", matter)
        self.assertNotIn("gmail_message_id", matter)
        self.assertEqual(stored_bytes, source_docx)
        self.assertEqual(list_status, 200)
        self.assertEqual([item["id"] for item in list_payload["matters"]], [matter["id"]])
        self.assertEqual(fetch_status, 200)
        self.assertEqual(fetch_payload["matter"]["id"], matter["id"])
        self.assertEqual(fetch_payload["matter"]["sender"], "Manual upload")
        self.assertEqual(fetch_payload["matter"]["can_send_redline"], False)

    def test_demo_reset_clears_repository_and_uploaded_documents(self):
        source_docx = make_docx([
            "This Agreement shall be governed by the laws of California.",
        ])

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                create_status, create_payload = self.request(
                    "POST",
                    "/api/matters",
                    {
                        "filename": "Reset NDA.docx",
                        "content_base64": base64.b64encode(source_docx).decode("ascii"),
                        "source_type": "gmail_demo",
                    },
                )
                matter = create_payload["matter"]
                stored_matter = matter_store.get_matter(matter["id"])
                stored_path = matter_store.UPLOADS_DIR / stored_matter["stored_filename"]
                reset_status, reset_payload = self.request("POST", "/api/demo/reset")
                stored_path_exists = stored_path.exists()
                list_status, list_payload = self.request("GET", "/api/matters")

        self.assertEqual(create_status, 201)
        self.assertEqual(reset_status, 200)
        self.assertEqual(reset_payload["removed"], 1)
        self.assertEqual(reset_payload["matters"], [])
        self.assertEqual(list_status, 200)
        self.assertEqual(list_payload["matters"], [])
        self.assertFalse(stored_path_exists)

    def test_public_matter_uses_explicit_allowlist(self):
        public = matter_view.public_matter({
            "id": "matter_1",
            "sender": "Sender <sender@example.com>",
            "subject": "NDA",
            "stored_filename": "internal.docx",
            "gmail_message_id": "msg_123",
            "gmail_attachment_id": "att_123",
            "review_result": {"clauses": []},
            "extracted_text": "Text",
        })

        self.assertEqual(public["id"], "matter_1")
        self.assertEqual(public["recipient_email"], "sender@example.com")
        self.assertEqual(public["can_send_redline"], True)
        self.assertNotIn("stored_filename", public)
        self.assertNotIn("gmail_message_id", public)
        self.assertNotIn("gmail_attachment_id", public)

    def test_matter_retention_prunes_old_closed_uploads(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patch.dict(os.environ, {"NDA_MATTER_RETENTION_LIMIT": "2"}):
                with patches[0], patches[1], patches[2]:
                    first = matter_store.create_matter(
                        source_filename="first.docx",
                        document_bytes=b"first",
                        extracted_text="first",
                        review_result={"clauses": []},
                        triage={},
                    )
                    first_path = matter_store.UPLOADS_DIR / first["stored_filename"]
                    matter_store.update_matter_stage(first["id"], "signed_closed")
                    second = matter_store.create_matter(
                        source_filename="second.docx",
                        document_bytes=b"second",
                        extracted_text="second",
                        review_result={"clauses": []},
                        triage={},
                    )
                    third = matter_store.create_matter(
                        source_filename="third.docx",
                        document_bytes=b"third",
                        extracted_text="third",
                        review_result={"clauses": []},
                        triage={},
                    )
                    matters = matter_store.list_matters()

        self.assertEqual({matter["id"] for matter in matters}, {second["id"], third["id"]})
        self.assertFalse(first_path.exists())

    def test_gmail_status_requires_env_token_paths(self):
        with patch.dict(os.environ, {
            gmail_integration.ROLE_TOKEN_ENV["inbound"]: "",
            gmail_integration.ROLE_TOKEN_ENV["outbound"]: "",
        }, clear=False):
            status = gmail_integration.gmail_status()

        self.assertEqual(status["inbound"]["configured"], False)
        self.assertEqual(status["outbound"]["configured"], False)
        self.assertIn(gmail_integration.ROLE_TOKEN_ENV["inbound"], status["inbound"]["error"])

    def test_matter_stage_update_persists_workflow_column(self):
        source_docx = make_docx([
            "This Agreement shall be governed by the laws of California.",
        ])

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                create_status, create_payload = self.request(
                    "POST",
                    "/api/matters",
                    {
                        "filename": "Workflow NDA.docx",
                        "content_base64": base64.b64encode(source_docx).decode("ascii"),
                    },
                )
                matter_id = create_payload["matter"]["id"]
                review_status, review_payload = self.request(
                    "POST",
                    f"/api/matters/{matter_id}/stage",
                    {"board_column": "in_review"},
                )
                list_status, list_payload = self.request("GET", "/api/matters")
                close_status, close_payload = self.request(
                    "POST",
                    f"/api/matters/{matter_id}/stage",
                    {"board_column": "signed_closed"},
                )
                invalid_status, invalid_payload = self.request(
                    "POST",
                    f"/api/matters/{matter_id}/stage",
                    {"board_column": "unknown"},
                )
                missing_status, missing_payload = self.request(
                    "POST",
                    "/api/matters/matter_missing/stage",
                    {"board_column": "in_review"},
                )

        self.assertEqual(create_status, 201)
        self.assertEqual(review_status, 200)
        self.assertEqual(review_payload["matter"]["board_column"], "in_review")
        self.assertEqual(review_payload["matter"]["status"], "active")
        self.assertEqual(list_status, 200)
        self.assertEqual(list_payload["matters"][0]["board_column"], "in_review")
        self.assertEqual(close_status, 200)
        self.assertEqual(close_payload["matter"]["board_column"], "signed_closed")
        self.assertEqual(close_payload["matter"]["status"], "closed")
        self.assertEqual(invalid_status, 400)
        self.assertEqual(invalid_payload["error"], "Unsupported matter stage.")
        self.assertEqual(missing_status, 404)
        self.assertEqual(missing_payload["error"], "Matter not found.")

    def test_matter_upload_preserves_email_intake_metadata(self):
        source_docx = make_docx([
            "This Agreement shall be governed by the laws of California.",
        ])

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                status, payload = self.request(
                    "POST",
                    "/api/matters",
                    {
                        "filename": "Attached NDA.docx",
                        "content_base64": base64.b64encode(source_docx).decode("ascii"),
                        "sender": "legal@example.com",
                        "subject": "Please review our NDA",
                        "received_at": "2026-05-31T10:15:00+01:00",
                        "message_snippet": "Hi team, please review the attached NDA.",
                        "attachment_filename": "Counterparty NDA.docx",
                        "source_type": "gmail_inbound",
                        "gmail_account": "inbound@example.com",
                        "gmail_attachment_id": "att_123",
                        "gmail_message_id": "msg_123",
                        "gmail_thread_id": "thr_123",
                    },
                )
                duplicate = matter_store.find_gmail_attachment("msg_123", "att_123")
                stored_matter = matter_store.get_matter(payload["matter"]["id"])

        self.assertEqual(status, 201)
        matter = payload["matter"]
        self.assertEqual(matter["source_type"], "gmail_inbound")
        self.assertEqual(matter["board_column"], "gmail_demo")
        self.assertEqual(matter["sender"], "legal@example.com")
        self.assertEqual(matter["recipient_email"], "legal@example.com")
        self.assertEqual(matter["can_send_redline"], True)
        self.assertEqual(matter["subject"], "Please review our NDA")
        self.assertEqual(matter["received_at"], "2026-05-31T10:15:00+01:00")
        self.assertEqual(matter["message_snippet"], "Hi team, please review the attached NDA.")
        self.assertEqual(matter["attachment_filename"], "Counterparty NDA.docx")
        self.assertNotIn("gmail_account", matter)
        self.assertNotIn("gmail_attachment_id", matter)
        self.assertNotIn("gmail_message_id", matter)
        self.assertNotIn("gmail_thread_id", matter)
        self.assertEqual(stored_matter["gmail_account"], "inbound@example.com")
        self.assertEqual(stored_matter["gmail_attachment_id"], "att_123")
        self.assertEqual(stored_matter["gmail_message_id"], "msg_123")
        self.assertEqual(stored_matter["gmail_thread_id"], "thr_123")
        self.assertEqual(duplicate["id"], matter["id"])

    def test_matter_upload_rejects_invalid_upload_cleanly(self):
        with tempfile.TemporaryDirectory() as data_dir:
            with self.matter_store_patches(data_dir)[0], self.matter_store_patches(data_dir)[1], self.matter_store_patches(data_dir)[2]:
                status, payload = self.request(
                    "POST",
                    "/api/matters",
                    {
                        "filename": "nda.txt",
                        "content_base64": base64.b64encode(b"not docx").decode("ascii"),
                    },
                )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "Upload a .docx Word document or text-based PDF.")

    def test_matter_upload_rejects_unsupported_source(self):
        source_docx = make_docx(["This Agreement shall be governed by the laws of California."])

        with tempfile.TemporaryDirectory() as data_dir:
            with self.matter_store_patches(data_dir)[0], self.matter_store_patches(data_dir)[1], self.matter_store_patches(data_dir)[2]:
                status, payload = self.request(
                    "POST",
                    "/api/matters",
                    {
                        "filename": "Acme NDA.docx",
                        "content_base64": base64.b64encode(source_docx).decode("ascii"),
                        "source_type": "unknown_board",
                    },
                )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "Unsupported matter source.")

    def test_matter_export_uses_preserved_original_docx(self):
        source_docx = make_docx([
            "This Agreement shall be governed by the laws of California.",
        ])

        with tempfile.TemporaryDirectory() as data_dir:
            with self.matter_store_patches(data_dir)[0], self.matter_store_patches(data_dir)[1], self.matter_store_patches(data_dir)[2]:
                create_status, create_payload = self.request(
                    "POST",
                    "/api/matters",
                    {
                        "filename": "Acme NDA.docx",
                        "content_base64": base64.b64encode(source_docx).decode("ascii"),
                    },
                )
                matter_id = create_payload["matter"]["id"]
                export_status, export_payload, export_headers = self.request_with_headers(
                    "POST",
                    "/api/export-review-docx",
                    {
                        "matter_id": matter_id,
                        "reviewed_text": "Stale reviewed text should not appear.",
                    },
                )

        self.assertEqual(create_status, 201)
        self.assertEqual(export_status, 200)
        self.assertEqual(export_headers["Content-Disposition"], 'attachment; filename="Acme-NDA-redlined.docx"')
        assert_source_export_has_no_report_leakage(
            self,
            export_payload,
            extra_forbidden=["Stale reviewed text should not appear."],
        )
        with ZipFile(BytesIO(export_payload)) as archive:
            document_xml = archive.read("word/document.xml").decode("utf-8")
        self.assertIn("California", document_xml)

    def test_pdf_matter_export_uses_review_report_docx(self):
        source_pdf = make_pdf("This Agreement shall be governed by the laws of California.")

        with tempfile.TemporaryDirectory() as data_dir:
            with self.matter_store_patches(data_dir)[0], self.matter_store_patches(data_dir)[1], self.matter_store_patches(data_dir)[2]:
                create_status, create_payload = self.request(
                    "POST",
                    "/api/matters",
                    {
                        "filename": "Acme NDA.pdf",
                        "content_base64": base64.b64encode(source_pdf).decode("ascii"),
                    },
                )
                matter = create_payload["matter"]
                export_status, export_payload, export_headers = self.request_with_headers(
                    "POST",
                    "/api/export-review-docx",
                    {"matter_id": matter["id"]},
                )

        self.assertEqual(create_status, 201)
        self.assertEqual(matter["source_filename"], "Acme NDA.pdf")
        self.assertEqual(matter["review_result"]["source"]["type"], "pdf")
        self.assertEqual(export_status, 200)
        self.assertEqual(export_headers["Content-Disposition"], 'attachment; filename="Acme-NDA-redlined.docx"')
        with ZipFile(BytesIO(export_payload)) as archive:
            document_xml = archive.read("word/document.xml").decode("utf-8")
        self.assertIn("NDA Redline", document_xml)
        self.assertIn("California", document_xml)

    def test_matter_export_fails_when_source_docx_is_missing(self):
        source_docx = make_docx([
            "This Agreement shall be governed by the laws of California.",
        ])

        with tempfile.TemporaryDirectory() as data_dir:
            with self.matter_store_patches(data_dir)[0], self.matter_store_patches(data_dir)[1], self.matter_store_patches(data_dir)[2]:
                create_status, create_payload = self.request(
                    "POST",
                    "/api/matters",
                    {
                        "filename": "Acme NDA.docx",
                        "content_base64": base64.b64encode(source_docx).decode("ascii"),
                    },
                )
                matter = create_payload["matter"]
                stored_matter = matter_store.get_matter(matter["id"])
                (matter_store.UPLOADS_DIR / stored_matter["stored_filename"]).unlink()
                export_status, export_payload = self.request(
                    "POST",
                    "/api/export-review-docx",
                    {"matter_id": matter["id"]},
                )

        self.assertEqual(create_status, 201)
        self.assertEqual(export_status, 400)
        self.assertEqual(export_payload["error"], "Matter source document is missing from storage.")

    def test_gmail_import_endpoint_uses_inbound_connector(self):
        with patch.object(server_module.gmail_integration, "import_inbound_matters", return_value={
            "account": "inbound@example.com",
            "imported": [{"id": "matter_1"}],
            "query": "has:attachment",
            "skipped": [{"message_id": "m1", "reason": "no_reviewable_attachment"}],
        }) as import_inbound_matters:
            status, payload = self.request(
                "POST",
                "/api/gmail/import",
                {"limit": 2, "query": "has:attachment"},
            )

        self.assertEqual(status, 200)
        self.assertEqual(payload["imported"][0]["id"], "matter_1")
        self.assertEqual(payload["imported"][0]["recipient_email"], "")
        self.assertEqual(payload["imported"][0]["can_send_redline"], False)
        self.assertEqual(payload["skipped"][0]["reason"], "no_reviewable_attachment")
        self.assertIsInstance(payload["synced_at"], str)
        self.assertTrue(payload["synced_at"])
        import_inbound_matters.assert_called_once_with(limit=2, query="has:attachment")

    def test_gmail_send_payload_replies_in_thread_only_for_same_account(self):
        class FakeExecutable:
            def __init__(self, payload):
                self.payload = payload

            def execute(self):
                return self.payload

        class FakeUsers:
            def __init__(self, email):
                self.email = email
                self.sent_body = None

            def getProfile(self, userId):
                return FakeExecutable({"emailAddress": self.email})

            def messages(self):
                return self

            def send(self, userId, body):
                self.sent_body = body
                return FakeExecutable({"id": "sent_1", "threadId": body.get("threadId", "new_thread")})

        class FakeGmailService:
            def __init__(self, email):
                self.users_api = FakeUsers(email)

            def users(self):
                return self.users_api

        same_account_service = FakeGmailService("legal@aspora.com")
        different_account_service = FakeGmailService("outbound@gmail.com")
        base_matter = {
            "gmail_account": "legal@aspora.com",
            "gmail_thread_id": "thread_inbound",
            "sender": "Counterparty <legal@example.com>",
            "subject": "Please review",
        }

        with patch.object(server_module.gmail_integration, "_gmail_service", return_value=same_account_service):
            same_result = server_module.gmail_integration.send_redline_email(base_matter, b"docx", "redline.docx")
        with patch.object(server_module.gmail_integration, "_gmail_service", return_value=different_account_service):
            different_result = server_module.gmail_integration.send_redline_email(base_matter, b"docx", "redline.docx")

        self.assertEqual(same_account_service.users_api.sent_body["threadId"], "thread_inbound")
        self.assertEqual(same_result["thread_id"], "thread_inbound")
        self.assertNotIn("threadId", different_account_service.users_api.sent_body)
        self.assertEqual(different_result["thread_id"], "new_thread")
        raw_message = different_account_service.users_api.sent_body["raw"]
        padding = "=" * ((4 - len(raw_message) % 4) % 4)
        decoded_message = base64.urlsafe_b64decode((raw_message + padding).encode("ascii"))
        self.assertIn(b"To: legal@example.com", decoded_message)
        self.assertIn(b'filename="redline.docx"', decoded_message)

    def test_gmail_send_redline_requires_confirmation_and_records_outbound_send(self):
        source_docx = make_docx([
            "This Agreement shall be governed by the laws of California.",
            "The Recipient must not circumvent the Company.",
        ])

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                create_status, create_payload = self.request(
                    "POST",
                    "/api/matters",
                    {
                        "filename": "Email NDA.docx",
                        "content_base64": base64.b64encode(source_docx).decode("ascii"),
                        "sender": "Legal Team <legal@example.com>",
                        "subject": "Review Email NDA",
                        "source_type": "gmail_inbound",
                        "gmail_thread_id": "thr_inbound",
                    },
                )
                matter_id = create_payload["matter"]["id"]
                with patch.object(server_module.gmail_integration, "send_redline_email", return_value={
                    "message_id": "msg_outbound",
                    "outbound_account": "outbound@example.com",
                    "sent_at": "2026-05-31T12:00:00+00:00",
                    "subject": "Re: Review Email NDA",
                    "thread_id": "thr_inbound",
                    "to": "legal@example.com",
                }) as send_redline_email:
                    unconfirmed_status, unconfirmed_payload = self.request(
                        "POST",
                        "/api/gmail/send-redline",
                        {"matter_id": matter_id},
                    )
                    confirmed_status, confirmed_payload = self.request(
                        "POST",
                        "/api/gmail/send-redline",
                        {"matter_id": matter_id, "confirm_send": True},
                    )
                    stored_matter = matter_store.get_matter(matter_id)

        self.assertEqual(create_status, 201)
        self.assertEqual(unconfirmed_status, 400)
        self.assertEqual(unconfirmed_payload["error"], "Confirm send is required before emailing a redline.")
        self.assertEqual(confirmed_status, 200)
        self.assertEqual(confirmed_payload["filename"], "Email-NDA-redlined.docx")
        self.assertEqual(confirmed_payload["matter"]["board_column"], "redline_ready")
        self.assertEqual(confirmed_payload["matter"]["last_outbound_to"], "legal@example.com")
        self.assertEqual(confirmed_payload["matter"]["last_outbound_account"], "outbound@example.com")
        self.assertEqual(confirmed_payload["matter"]["last_outbound_message_id"], "msg_outbound")
        self.assertEqual(stored_matter["board_column"], "redline_ready")
        self.assertEqual(stored_matter["last_outbound_filename"], "Email-NDA-redlined.docx")
        send_redline_email.assert_called_once()
        _matter, attachment_bytes, attachment_filename = send_redline_email.call_args.args
        self.assertEqual(attachment_filename, "Email-NDA-redlined.docx")
        self.assertGreater(len(attachment_bytes), 1000)

    def test_gmail_send_redline_applies_review_export_decisions(self):
        source_docx = make_docx([
            "This Agreement shall be governed by the laws of California.",
            "The Recipient must not circumvent the Company.",
        ])
        captured = {}

        def capture_redline_build(_source_bytes, review_result):
            captured["redline_count"] = len(review_result.get("redline_edits") or [])
            return source_docx

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                create_status, create_payload = self.request(
                    "POST",
                    "/api/matters",
                    {
                        "filename": "Decision NDA.docx",
                        "content_base64": base64.b64encode(source_docx).decode("ascii"),
                        "sender": "legal@example.com",
                    },
                )
                matter = create_payload["matter"]
                self.assertGreater(len(matter["review_result"].get("redline_edits") or []), 0)
                with patch.object(server_module.redline_export_service, "build_source_redline_docx", side_effect=capture_redline_build):
                    with patch.object(server_module.redline_export_service, "validate_docx_open_health", return_value=[]):
                        with patch.object(server_module.gmail_integration, "send_redline_email", return_value={
                            "message_id": "msg_outbound",
                            "outbound_account": "legal@aspora.com",
                            "sent_at": "2026-05-31T12:00:00+00:00",
                            "subject": "Re: Decision NDA",
                            "thread_id": "thread_outbound",
                            "to": "legal@example.com",
                        }):
                            send_status, send_payload = self.request(
                                "POST",
                                "/api/gmail/send-redline",
                                {
                                    "matter_id": matter["id"],
                                    "confirm_send": True,
                                    "export_redline_edits": [],
                                },
                            )

        self.assertEqual(create_status, 201)
        self.assertEqual(send_status, 200)
        self.assertEqual(send_payload["matter"]["board_column"], "redline_ready")
        self.assertEqual(captured["redline_count"], 0)

    def test_corrupt_matter_store_does_not_reset_repository(self):
        with tempfile.TemporaryDirectory() as data_dir:
            data_path = server_module.Path(data_dir)
            data_path.mkdir(parents=True, exist_ok=True)
            (data_path / "matters.json").write_text("{not valid json", encoding="utf-8")
            with self.matter_store_patches(data_dir)[0], self.matter_store_patches(data_dir)[1], self.matter_store_patches(data_dir)[2]:
                status, payload = self.request("GET", "/api/matters")

        self.assertEqual(status, 500)
        self.assertEqual(payload["error"], "Matter store is not valid JSON.")

    def test_text_review_reports_playbook_template_error(self):
        with patch.object(server_module, "review_nda", side_effect=PlaybookTemplateError("bad template")):
            status, payload = self.request("POST", "/api/review", {"text": "Reviewable NDA text."})

        self.assertEqual(status, 500)
        self.assertEqual(payload["error"], server_module.PLAYBOOK_TEMPLATE_ERROR_MESSAGE)

    def test_text_review_reports_real_malformed_playbook_template(self):
        with patch("nda_automation.checker.load_playbook", return_value=self.malformed_template_playbook()):
            status, payload = self.request(
                "POST",
                "/api/review",
                {"text": "The confidentiality obligations survive for seven (7) years."},
            )

        self.assertEqual(status, 500)
        self.assertEqual(payload["error"], server_module.PLAYBOOK_TEMPLATE_ERROR_MESSAGE)

    def test_text_review_returns_structured_redline_edits(self):
        status, payload = self.request(
            "POST",
            "/api/review",
            {"text": "This Agreement shall be governed by the laws of California."},
        )

        self.assertEqual(status, 200)
        governing_law_redline = next(edit for edit in payload["redline_edits"] if edit["clause_id"] == "governing_law")
        self.assertEqual(governing_law_redline["action"], "replace_paragraph")
        self.assertEqual(governing_law_redline["status"], "proposed")
        self.assertEqual(governing_law_redline["paragraph_id"], "p1")
        self.assertEqual(
            [option["label"] for option in governing_law_redline["template_options"]],
            ["India", "Delaware", "England and Wales", "DIFC"],
        )

    def test_text_review_returns_insert_redlines_for_missing_clauses(self):
        status, payload = self.request(
            "POST",
            "/api/review",
            {"text": "The parties will discuss a possible transaction."},
        )

        self.assertEqual(status, 200)
        redlines_by_clause = {edit["clause_id"]: edit for edit in payload["redline_edits"]}
        self.assertEqual(redlines_by_clause["governing_law"]["action"], "insert_after_paragraph")
        self.assertIn("England and Wales", redlines_by_clause["governing_law"]["insert_text"])
        self.assertEqual(redlines_by_clause["term_and_survival"]["action"], "insert_after_paragraph")
        self.assertIn("up to five years", redlines_by_clause["term_and_survival"]["insert_text"])
        self.assertEqual(redlines_by_clause["signatures"]["action"], "insert_after_paragraph")
        self.assertIn("For [Party 1 legal name]", redlines_by_clause["signatures"]["insert_text"])

    def test_text_review_returns_term_and_non_circumvention_redlines(self):
        status, payload = self.request(
            "POST",
            "/api/review",
            {
                "text": (
                    "The confidentiality obligations survive for seven years.\n\n"
                    "The Recipient must not circumvent the Company or deal directly with introduced parties."
                )
            },
        )

        self.assertEqual(status, 200)
        redlines_by_clause = {edit["clause_id"]: edit for edit in payload["redline_edits"]}
        self.assertEqual(redlines_by_clause["term_and_survival"]["action"], "replace_paragraph")
        self.assertIn("up to five years", redlines_by_clause["term_and_survival"]["replacement_text"])
        self.assertEqual(redlines_by_clause["non_circumvention"]["action"], "delete_paragraph")
        self.assertEqual(redlines_by_clause["non_circumvention"]["replacement_text"], "")

    def test_review_docx_export_returns_track_changes_enabled_docx(self):
        with tempfile.TemporaryDirectory() as exports_dir:
            with patch.object(export_service, "EXPORTS_DIR", server_module.Path(exports_dir)):
                status, payload, headers = self.request_with_headers(
                    "POST",
                    "/api/export-review-docx",
                    {"text": "This Agreement shall be governed by the laws of California.", "title": "California NDA"},
                )
                saved_payload = server_module.Path(headers["X-Export-Path"]).read_bytes()

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], DOCX_MIME)
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="nda-review-report.docx"')
        self.assertEqual(headers["X-Export-Verified"], "word-package; track-revisions")
        self.assertEqual(headers["X-Export-URL"], "/exports/nda-review-report.docx")
        self.assertTrue(headers["X-Export-Path"].endswith("nda-review-report.docx"))
        self.assertEqual(saved_payload, payload)
        with ZipFile(BytesIO(payload)) as archive:
            self.assertIsNone(archive.testzip())
            settings_xml = archive.read("word/settings.xml").decode("utf-8")
            document_xml = archive.read("word/document.xml").decode("utf-8")
        settings_root = ET.fromstring(settings_xml)
        document_root = ET.fromstring(document_xml)
        self.assertIsNotNone(settings_root.find(".//w:trackRevisions", W_NS))
        self.assertIn("California NDA", document_xml)
        self.assertGreaterEqual(len(document_root.findall(".//w:del", W_NS)), 1)
        self.assertGreaterEqual(len(document_root.findall(".//w:ins", W_NS)), 1)

    def test_export_dir_is_opt_in_for_saved_export_routes(self):
        if "NDA_EXPORTS_DIR" not in os.environ:
            self.assertIsNone(export_service.EXPORTS_DIR)
            self.assertIsNone(export_service.persist_export(b"data", "export.docx"))

            status, payload = self.request("GET", "/exports/export.docx")
            self.assertEqual(status, 404)
            self.assertEqual(payload["error"], "Not found")

    def test_review_docx_export_text_path_uses_reviewed_text(self):
        status, payload, _headers = self.request_with_headers(
            "POST",
            "/api/export-review-docx",
            {
                "reviewed_text": "This Agreement shall be governed by the laws of California.",
                "title": "Reviewed Text NDA",
            },
        )

        self.assertEqual(status, 200)
        with ZipFile(BytesIO(payload)) as archive:
            self.assertIsNone(archive.testzip())
            document_xml = archive.read("word/document.xml").decode("utf-8")
        self.assertIn("Reviewed Text NDA", document_xml)
        self.assertIn("This Agreement shall be governed by the laws of California.", document_xml)

    def test_review_docx_export_preserves_manual_viewer_redlines(self):
        manual_redlines = [
            {
                "id": "manual-p1",
                "action": "replace_paragraph",
                "paragraph_id": "p1",
                "paragraph_index": 1,
                "source_index": 1,
                "original_text": "NON-DISCLOSURE AGREEMENT (NDA)",
                "replacement_text": "Do you see problem?",
            }
        ]
        status, payload, _headers = self.request_with_headers(
            "POST",
            "/api/export-review-docx",
            {
                "text": "Do you see problem?",
                "reviewed_text": "Do you see problem?",
                "manual_redline_edits": manual_redlines,
            },
        )

        self.assertEqual(status, 200)
        assert_docx_redline_contract(self, payload, manual_redlines)
        with ZipFile(BytesIO(payload)) as archive:
            document_root = ET.fromstring(archive.read("word/document.xml"))
        revision_states = [
            (
                revision_text_for_state(paragraph, accepted=False),
                revision_text_for_state(paragraph, accepted=True),
            )
            for paragraph in document_root.findall(".//w:p", W_NS)
        ]
        self.assertIn(("NON-DISCLOSURE AGREEMENT (NDA)", "Do you see problem?"), revision_states)

    def test_manual_export_redline_rejects_blank_replace(self):
        redline = {
            "id": "blank-replace",
            "action": "replace_paragraph",
            "paragraph_id": "p1",
            "source_index": 1,
            "original_text": "The original paragraph.",
            "replacement_text": "",
        }

        self.assertIsNone(export_service.clean_manual_export_redline(redline))

    def test_manual_export_redline_cleaner_trims_direct_api_text_fields(self):
        redline = {
            "id": "manual-p1",
            "action": "replace_paragraph",
            "paragraph_id": " p1 ",
            "original_text": "  Old paragraph.  ",
            "replacement_text": "  New paragraph.  ",
            "anchor_text": "  Anchor paragraph.  ",
            "insert_text": "  Insert paragraph.  ",
        }

        manual = export_service.clean_manual_export_redline(redline)

        self.assertEqual(manual["paragraph_id"], "p1")
        self.assertEqual(manual["original_text"], "Old paragraph.")
        self.assertEqual(manual["replacement_text"], "New paragraph.")

    def test_selected_export_redlines_rederive_text_server_side(self):
        malicious_selected_redline = {
            "id": "r1",
            "action": "replace_paragraph",
            "paragraph_id": "p1",
            "source_index": 1,
            "original_text": "Client supplied fake original.",
            "replacement_text": "MALICIOUS CLIENT SUPPLIED REDLINE.",
            "template_options": [
                {"id": "governing_law_delaware", "selected": True, "text": "MALICIOUS TEMPLATE TEXT."}
            ],
        }

        status, payload, _headers = self.request_with_headers(
            "POST",
            "/api/export-review-docx",
            {
                "text": "This Agreement shall be governed by the laws of California.",
                "export_redline_edits": [malicious_selected_redline],
            },
        )

        self.assertEqual(status, 200)
        with ZipFile(BytesIO(payload)) as archive:
            document_xml = archive.read("word/document.xml").decode("utf-8")
        self.assertNotIn("MALICIOUS", document_xml)
        self.assertIn("Delaware", document_xml)
        self.assertIn("California", document_xml)

    def test_review_docx_export_download_does_not_require_saved_copy(self):
        with patch.object(export_service, "EXPORTS_DIR", None):
            status, payload, headers = self.request_with_headers(
                "POST",
                "/api/export-review-docx",
                {"text": "This Agreement shall be governed by the laws of California."},
            )

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], DOCX_MIME)
        self.assertEqual(headers["X-Export-Verified"], "word-package; track-revisions")
        self.assertNotIn("X-Export-Path", headers)
        self.assertNotIn("X-Export-URL", headers)
        with ZipFile(BytesIO(payload)) as archive:
            self.assertIsNone(archive.testzip())

    def test_review_docx_export_fails_before_download_when_docx_health_fails(self):
        with tempfile.TemporaryDirectory() as exports_dir:
            with (
                patch.object(export_service, "EXPORTS_DIR", server_module.Path(exports_dir)),
                patch.object(
                    server_module.redline_export_service,
                    "validate_docx_open_health",
                    return_value=["Missing DOCX parts: _rels/.rels."],
                ),
            ):
                status, payload, headers = self.request_with_headers(
                    "POST",
                    "/api/export-review-docx",
                    {"text": "This Agreement shall be governed by the laws of California."},
                )
                saved_files = list(server_module.Path(exports_dir).iterdir())

        self.assertEqual(status, 500)
        self.assertEqual(payload["error"], "The exported Word document failed its open-health check.")
        self.assertEqual(payload["details"], ["Missing DOCX parts: _rels/.rels."])
        self.assertNotEqual(headers.get("Content-Type"), DOCX_MIME)
        self.assertEqual(saved_files, [])

    def test_review_docx_export_rejects_text_that_differs_from_reviewed_text(self):
        status, payload = self.request(
            "POST",
            "/api/export-review-docx",
            {
                "text": "This Agreement shall be governed by the laws of England and Wales.",
                "reviewed_text": "This Agreement shall be governed by the laws of California.",
            },
        )

        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "Export text must match the latest reviewed text. Run Review NDA again.")

    def test_review_docx_export_reports_playbook_template_error(self):
        with patch.object(server_module.redline_export_service, "review_nda", side_effect=PlaybookTemplateError("bad template")):
            status, payload = self.request(
                "POST",
                "/api/export-review-docx",
                {"reviewed_text": "This Agreement shall be governed by the laws of California."},
            )

        self.assertEqual(status, 500)
        self.assertEqual(payload["error"], server_module.PLAYBOOK_TEMPLATE_ERROR_MESSAGE)

    def test_review_docx_export_reports_real_malformed_playbook_template(self):
        with patch("nda_automation.checker.load_playbook", return_value=self.malformed_template_playbook()):
            status, payload = self.request(
                "POST",
                "/api/export-review-docx",
                {"reviewed_text": "The confidentiality obligations survive for seven (7) years."},
            )

        self.assertEqual(status, 500)
        self.assertEqual(payload["error"], server_module.PLAYBOOK_TEMPLATE_ERROR_MESSAGE)

    def test_review_docx_export_preserves_word_source_index(self):
        source_docx = make_docx([
            "Intro paragraph.",
            "This Agreement shall be governed by the laws of California.",
        ])
        status, payload, headers = self.request_with_headers(
            "POST",
            "/api/export-review-docx",
            {
                "text": "Stale browser text should not drive DOCX export.",
                "title": "Uploaded NDA",
                "filename": "uploaded.docx",
                "content_base64": base64.b64encode(source_docx).decode("ascii"),
            },
        )

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="uploaded-redlined.docx"')
        assert_source_export_has_no_report_leakage(
            self,
            payload,
            extra_forbidden=["Stale browser text should not drive DOCX export."],
        )
        with ZipFile(BytesIO(payload)) as archive:
            self.assertIsNone(archive.testzip())
            document_xml = archive.read("word/document.xml").decode("utf-8")
        document_root = ET.fromstring(document_xml)
        deletion_text = [
            "".join(node.text or "" for node in deletion.findall(".//w:delText", W_NS))
            for deletion in document_root.findall(".//w:del", W_NS)
        ]
        self.assertTrue(any("California" in text for text in deletion_text))
        self.assertFalse(any("This Agreement shall be governed by the laws of California." in text for text in deletion_text))

    def test_review_docx_export_uses_uploaded_docx_over_stale_reviewed_text(self):
        source_docx = make_docx([
            "This Agreement shall be governed by the laws of California.",
        ])
        status, payload, _headers = self.request_with_headers(
            "POST",
            "/api/export-review-docx",
            {
                "reviewed_text": "Stale reviewed text should not drive DOCX export.",
                "filename": "uploaded.docx",
                "content_base64": base64.b64encode(source_docx).decode("ascii"),
            },
        )

        self.assertEqual(status, 200)
        assert_source_export_has_no_report_leakage(
            self,
            payload,
            extra_forbidden=["Stale reviewed text should not drive DOCX export."],
        )
        with ZipFile(BytesIO(payload)) as archive:
            self.assertIsNone(archive.testzip())
            document_xml = archive.read("word/document.xml").decode("utf-8")
        self.assertIn("California", document_xml)

    def test_uploaded_docx_export_preserves_manual_viewer_redlines_on_source(self):
        source_docx = make_docx([
            "NON-DISCLOSURE AGREEMENT (NDA)",
            "This reciprocal confidentiality agreement is dated 2025.",
        ])
        manual_redlines = [
            {
                "id": "manual-p1",
                "action": "replace_paragraph",
                "paragraph_id": "p1",
                "paragraph_index": 1,
                "source_index": 1,
                "original_text": "NON-DISCLOSURE AGREEMENT (NDA)",
                "replacement_text": "Do you see problem?",
            },
            {
                "id": "manual-p2",
                "action": "replace_paragraph",
                "paragraph_id": "p2",
                "paragraph_index": 2,
                "source_index": 2,
                "original_text": "This reciprocal confidentiality agreement is dated 2025.",
                "replacement_text": "Hello",
            },
        ]
        status, payload, headers = self.request_with_headers(
            "POST",
            "/api/export-review-docx",
            {
                "reviewed_text": "Do you see problem?\n\nHello",
                "filename": "Orbii - NDA DIFC.docx",
                "content_base64": base64.b64encode(source_docx).decode("ascii"),
                "manual_redline_edits": manual_redlines,
            },
        )

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="Orbii---NDA-DIFC-redlined.docx"')
        assert_source_export_has_no_report_leakage(self, payload)
        assert_docx_redline_contract(self, payload, manual_redlines)
        with ZipFile(BytesIO(payload)) as archive:
            document_root = ET.fromstring(archive.read("word/document.xml"))
        revision_states = [
            (
                revision_text_for_state(paragraph, accepted=False),
                revision_text_for_state(paragraph, accepted=True),
            )
            for paragraph in document_root.findall(".//w:p", W_NS)
        ]
        self.assertIn(("NON-DISCLOSURE AGREEMENT (NDA)", "Do you see problem?"), revision_states)
        self.assertIn(("This reciprocal confidentiality agreement is dated 2025.", "Hello"), revision_states)

    def test_docx_review_then_export_round_trip_uses_uploaded_source_revisions(self):
        source_docx = make_docx([
            "Intro paragraph.",
            "This Agreement shall be governed by the laws of California.",
            "The Recipient must not circumvent the Company.",
        ])
        content_base64 = base64.b64encode(source_docx).decode("ascii")

        review_status, review_payload = self.request(
            "POST",
            "/api/review-document",
            {
                "filename": "round-trip.docx",
                "content_base64": content_base64,
            },
        )
        export_status, export_payload, export_headers = self.request_with_headers(
            "POST",
            "/api/export-review-docx",
            {
                "reviewed_text": "Stale pasted browser text should not appear in the export.",
                "filename": "round-trip.docx",
                "content_base64": content_base64,
            },
        )

        self.assertEqual(review_status, 200)
        self.assertEqual(review_payload["source"]["type"], "docx")
        self.assertEqual(review_payload["paragraphs"][1]["source_index"], 2)
        governing_law_redline = next(edit for edit in review_payload["redline_edits"] if edit["clause_id"] == "governing_law")
        non_circumvention_redline = next(edit for edit in review_payload["redline_edits"] if edit["clause_id"] == "non_circumvention")
        self.assertEqual(governing_law_redline["source_index"], 2)
        self.assertEqual(non_circumvention_redline["source_index"], 3)

        self.assertEqual(export_status, 200)
        self.assertEqual(export_headers["Content-Disposition"], 'attachment; filename="round-trip-redlined.docx"')
        assert_source_export_has_no_report_leakage(
            self,
            export_payload,
            extra_forbidden=["Stale pasted browser text should not appear in the export."],
        )
        assert_docx_redline_contract(self, export_payload, [governing_law_redline, non_circumvention_redline])
        with ZipFile(BytesIO(export_payload)) as archive:
            self.assertIsNone(archive.testzip())
            document_xml = archive.read("word/document.xml").decode("utf-8")
        document_root = ET.fromstring(document_xml)
        revision_states = [
            (
                revision_text_for_state(paragraph, accepted=False),
                revision_text_for_state(paragraph, accepted=True),
            )
            for paragraph in document_root.findall(".//w:p", W_NS)
        ]
        self.assertIn("Intro paragraph.", document_xml)
        self.assertIn(
            (
                "This Agreement shall be governed by the laws of California.",
                "This Agreement shall be governed by the laws of England and Wales.",
            ),
            revision_states,
        )
        self.assertIn(("The Recipient must not circumvent the Company.", ""), revision_states)

    def test_saved_export_route_returns_exact_docx_bytes(self):
        with tempfile.TemporaryDirectory() as exports_dir:
            with patch.object(export_service, "EXPORTS_DIR", server_module.Path(exports_dir)):
                status, payload, headers = self.request_with_headers(
                    "POST",
                    "/api/export-review-docx",
                    {"text": "This Agreement shall be governed by the laws of California."},
                )
                self.assert_saved_export_url_matches_response(headers, payload)

        self.assertEqual(status, 200)

    def test_saved_uploaded_docx_export_route_returns_exact_docx_bytes(self):
        source_docx = make_docx([
            "This Agreement shall be governed by the laws of California.",
        ])
        with tempfile.TemporaryDirectory() as exports_dir:
            with patch.object(export_service, "EXPORTS_DIR", server_module.Path(exports_dir)):
                status, payload, headers = self.request_with_headers(
                    "POST",
                    "/api/export-review-docx",
                    {
                        "filename": "uploaded.docx",
                        "content_base64": base64.b64encode(source_docx).decode("ascii"),
                    },
                )
                self.assert_saved_export_url_matches_response(headers, payload)

        self.assertEqual(status, 200)
        self.assertEqual(headers["X-Export-URL"], "/exports/uploaded-redlined.docx")
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="uploaded-redlined.docx"')
        assert_source_export_has_no_report_leakage(self, payload)

    def test_persisted_exports_prune_old_saved_docx_files(self):
        with tempfile.TemporaryDirectory() as exports_dir:
            exports_path = server_module.Path(exports_dir)
            with patch.object(export_service, "EXPORTS_DIR", exports_path):
                with patch.object(export_service, "MAX_SAVED_EXPORTS", 2):
                    old_one = exports_path / "old-one.docx"
                    old_two = exports_path / "old-two.docx"
                    old_one.write_bytes(b"old-one")
                    old_two.write_bytes(b"old-two")
                    os.utime(old_one, (1, 1))
                    os.utime(old_two, (2, 2))

                    saved_path = export_service.persist_export(b"new", "new.docx")

                    self.assertEqual(saved_path.resolve(), (exports_path / "new.docx").resolve())
                    self.assertEqual(
                        sorted(path.name for path in exports_path.glob("*.docx")),
                        ["new.docx", "old-two.docx"],
                    )
                    self.assertEqual((exports_path / "new.docx").read_bytes(), b"new")

    def test_text_review_reports_malformed_playbook_search_terms(self):
        playbook = deepcopy(load_playbook())
        mutuality = next(clause for clause in playbook["clauses"] if clause["id"] == "mutuality")
        mutuality["search_terms"] = []

        with patch("nda_automation.checker.load_playbook", return_value=playbook):
            status, payload = self.request("POST", "/api/review", {"text": "Reviewable NDA text."})

        self.assertEqual(status, 500)
        self.assertEqual(payload["error"], server_module.PLAYBOOK_TEMPLATE_ERROR_MESSAGE)

    def test_review_docx_export_rejects_empty_text(self):
        status, payload = self.request("POST", "/api/export-review-docx", {"text": " "})

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "Provide NDA text to export.")

    def test_document_review_rejects_bad_json(self):
        status, payload = self.request(
            "POST",
            "/api/review-document",
            "{not json",
            {"Content-Type": "application/json"},
        )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "Request body must be valid JSON.")

    def test_document_review_rejects_non_docx(self):
        status, payload = self.request(
            "POST",
            "/api/review-document",
            {
                "filename": "nda.txt",
                "content_base64": base64.b64encode(b"not a word document").decode("ascii"),
            },
        )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "Upload a .docx Word document or text-based PDF.")

    def test_document_review_rejects_oversize_upload(self):
        with patch.object(document_limits, "MAX_DOCUMENT_BYTES", 4):
            status, payload = self.request(
                "POST",
                "/api/review-document",
                {
                    "filename": "nda.docx",
                    "content_base64": base64.b64encode(b"too large").decode("ascii"),
                },
            )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "The document is larger than the 10 MB upload limit.")

    def test_document_review_rejects_docx_decompression_bomb(self):
        source_docx = make_compressed_docx("A" * 4096)

        with patch.object(docx_text, "MAX_DOCX_ENTRY_COMPRESSION_RATIO", 2):
            status, payload = self.request(
                "POST",
                "/api/review-document",
                {
                    "filename": "bomb.docx",
                    "content_base64": base64.b64encode(source_docx).decode("ascii"),
                },
            )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "The Word document uses a suspicious compression ratio.")

    def test_matter_upload_rejects_oversize_upload_at_ingestion_boundary(self):
        with patch.object(document_limits, "MAX_DOCUMENT_BYTES", 4):
            status, payload = self.request(
                "POST",
                "/api/matters",
                {
                    "filename": "nda.docx",
                    "content_base64": base64.b64encode(b"too large").decode("ascii"),
                },
            )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "The document is larger than the 10 MB upload limit.")

    def test_document_review_reports_paragraph_alignment_failure(self):
        with patch.object(server_module, "extract_document", return_value=("docx", [{"source_index": 1, "text": "Paragraph"}], None)):
            with patch.object(server_module, "review_nda", side_effect=ParagraphAlignmentError("alignment failed")):
                status, payload = self.request(
                    "POST",
                    "/api/review-document",
                    {
                        "filename": "nda.docx",
                        "content_base64": base64.b64encode(b"word bytes").decode("ascii"),
                    },
                )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "The extracted document paragraphs could not be aligned to the extracted text.")

    def test_document_review_reports_playbook_template_error(self):
        with patch.object(server_module, "extract_document", return_value=("docx", [{"source_index": 1, "text": "Paragraph"}], None)):
            with patch.object(server_module, "review_nda", side_effect=PlaybookTemplateError("bad template")):
                status, payload = self.request(
                    "POST",
                    "/api/review-document",
                    {
                        "filename": "nda.docx",
                        "content_base64": base64.b64encode(b"word bytes").decode("ascii"),
                    },
                )

        self.assertEqual(status, 500)
        self.assertEqual(payload["error"], server_module.PLAYBOOK_TEMPLATE_ERROR_MESSAGE)

    def test_document_review_reports_real_malformed_playbook_template(self):
        extracted_paragraphs = [
            {"source_index": 1, "text": "The confidentiality obligations survive for seven (7) years."}
        ]
        with patch.object(server_module, "extract_document", return_value=("docx", extracted_paragraphs, None)):
            with patch("nda_automation.checker.load_playbook", return_value=self.malformed_template_playbook()):
                status, payload = self.request(
                    "POST",
                    "/api/review-document",
                    {
                        "filename": "nda.docx",
                        "content_base64": base64.b64encode(b"word bytes").decode("ascii"),
                    },
                )

        self.assertEqual(status, 500)
        self.assertEqual(payload["error"], server_module.PLAYBOOK_TEMPLATE_ERROR_MESSAGE)

    def test_static_route_blocks_directory_traversal(self):
        status, payload = self.request("GET", "/static/../README.md")

        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "Not found")

    def test_static_files_use_etag_cache_validation(self):
        status, payload, headers = self.request_with_headers("GET", "/static/app.js")
        etag = headers.get("ETag")
        cached_status, cached_payload, cached_headers = self.request_with_headers(
            "GET",
            "/static/app.js",
            headers={"If-None-Match": etag},
        )

        self.assertEqual(status, 200)
        self.assertIsInstance(payload, bytes)
        self.assertTrue(etag)
        self.assertEqual(headers["Cache-Control"], "no-cache, max-age=0, must-revalidate")
        self.assertEqual(cached_status, 304)
        self.assertEqual(cached_payload, b"")
        self.assertEqual(cached_headers["ETag"], etag)

    def test_head_static_route_uses_app_cache_strategy(self):
        status, payload, headers = self.request_with_headers("HEAD", "/static/app.js")

        self.assertEqual(status, 200)
        self.assertEqual(payload, b"")
        self.assertTrue(headers.get("ETag"))
        self.assertEqual(headers["Cache-Control"], "no-cache, max-age=0, must-revalidate")


def make_docx(paragraphs):
    body = "".join(
        f"<w:p><w:r><w:t>{escape_xml(paragraph)}</w:t></w:r></w:p>"
        for paragraph in paragraphs
    )
    document_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>{body}</w:body>
</w:document>"""
    with BytesIO() as output:
        with ZipFile(output, "w") as archive:
            archive.writestr("word/document.xml", document_xml)
        return output.getvalue()


def make_compressed_docx(text):
    document_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>{escape_xml(text)}</w:t></w:r></w:p></w:body>
</w:document>"""
    with BytesIO() as output:
        with ZipFile(output, "w", ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", document_xml)
        return output.getvalue()


def make_pdf(text):
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    objects = [
        "1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
        "2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n",
        "3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >> endobj\n",
        "4 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n",
    ]
    stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET\n"
    objects.append(f"5 0 obj << /Length {len(stream.encode('latin-1'))} >> stream\n{stream}endstream endobj\n")
    with BytesIO() as output:
        output.write(b"%PDF-1.4\n")
        offsets = [0]
        for pdf_object in objects:
            offsets.append(output.tell())
            output.write(pdf_object.encode("latin-1"))
        xref_offset = output.tell()
        output.write(f"xref\n0 {len(objects) + 1}\n".encode("latin-1"))
        output.write(b"0000000000 65535 f \n")
        for offset in offsets[1:]:
            output.write(f"{offset:010d} 00000 n \n".encode("latin-1"))
        output.write(f"trailer << /Root 1 0 R /Size {len(objects) + 1} >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("latin-1"))
        return output.getvalue()


def escape_xml(value):
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def assert_source_export_has_no_report_leakage(testcase, docx_bytes, extra_forbidden=()):
    with ZipFile(BytesIO(docx_bytes)) as archive:
        testcase.assertIsNone(archive.testzip())
        document_xml = archive.read("word/document.xml").decode("utf-8")
    for phrase in [*SOURCE_EXPORT_REPORT_LEAKAGE_PHRASES, *extra_forbidden]:
        testcase.assertNotIn(phrase, document_xml)


def revision_text_for_state(node, accepted):
    tag = node.tag.rsplit("}", 1)[-1]
    if tag == "del":
        return "".join(item.text or "" for item in node.findall(".//w:delText", W_NS)) if not accepted else ""
    if tag == "ins":
        return "".join(item.text or "" for item in node.findall(".//w:t", W_NS)) if accepted else ""
    if tag == "t":
        return node.text or ""
    if tag == "br":
        return "\n"
    return "".join(revision_text_for_state(child, accepted) for child in list(node))


if __name__ == "__main__":
    unittest.main()
