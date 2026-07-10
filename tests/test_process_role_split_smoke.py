"""Integration smoke for the NDA_PROCESS_ROLE web/worker process split.

Spawns REAL server subprocesses (``sys.executable -m nda_automation.server``)
against a shared temp NDA_DATA_DIR and asserts the two-process contract
END-TO-END over real HTTP. Harness mirrors tests/frontend/add-clause.cjs (the
repo's real spawned-server pattern): repo-root cwd, throwaway data dir, free
port (never 8787), Gmail polling hard-off, key-free AI stub.

CONTRACT under test (feat/process-role-split):
  NDA_PROCESS_ROLE in {"all" (default), "web", "worker"}
    role=web    -> full HTTP app on PORT; Gmail scheduler NEVER starts
    role=worker -> NO app routes; minimal HTTP serving ONLY /healthz on PORT
                   (non-healthz -> 404/503); runs the Gmail scheduler
    role=all    -> today's single-process behavior

OBSERVABLE SCHEDULER SIGNAL: the server has no dedicated "scheduler started"
log line, but with NDA_TELEMETRY_SNAPSHOT_TICKS=1 the scheduler loop
(_gmail_sync_scheduler_loop -> _maybe_log_telemetry_snapshot) prints exactly
one '{"event": "telemetry_snapshot", ...}' JSON line to stdout on its FIRST
tick, which happens immediately at thread start -- BEFORE serve_forever() is
reached -- and fires even with NDA_GMAIL_SYNC_ENABLED=false (the tick counter
increments after the early-out toggle check). So: line present ~= scheduler
thread ran; line absent after the HTTP server is already answering ~= the
scheduler was never started. That is the cleanest external signal available
today; asserting on it is documented here as the smoke's scheduler oracle.

MERGE GATING: until feat/process-role-split lands, NDA_PROCESS_ROLE is an
unknown env var and every role behaves like "all". A cached capability probe
(spawn role=worker, check whether app routes are suppressed) skips the
web/worker cases with reason "feat/process-role-split not merged" so this file
is green against origin/main today: role=all parity + the cross-process
store-visibility case run unconditionally.

Opt-out: set NDA_SKIP_PROCESS_SMOKE=1 to skip the whole module (these tests
boot real subprocesses and take a few seconds each).
"""
from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape as escape_xml

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BOOT_TIMEOUT_SECONDS = 45.0
SCHEDULER_SIGNAL_NEEDLE = '"telemetry_snapshot"'
VISIBILITY_BUDGET_SECONDS = 10.0

pytestmark = pytest.mark.skipif(
    os.environ.get("NDA_SKIP_PROCESS_SMOKE", "").strip() == "1",
    reason="NDA_SKIP_PROCESS_SMOKE=1: subprocess smoke opted out",
)

