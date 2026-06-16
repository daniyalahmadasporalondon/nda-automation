"""Observability + deterministic-foreground tests for POST /api/generate-nda.

Two guarantees, end-to-end over the real HTTP route:

1. Every generate emits ``generation_phase`` structured log records, each
   stamped with a per-request id and carrying the boundary phases (``generate
   started``, ``waited for slot``, ``response sent``). The ``waited for slot``
   phase is the headline: tonight's lost-queue time is now visible.
2. Generate stays deterministic — it ships WITHOUT touching any OpenRouter / AI
   network path — while the deterministic, network-free self-check / ship-gate
   still runs (the millisecond compliance safety net is untouched).
"""

from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from nda_automation import generation_timing
from nda_automation import matter_store
from nda_automation import nda_generation
from nda_automation import server as server_module
from nda_automation import telemetry
from nda_automation.review_engine import ACTIVE_REVIEW_ENGINE_ENV
from nda_automation.server import NdaAutomationHandler


class QuietHandler(NdaAutomationHandler):
    def log_message(self, *args, **kwargs):
        return


class _PhaseLogCapture:
    """Capture the JSON ``generation_phase`` records the stopwatch emits.

    Patches the stopwatch's emit print so the records are collected in-process
    (the threaded test server runs in this process), regardless of stdout.
    """

    def __init__(self):
        self.records: list[dict] = []

    def __enter__(self):
        self._patch = patch.object(generation_timing, "print", self._capture, create=True)
        self._patch.__enter__()
        return self

    def __exit__(self, *exc):
        return self._patch.__exit__(*exc)

    def _capture(self, line, *args, **kwargs):  # mirrors builtins.print signature loosely
        try:
            record = json.loads(line)
        except (TypeError, ValueError):
            return
        if isinstance(record, dict) and record.get("event") == "generation_phase":
            self.records.append(record)


