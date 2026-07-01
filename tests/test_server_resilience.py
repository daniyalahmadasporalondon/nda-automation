"""Tests for the server.py HTTP resilience/observability hardening.

Covers three additive, server.py-local fixes:
1. Socket read timeout on NdaAutomationHandler (slow-loris / half-open guard).
2. Split liveness/readiness probes:
   - /healthz is PURE LIVENESS: always 200 {"status":"ok", ...} while the
     process is alive. It carries the in-memory load fields for info, but it
     NEVER returns 503 on load -- so Render's deploy gate (healthCheckPath:
     /healthz) is never tripped by transient load.
   - /readyz is the in-memory-only load/readiness probe that returns 503
     {"status":"degraded", ...} under load and fail-opens to a static ok
     payload on any probe error.
3. Per-request structured latency logging + a slow-request WARN line, with the
   matter/uuid path segment templated to <id> for aggregation.
"""

from __future__ import annotations

import http.client
import importlib
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from io import StringIO
from unittest.mock import patch

from nda_automation import (
    generation_priority,
    ingestion_service,
    process_memory,
    telemetry,
)
from nda_automation import server as server_module
from nda_automation.server import NdaAutomationHandler


class QuietNdaAutomationHandler(NdaAutomationHandler):
    def log_message(self, format, *args):
        return


class HandlerTimeoutTests(unittest.TestCase):
    def test_timeout_is_positive_int(self):
        self.assertIsInstance(NdaAutomationHandler.timeout, int)
        self.assertGreater(NdaAutomationHandler.timeout, 0)

    def test_timeout_honors_env_override(self):
        with patch.dict("os.environ", {"NDA_HTTP_REQUEST_TIMEOUT_SECONDS": "45"}):
            reloaded = importlib.reload(server_module)
            try:
                self.assertEqual(reloaded.NdaAutomationHandler.timeout, 45)
                self.assertEqual(reloaded._http_request_timeout_seconds(), 45)
            finally:
                # Restore the module to its default-env state for other tests.
                importlib.reload(server_module)

    def test_timeout_fails_open_on_bad_env(self):
        with patch.dict("os.environ", {"NDA_HTTP_REQUEST_TIMEOUT_SECONDS": "not-an-int"}):
            reloaded = importlib.reload(server_module)
            try:
                self.assertEqual(reloaded._http_request_timeout_seconds(), 120)
                self.assertEqual(reloaded.NdaAutomationHandler.timeout, 120)
            finally:
                importlib.reload(server_module)


class TemplatizePathTests(unittest.TestCase):
    def test_matter_id_segment_collapses_to_id(self):
        templated = server_module._templatize_request_path(
            "/api/matters/matter_abcdef012345/review"
        )
        self.assertEqual(templated, "/api/matters/<id>/review")

    def test_uuid_segment_collapses_to_id(self):
        templated = server_module._templatize_request_path(
            "/api/matters/12345678-1234-1234-1234-1234567890ab/source"
        )
        self.assertEqual(templated, "/api/matters/<id>/source")

    def test_static_path_unchanged(self):
        self.assertEqual(
            server_module._templatize_request_path("/static/app.js"),
            "/static/app.js",
        )