# Env prefixes/keys that must never leak from the developer/CI environment into
# the spawned processes: credentials (no AI spend, no real Gmail/Drive), and
# every NDA_* knob (tests/conftest.py pins several globally; this smoke owns
# its own isolated configuration end-to-end).
_SCRUB_PREFIXES = ("NDA_", "GOOGLE_", "GMAIL_", "OPENROUTER", "ANTHROPIC", "DOCUSIGN")
_SCRUB_EXACT = {"PORT"}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _base_env(data_dir: str) -> dict[str, str]:
    """Minimal-but-bootable env, mirroring add-clause.cjs + tests/conftest.py.

    Keeps PATH/HOME/etc. (the interpreter needs them), scrubs every secret and
    NDA_* knob, then sets exactly what the server needs to boot key-free:
    isolated data dir + user store, Gmail polling hard-off (determinism), the
    no-network AI assessment stub, and snapshot-every-tick so the scheduler
    announces itself on stdout immediately (see module docstring).
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(_SCRUB_PREFIXES) and key not in _SCRUB_EXACT
    }
    env.update(
        {
            "PYTHONUNBUFFERED": "1",
            "NDA_DATA_DIR": data_dir,
            "NDA_USERS_PATH": str(Path(data_dir) / "users.json"),
            "NDA_GMAIL_SYNC_ENABLED": "false",
            "NDA_AI_REVIEW_ENABLED": "true",
            "NDA_AI_ASSESSMENT_STUB": "1",
            "NDA_TELEMETRY_SNAPSHOT_TICKS": "1",
        }
    )
    return env


class _ServerProcess:
    """A real ``python -m nda_automation.server`` subprocess with stdout capture.

    Always terminated in ``stop()`` (SIGTERM, then SIGKILL after 5s); use as a
    context manager so cleanup runs even when an assertion throws.
    """

    def __init__(self, *, role: str, port: int, data_dir: str) -> None:
        assert port != 8787, "must never use the operator's live 8787"
        self.port = port
        self.role = role
        env = _base_env(data_dir)
        # The role-split contract reads NDA_PROCESS_ROLE + PORT; today's main
        # reads --port. Pass all three so the same spawn is correct on both
        # sides of the feature landing.
        env["NDA_PROCESS_ROLE"] = role
        env["PORT"] = str(port)
        self._lines: list[str] = []
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "nda_automation.server",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._lines.append(line)

    # -- observation ---------------------------------------------------------
    def stdout_snapshot(self) -> str:
        return "".join(self._lines)

    def saw_line(self, needle: str) -> bool:
        return any(needle in line for line in list(self._lines))

    def wait_for_stdout(self, needle: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.saw_line(needle):
                return True
            if self.proc.poll() is not None:
                return self.saw_line(needle)
            time.sleep(0.05)
        return self.saw_line(needle)

    def wait_for_healthz(self, timeout: float = BOOT_TIMEOUT_SECONDS) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise AssertionError(
                    f"server (role={self.role}) exited rc={self.proc.returncode} "
                    f"before serving /healthz:\n{self.stdout_snapshot()}"
                )
            status, _ = _http_get(self.port, "/healthz")
            if status == 200:
                return
            time.sleep(0.15)
        raise AssertionError(
            f"server (role={self.role}) did not answer /healthz within "
            f"{timeout:.0f}s:\n{self.stdout_snapshot()}"
        )

    # -- lifecycle ------------------------------------------------------------
    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        self._reader.join(timeout=2)
        if self.proc.stdout is not None:
            self.proc.stdout.close()

    def __enter__(self) -> "_ServerProcess":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()


def _http_get(port: int, path: str, timeout: float = 5.0) -> tuple[int, bytes]:
    """(status, body) for a GET against the spawned server; -1 = not reachable."""
    url = f"http://127.0.0.1:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - loopback
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    except (urllib.error.URLError, ConnectionError, socket.timeout, OSError):
        return -1, b""


def _get_json(port: int, path: str) -> tuple[int, object]:
    status, body = _http_get(port, path)
    try:
        return status, json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return status, None


def _make_docx(paragraphs: list[str]) -> bytes:
    """Minimal DOCX bytes; same shape as tests/test_server.py's make_docx."""
    body = "".join(
        f"<w:p><w:r><w:t>{escape_xml(paragraph)}</w:t></w:r></w:p>"
        for paragraph in paragraphs
    )
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    with io.BytesIO() as output:
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("word/document.xml", document_xml)
        return output.getvalue()


# --------------------------------------------------------------------------
# Capability probe: is the role split actually implemented on this checkout?
# --------------------------------------------------------------------------
_PROBE_CACHE: dict[str, object] = {}


def _role_split_implemented() -> object:
    """True | False | "probe-error: ..." -- cached once per session.

    Boots a role=worker server and checks whether app routes are suppressed.
    On origin/main (env var unknown -> role defaults to all) /api/matters
    answers 200 and the probe reports False.
    """
    if "result" in _PROBE_CACHE:
        return _PROBE_CACHE["result"]
    result: object
    try:
        with tempfile.TemporaryDirectory(prefix="role-split-probe-") as data_dir:
            with _ServerProcess(role="worker", port=_free_port(), data_dir=data_dir) as server:
                server.wait_for_healthz()
                status, _ = _http_get(server.port, "/api/matters")
                result = status in (404, 503)
    except Exception as error:  # noqa: BLE001 - probe must never error the suite
        result = f"probe-error: {error}"
    _PROBE_CACHE["result"] = result
    return result


