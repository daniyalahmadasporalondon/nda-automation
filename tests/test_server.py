import base64
import http.client
import json
import os
import tempfile
import threading
import unittest
from copy import deepcopy
from http.server import ThreadingHTTPServer
from io import BytesIO
from unittest.mock import patch
from zipfile import ZipFile
import xml.etree.ElementTree as ET

from nda_automation.checker import ParagraphAlignmentError, PlaybookTemplateError, load_playbook
from nda_automation.docx_export import DOCX_MIME
from nda_automation import server as server_module
from nda_automation.server import NdaAutomationHandler

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

    def assert_saved_export_url_matches_response(self, headers, payload):
        self.assertIn("X-Export-URL", headers)
        route_status, route_payload, route_headers = self.request_with_headers("GET", headers["X-Export-URL"])

        self.assertEqual(route_status, 200)
        self.assertEqual(route_headers["Content-Type"], DOCX_MIME)
        self.assertEqual(route_headers["Content-Disposition"], headers["Content-Disposition"])
        self.assertEqual(route_payload, payload)
        self.assertEqual(server_module.Path(headers["X-Export-Path"]).read_bytes(), payload)

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
            with patch.object(server_module, "EXPORTS_DIR", server_module.Path(exports_dir)):
                status, payload, headers = self.request_with_headers(
                    "POST",
                    "/api/export-review-docx",
                    {"text": "This Agreement shall be governed by the laws of California.", "title": "California NDA"},
                )
                saved_payload = server_module.Path(headers["X-Export-Path"]).read_bytes()

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], DOCX_MIME)
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="nda-review-report.docx"')
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
            self.assertIsNone(server_module.EXPORTS_DIR)
            self.assertIsNone(server_module._persist_export(b"data", "export.docx"))

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

    def test_review_docx_export_download_does_not_require_saved_copy(self):
        with patch.object(server_module, "EXPORTS_DIR", None):
            status, payload, headers = self.request_with_headers(
                "POST",
                "/api/export-review-docx",
                {"text": "This Agreement shall be governed by the laws of California."},
            )

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], DOCX_MIME)
        self.assertNotIn("X-Export-Path", headers)
        self.assertNotIn("X-Export-URL", headers)
        with ZipFile(BytesIO(payload)) as archive:
            self.assertIsNone(archive.testzip())

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
        with patch.object(server_module, "review_nda", side_effect=PlaybookTemplateError("bad template")):
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
            with patch.object(server_module, "EXPORTS_DIR", server_module.Path(exports_dir)):
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
            with patch.object(server_module, "EXPORTS_DIR", server_module.Path(exports_dir)):
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
            with patch.object(server_module, "EXPORTS_DIR", exports_path):
                with patch.object(server_module, "MAX_SAVED_EXPORTS", 2):
                    old_one = exports_path / "old-one.docx"
                    old_two = exports_path / "old-two.docx"
                    old_one.write_bytes(b"old-one")
                    old_two.write_bytes(b"old-two")
                    os.utime(old_one, (1, 1))
                    os.utime(old_two, (2, 2))

                    saved_path = server_module._persist_export(b"new", "new.docx")

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
        self.assertEqual(payload["error"], "Upload a .docx Word document.")

    def test_document_review_rejects_oversize_upload(self):
        with patch.object(server_module, "MAX_DOCUMENT_BYTES", 4):
            status, payload = self.request(
                "POST",
                "/api/review-document",
                {
                    "filename": "nda.docx",
                    "content_base64": base64.b64encode(b"too large").decode("ascii"),
                },
            )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "The Word document is larger than the 10 MB upload limit.")

    def test_document_review_reports_paragraph_alignment_failure(self):
        with patch.object(server_module, "extract_docx_paragraphs", return_value=[{"source_index": 1, "text": "Paragraph"}]):
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
        with patch.object(server_module, "extract_docx_paragraphs", return_value=[{"source_index": 1, "text": "Paragraph"}]):
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
        with patch.object(server_module, "extract_docx_paragraphs", return_value=extracted_paragraphs):
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