class HealthzReadyzAndLatencyTests(unittest.TestCase):
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

    def setUp(self):
        telemetry.reset()

    def request(self, method, path):
        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            connection.request(method, path)
            response = connection.getresponse()
            raw_body = response.read()
            payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
            return response.status, payload
        finally:
            connection.close()

    # -- /healthz is PURE LIVENESS (never 503 on load) --------------------- #

    def test_healthz_ok_under_low_load(self):
        with patch.object(
            ingestion_service._INBOUND_REVIEW_POOL, "queue_depth", return_value=0
        ), patch.object(
            process_memory, "memory_usage", return_value={"used_fraction": 0.1}
        ), patch.object(
            generation_priority, "active_generation_count", return_value=0
        ):
            status, payload = self.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["queue_depth"], 0)
        self.assertEqual(payload["used_fraction"], 0.1)
        self.assertEqual(payload["generation_in_flight"], 0)

    def test_healthz_stays_200_when_queue_is_deep(self):
        # Liveness must not depend on load: a deep queue would make /readyz go
        # 503, but /healthz stays 200 "ok" so Render's deploy gate is never
        # tripped by transient load. Load fields are still reported for info.
        deep = server_module.READYZ_QUEUE_DEGRADED_DEPTH + 5
        with patch.object(
            ingestion_service._INBOUND_REVIEW_POOL, "queue_depth", return_value=deep
        ), patch.object(
            process_memory, "memory_usage", return_value={"used_fraction": 0.1}
        ), patch.object(
            generation_priority, "active_generation_count", return_value=3
        ):
            status, payload = self.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["queue_depth"], deep)
        self.assertEqual(payload["generation_in_flight"], 3)

    def test_healthz_stays_200_under_memory_pressure(self):
        # High memory would make /readyz go 503, but /healthz stays 200 "ok".
        with patch.object(
            ingestion_service._INBOUND_REVIEW_POOL, "queue_depth", return_value=0
        ), patch.object(
            process_memory, "memory_usage", return_value={"used_fraction": 0.99}
        ), patch.object(
            generation_priority, "active_generation_count", return_value=0
        ):
            status, payload = self.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["used_fraction"], 0.99)

    def test_healthz_fails_open_when_probe_raises(self):
        def _boom():
            raise RuntimeError("probe exploded")

        with patch.object(
            ingestion_service._INBOUND_REVIEW_POOL, "queue_depth", side_effect=_boom
        ):
            status, payload = self.request("GET", "/healthz")
        # A probe bug must never take liveness down: static ok, 200.
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")

    # -- /readyz carries the load/readiness signal (503 under load) -------- #

    def test_readyz_ok_under_low_load(self):
        with patch.object(
            ingestion_service._INBOUND_REVIEW_POOL, "queue_depth", return_value=0
        ), patch.object(
            process_memory, "memory_usage", return_value={"used_fraction": 0.1}
        ), patch.object(
            generation_priority, "active_generation_count", return_value=0
        ):
            status, payload = self.request("GET", "/readyz")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["queue_depth"], 0)
        self.assertEqual(payload["used_fraction"], 0.1)
        self.assertEqual(payload["generation_in_flight"], 0)

    def test_readyz_degraded_on_deep_queue(self):
        deep = server_module.READYZ_QUEUE_DEGRADED_DEPTH + 5
        with patch.object(
            ingestion_service._INBOUND_REVIEW_POOL, "queue_depth", return_value=deep
        ), patch.object(
            process_memory, "memory_usage", return_value={"used_fraction": 0.1}
        ), patch.object(
            generation_priority, "active_generation_count", return_value=3
        ):
            status, payload = self.request("GET", "/readyz")
        self.assertEqual(status, 503)
        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(payload["queue_depth"], deep)
        self.assertEqual(payload["generation_in_flight"], 3)

    def test_readyz_degraded_on_memory_pressure(self):
        with patch.object(
            ingestion_service._INBOUND_REVIEW_POOL, "queue_depth", return_value=0
        ), patch.object(
            process_memory, "memory_usage", return_value={"used_fraction": 0.9}
        ), patch.object(
            generation_priority, "active_generation_count", return_value=0
        ):
            status, payload = self.request("GET", "/readyz")
        self.assertEqual(status, 503)
        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(payload["used_fraction"], 0.9)

    def test_readyz_fails_open_when_probe_raises(self):
        def _boom():
            raise RuntimeError("probe exploded")

        with patch.object(
            ingestion_service._INBOUND_REVIEW_POOL, "queue_depth", side_effect=_boom
        ):
            status, payload = self.request("GET", "/readyz")
        # A probe bug must never take readyz down: static ok, 200.
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")

    # -- per-request latency logging --------------------------------------- #

    def test_request_emits_templated_latency_line(self):
        real_id = "matter_abcdef012345"
        captured = StringIO()
        with patch("sys.stdout", captured):
            # A real matter id path (404 is fine; we assert on the log line).
            self.request("GET", f"/api/matters/{real_id}/review")
        lines = [line for line in captured.getvalue().splitlines() if line.strip()]
        events = [json.loads(line) for line in lines]
        http_events = [e for e in events if e.get("event") == "http_request"]
        self.assertTrue(http_events, f"no http_request line in {events!r}")
        event = http_events[-1]
        self.assertEqual(event["method"], "GET")
        self.assertEqual(event["path"], "/api/matters/<id>/review")
        self.assertIn("elapsed_ms", event)
        self.assertIsInstance(event["elapsed_ms"], (int, float))

    def test_slow_request_emits_warn_line(self):
        # Drive perf_counter so the measured elapsed exceeds the slow threshold.
        clock = iter([0.0, (server_module.SLOW_REQUEST_WARN_MS / 1000.0) + 1.0])

        def _fake_perf_counter():
            try:
                return next(clock)
            except StopIteration:
                return (server_module.SLOW_REQUEST_WARN_MS / 1000.0) + 1.0

        captured = StringIO()
        with patch("sys.stdout", captured), patch.object(
            server_module.time, "perf_counter", side_effect=_fake_perf_counter
        ):
            self.request("GET", "/healthz")
        events = [
            json.loads(line)
            for line in captured.getvalue().splitlines()
            if line.strip()
        ]
        slow_events = [e for e in events if e.get("event") == "slow_request"]
        self.assertTrue(slow_events, f"no slow_request line in {events!r}")
        slow = slow_events[-1]
        self.assertEqual(slow["level"], "warn")
        self.assertEqual(slow["method"], "GET")
        self.assertGreater(slow["elapsed_ms"], server_module.SLOW_REQUEST_WARN_MS)


if __name__ == "__main__":
    unittest.main()