def _require_role_split() -> None:
    outcome = _role_split_implemented()
    if outcome is not True:
        pytest.skip(f"feat/process-role-split not merged (probe outcome: {outcome!r})")


# --------------------------------------------------------------------------
# (c) role=all: today's single-process parity -- GREEN against origin/main.
# --------------------------------------------------------------------------
def test_role_all_serves_app_and_starts_scheduler() -> None:
    with tempfile.TemporaryDirectory(prefix="role-split-all-") as data_dir:
        with _ServerProcess(role="all", port=_free_port(), data_dir=data_dir) as server:
            server.wait_for_healthz()

            health_status, health_payload = _get_json(server.port, "/healthz")
            assert health_status == 200
            assert isinstance(health_payload, dict) and health_payload.get("status") == "ok"

            index_status, index_body = _http_get(server.port, "/")
            assert index_status == 200, "role=all must serve the app shell"
            assert b"<" in index_body and index_body, "GET / should return the SPA HTML"

            matters_status, matters_payload = _get_json(server.port, "/api/matters")
            assert matters_status == 200, "role=all must serve app API routes"
            assert isinstance(matters_payload, dict) and "matters" in matters_payload

            # Scheduler DOES start under role=all: the first-tick telemetry
            # snapshot line must appear (see module docstring for the oracle).
            assert server.wait_for_stdout(SCHEDULER_SIGNAL_NEEDLE, timeout=15.0), (
                "role=all should start the Gmail scheduler (expected the first-tick "
                "telemetry_snapshot stdout line):\n" + server.stdout_snapshot()
            )


# --------------------------------------------------------------------------
# (a) role=web: full app, scheduler NEVER starts -- gated on the feature.
# --------------------------------------------------------------------------
def test_role_web_serves_app_without_scheduler() -> None:
    _require_role_split()
    with tempfile.TemporaryDirectory(prefix="role-split-web-") as data_dir:
        with _ServerProcess(role="web", port=_free_port(), data_dir=data_dir) as server:
            server.wait_for_healthz()

            health_status, _ = _http_get(server.port, "/healthz")
            assert health_status == 200

            index_status, _ = _http_get(server.port, "/")
            assert index_status == 200, "role=web must serve the app shell"

            matters_status, matters_payload = _get_json(server.port, "/api/matters")
            assert matters_status == 200, "role=web must serve app API routes"
            assert isinstance(matters_payload, dict) and "matters" in matters_payload

            # Scheduler must NOT start. The first-tick snapshot line is printed
            # BEFORE serve_forever() is reached when the scheduler does start,
            # so by the time /healthz answers it would already be in stdout; the
            # extra 4s grace makes the negative assertion robust, not racy.
            time.sleep(4.0)
            assert not server.saw_line(SCHEDULER_SIGNAL_NEEDLE), (
                "role=web must never start the Gmail scheduler, but the "
                "telemetry_snapshot scheduler-tick line appeared:\n"
                + server.stdout_snapshot()
            )


# --------------------------------------------------------------------------
# (b) role=worker: /healthz only, stays alive -- gated on the feature.
# --------------------------------------------------------------------------
def test_role_worker_serves_only_healthz_and_stays_alive() -> None:
    _require_role_split()
    with tempfile.TemporaryDirectory(prefix="role-split-worker-") as data_dir:
        with _ServerProcess(role="worker", port=_free_port(), data_dir=data_dir) as server:
            server.wait_for_healthz()

            matters_status, _ = _http_get(server.port, "/api/matters")
            assert matters_status in (404, 503), (
                f"role=worker must not serve app routes; /api/matters -> {matters_status}"
            )
            index_status, _ = _http_get(server.port, "/")
            assert index_status in (404, 503), (
                f"role=worker must not serve the app shell; / -> {index_status}"
            )

            # Long-lived worker: alive and healthy >= 3s after boot.
            time.sleep(3.0)
            assert server.proc.poll() is None, (
                "role=worker exited early:\n" + server.stdout_snapshot()
            )
            health_status, _ = _http_get(server.port, "/healthz")
            assert health_status == 200, "role=worker /healthz must stay 200"