class GenerateObservabilityRouteTests(unittest.TestCase):
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

    def matter_store_patches(self, data_dir):
        data_path = server_module.Path(data_dir)
        return (
            patch.object(matter_store, "DATA_DIR", data_path),
            patch.object(matter_store, "MATTERS_PATH", data_path / "matters.json"),
            patch.object(matter_store, "UPLOADS_DIR", data_path / "uploads"),
        )

    def env(self):
        # Deterministic config matching prod: AI generation kill-switch OFF, the
        # deterministic review engine selected. The self-check / ship-gate still run.
        return {
            ACTIVE_REVIEW_ENGINE_ENV: "deterministic",
            "NDA_GENERATION_AI_ENABLED": "false",
        }

    def generate(self, body):
        return self.request("POST", "/api/generate-nda", body)

    def _valid_body(self):
        return {
            "signing_entity_id": "aspora_technology",
            "intake": {
                "counterparty_name": "Observability Holdings Limited",
                "project": "evaluating a potential commercial relationship",
                "term_years": 2,
                "nda_type": "mutual",
            },
        }

    # --- (a) phase-timing records ---------------------------------------- #
    def test_emits_phase_records_with_request_id_and_expected_phase_names(self):
        with tempfile.TemporaryDirectory() as data_dir:
            p = self.matter_store_patches(data_dir)
            with p[0], p[1], p[2], patch.dict(os.environ, self.env()), _PhaseLogCapture() as cap:
                status, payload, _ = self.generate(self._valid_body())
                self.assertEqual(status, 201, payload)

        self.assertTrue(cap.records, "no generation_phase records were emitted")

        # Every record carries the SAME short request id and a numeric elapsed_ms.
        request_ids = {r["request_id"] for r in cap.records}
        self.assertEqual(len(request_ids), 1, request_ids)
        request_id = next(iter(request_ids))
        self.assertTrue(request_id)
        self.assertLessEqual(len(request_id), 12)
        for record in cap.records:
            self.assertEqual(record["event"], "generation_phase")
            self.assertIsInstance(record["elapsed_ms"], (int, float))
            self.assertGreaterEqual(record["elapsed_ms"], 0.0)
            self.assertIn("phase", record)

        # The boundary phases the route owns are all present. "waited for slot"
        # is the headline metric (the lost-queue time made visible).
        phases = {r["phase"] for r in cap.records}
        for required in ("generate started", "waited for slot", "response sent"):
            self.assertIn(required, phases, phases)

        # "response sent" is the cumulative total (since-start), the others are
        # per-phase deltas.
        response_sent = next(r for r in cap.records if r["phase"] == "response sent")
        self.assertTrue(response_sent["cumulative"])

    def test_workflow_phase_marks_are_recorded_on_the_bound_stopwatch(self):
        # The deeper workflow phases reach the same per-request timeline via the
        # bound-stopwatch seam (generation_timing.mark_phase). We assert the seam
        # works: a mark_phase made while a generate is in flight lands on that
        # request's stopwatch (same request id). We hook it by wrapping the real
        # workflow call and emitting a workflow-owned phase from inside it.
        from nda_automation import nda_generation_workflow

        real = nda_generation_workflow.generate_nda_from_payload

        def instrumented(payload, **kwargs):
            # Simulates what the integrator wires into the workflow body.
            generation_timing.mark_phase("playbook loaded")
            result = real(payload, **kwargs)
            generation_timing.mark_phase("matter persisted")
            return result

        with tempfile.TemporaryDirectory() as data_dir:
            p = self.matter_store_patches(data_dir)
            with p[0], p[1], p[2], patch.dict(os.environ, self.env()), _PhaseLogCapture() as cap, patch.object(
                nda_generation_workflow, "generate_nda_from_payload", side_effect=instrumented
            ):
                status, payload, _ = self.generate(self._valid_body())
                self.assertEqual(status, 201, payload)

        phases = {r["phase"] for r in cap.records}
        self.assertIn("playbook loaded", phases)
        self.assertIn("matter persisted", phases)
        # All on ONE request timeline (no cross-recording).
        self.assertEqual(len({r["request_id"] for r in cap.records}), 1)

    def test_mark_phase_is_a_no_op_with_no_bound_stopwatch(self):
        # Outside a generate, mark_phase must never raise and must emit nothing.
        with _PhaseLogCapture() as cap:
            generation_timing.mark_phase("playbook loaded")
        self.assertEqual(cap.records, [])

    # --- (b) deterministic: no AI / OpenRouter on the request path -------- #
    def test_generate_completes_without_any_openrouter_ai_call(self):
        # Belt-and-suspenders: make ANY OpenRouter HTTP attempt fail loudly, then
        # prove a full generate still ships 201. If the request path touched the AI
        # network this would surface as a 500.
        import urllib.request

        def explode(*args, **kwargs):
            raise AssertionError("generate must not make any OpenRouter/AI network call")

        with tempfile.TemporaryDirectory() as data_dir:
            p = self.matter_store_patches(data_dir)
            with p[0], p[1], p[2], patch.dict(os.environ, self.env()), patch.object(
                urllib.request, "urlopen", side_effect=explode
            ):
                status, payload, _ = self.generate(self._valid_body())

        self.assertEqual(status, 201, payload)
        self.assertEqual(payload["status"], "generated")

    def test_deterministic_self_check_ship_gate_still_runs(self):
        # The deterministic, network-free self-check / ship-gate is the millisecond
        # compliance safety net and must STAY. Prove both run on the request path:
        # (1) a clean draft reports a passing self_check; (2) the ship gate
        # (assert_generated_nda_is_on_position) is invoked on the route's path.
        with tempfile.TemporaryDirectory() as data_dir:
            p = self.matter_store_patches(data_dir)
            with p[0], p[1], p[2], patch.dict(os.environ, self.env()), patch.object(
                nda_generation,
                "assert_generated_nda_is_on_position",
                wraps=nda_generation.assert_generated_nda_is_on_position,
            ) as gate:
                status, payload, _ = self.generate(self._valid_body())

        self.assertEqual(status, 201, payload)
        # Self-check ran and reported a verdict (the millisecond compliance net).
        self.assertIn("self_check", payload)
        self.assertTrue(payload["self_check"]["passed"], payload["self_check"])
        self.assertEqual(payload["self_check"]["native_failures"], [])
        # The deterministic ship gate fired on the request path.
        self.assertTrue(gate.called)


if __name__ == "__main__":
    unittest.main()
