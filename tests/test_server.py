import base64
import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from nda_automation import server as server_module
from nda_automation.server import NdaAutomationHandler


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
            return response.status, payload
        finally:
            connection.close()

    def test_text_review_rejects_bad_json(self):
        status, payload = self.request(
            "POST",
            "/api/review",
            "{not json",
            {"Content-Type": "application/json"},
        )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "Request body must be valid JSON.")

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

    def test_static_route_blocks_directory_traversal(self):
        status, payload = self.request("GET", "/static/../README.md")

        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "Not found")


if __name__ == "__main__":
    unittest.main()