# --------------------------------------------------------------------------
# (d) CROSS-PROCESS VISIBILITY -- GREEN against origin/main today.
#
# With the web process running, a SECOND process (mirroring a worker-side
# ingest) writes a matter into the SAME NDA_DATA_DIR through the real store
# code, then we poll the web process's /api/matters until it shows up and
# record the measured latency. This quantifies web-process store staleness
# empirically for the split.
# --------------------------------------------------------------------------
_OUT_OF_BAND_WRITER = textwrap.dedent(
    """
    import json
    import sys

    from nda_automation import ingestion_service

    document_bytes = sys.stdin.buffer.read()
    matter = ingestion_service.create_matter_from_document(
        filename="Role Split Smoke NDA.docx",
        document_bytes=document_bytes,
        source_type="manual_upload",
        board_column="in_review",
        defer_ai_review=True,
    )
    print(json.dumps({"id": matter["id"]}), flush=True)
    """
)


def test_cross_process_store_write_becomes_visible_in_web_process() -> None:
    docx_bytes = _make_docx(
        [
            "MUTUAL NON-DISCLOSURE AGREEMENT",
            "This Agreement is entered into between the parties for the purpose "
            "of evaluating a potential business relationship.",
            "This Agreement shall be governed by the laws of England and Wales.",
        ]
    )
    with tempfile.TemporaryDirectory(prefix="role-split-visibility-") as data_dir:
        with _ServerProcess(role="web", port=_free_port(), data_dir=data_dir) as server:
            server.wait_for_healthz()

            baseline_status, baseline_payload = _get_json(server.port, "/api/matters")
            assert baseline_status == 200
            assert isinstance(baseline_payload, dict)
            baseline_ids = {m.get("id") for m in baseline_payload.get("matters", [])}

            # Out-of-band write: a fresh interpreter (worker stand-in) pointed
            # at the SAME NDA_DATA_DIR; matter_store resolves its paths from
            # the env at import time, so a clean subprocess -- not an in-test
            # re-import -- is the faithful second-process store client.
            writer = subprocess.run(
                [sys.executable, "-c", _OUT_OF_BAND_WRITER],
                cwd=str(REPO_ROOT),
                env=_base_env(data_dir),
                input=docx_bytes,
                capture_output=True,
                timeout=120,
            )
            assert writer.returncode == 0, (
                "out-of-band store write failed:\n"
                + writer.stdout.decode("utf-8", "replace")
                + writer.stderr.decode("utf-8", "replace")
            )
            written_id = json.loads(writer.stdout.decode("utf-8").strip().splitlines()[-1])["id"]
            assert written_id and written_id not in baseline_ids

            # Poll the web process until the worker-written matter is visible.
            write_visible_from = time.perf_counter()
            latency: float | None = None
            deadline = write_visible_from + VISIBILITY_BUDGET_SECONDS
            while time.perf_counter() < deadline:
                status, payload = _get_json(server.port, "/api/matters")
                if status == 200 and isinstance(payload, dict):
                    ids = {m.get("id") for m in payload.get("matters", [])}
                    if written_id in ids:
                        latency = time.perf_counter() - write_visible_from
                        break
                time.sleep(0.1)

            assert latency is not None, (
                f"matter {written_id} written out-of-band did not become visible "
                f"via the web process within {VISIBILITY_BUDGET_SECONDS:.0f}s:\n"
                + server.stdout_snapshot()
            )
            # RECORDED MEASUREMENT: empirical web-process staleness bound.
            print(
                f"\n[role-split-smoke] cross-process visibility latency: "
                f"{latency * 1000:.0f} ms (budget {VISIBILITY_BUDGET_SECONDS:.0f}s)"
            )
            assert latency <= VISIBILITY_BUDGET_SECONDS
