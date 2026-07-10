"""Tests for the role="all" worker-process supervisor (feat/worker-supervisor).

Two layers:

* DETERMINISTIC UNIT tests drive ``server.WorkerSupervisor`` with an injected
  fake spawn + fake clock/sleep, so the restart backoff ladder, the crash-storm
  circuit breaker, and the SIGTERM->SIGKILL->reap shutdown are asserted with no
  real processes and no wall-clock waits. These prove the LOGIC.
* REAL-SUBPROCESS integration tests boot an actual ``python -m
  nda_automation.server`` and exercise the end-to-end contract: role="all"
  spawns exactly one worker child, NDA_WORKER_PROCESS=0 spawns none, a SIGTERM
  to the parent reaps the child (no orphan), a SIGKILL of the parent does not
  leave the child alive (parent-death watchdog), and two processes writing the
  matter store concurrently never corrupt it. These prove the WIRING.

Opt-out: NDA_SKIP_PROCESS_SMOKE=1 skips the (slow) real-subprocess cases.
"""
from __future__ import annotations

import io
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape as escape_xml

import pytest

from nda_automation import app_settings
from nda_automation import server

REPO_ROOT = Path(__file__).resolve().parents[1]
BOOT_TIMEOUT_SECONDS = 45.0

_skip_subprocess = pytest.mark.skipif(
    os.environ.get("NDA_SKIP_PROCESS_SMOKE", "").strip() == "1",
    reason="NDA_SKIP_PROCESS_SMOKE=1: subprocess tests opted out",
)


# =========================================================================== #
# process_role() + env-knob parsing
# =========================================================================== #
def test_process_role_defaults_to_all(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(app_settings.PROCESS_ROLE_ENV, raising=False)
    assert app_settings.process_role() == app_settings.PROCESS_ROLE_ALL


@pytest.mark.parametrize("value", ["web", "WEB", " worker ", "all"])
def test_process_role_accepts_known_roles(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(app_settings.PROCESS_ROLE_ENV, value)
    assert app_settings.process_role() == value.strip().lower()


def test_process_role_rejects_typo_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    # A typo must NOT silently fall back to "all" (that would start a second
    # poller in the web container -- the exact failure this split prevents).
    monkeypatch.setenv(app_settings.PROCESS_ROLE_ENV, "webb")
    with pytest.raises(ValueError):
        app_settings.process_role()


@pytest.mark.parametrize(
    "value,enabled",
    [("", True), ("1", True), ("yes", True), ("0", False), ("false", False), ("OFF", False)],
)
def test_worker_process_enabled_env(monkeypatch: pytest.MonkeyPatch, value: str, enabled: bool) -> None:
    if value:
        monkeypatch.setenv(server.WORKER_PROCESS_ENV, value)
    else:
        monkeypatch.delenv(server.WORKER_PROCESS_ENV, raising=False)
    assert server._worker_process_enabled() is enabled


# =========================================================================== #
# WorkerSupervisor -- deterministic unit tests (fake spawn/clock/sleep)
# =========================================================================== #
class _FakeChild:
    """A worker child that 'dies' the instant it is waited on (rc configurable)."""

    def __init__(self, rc: int = 1) -> None:
        self._rc = rc
        self.terminated = False
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        return self._rc

    def poll(self) -> int:
        return self._rc

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


def _make_supervisor(**kwargs):
    """A supervisor whose children die immediately, with a fixed clock.

    ``restart_delays`` records the backoff each restart used; the fake sleep
    never actually sleeps. The default fixed clock means every death lands in
    the same crash-window and uptime is 0 (< healthy_uptime), so the breaker and
    the backoff ladder behave deterministically.
    """
    spawn_calls: list[_FakeChild] = []

    def _spawn() -> _FakeChild:
        child = _FakeChild()
        spawn_calls.append(child)
        return child

    defaults = dict(
        backoff_initial=1.0,
        backoff_cap=30.0,
        healthy_uptime=60.0,
        crash_window=60.0,
        crash_threshold=5,
        clock=lambda: 1000.0,
    )
    defaults.update(kwargs)
    sup = server.WorkerSupervisor(spawn=_spawn, **defaults)
    return sup, spawn_calls


def test_restart_uses_exponential_backoff_then_trips_breaker() -> None:
    # crash_threshold=5: 5 immediate deaths => breaker trips. The 4 restarts
    # before the trip must use an EXPONENTIAL ladder (1,2,4,8) -- never a tight
    # 0-delay respawn loop.
    sup, spawns = _make_supervisor(crash_threshold=5)
    sup._child = sup._spawn()
    sup._monitor_loop()  # runs synchronously; returns when the breaker trips

    assert sup.restart_delays == [1.0, 2.0, 4.0, 8.0]
    assert all(delay > 0 for delay in sup.restart_delays), "never a tight (0s) loop"
    assert sup.breaker_tripped is True
    # 1 initial + 4 restarts spawned; the 5th death trips the breaker (no respawn).
    assert len(spawns) == 5
    assert sup.child is None  # breaker released the child; web keeps serving


def test_backoff_caps_at_30_seconds() -> None:
    # High threshold so the breaker never trips; a fake sleep stops the loop
    # after 9 backoffs so we can see the ladder saturate at the 30s cap.
    sup, _spawns = _make_supervisor(crash_threshold=1000)

    def _sleep(delay: float) -> bool:
        if len(sup.restart_delays) >= 9:
            sup._stop.set()
            return True
        return False

    sup._sleep = _sleep
    sup._child = sup._spawn()
    sup._monitor_loop()

    assert sup.restart_delays == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0, 30.0]


def test_healthy_uptime_resets_backoff() -> None:
    # A child that stayed up beyond healthy_uptime resets the ladder: its death
    # starts again at the initial backoff, not wherever the ladder had climbed.
    # clock() is called twice per iteration: spawned_at (before wait), now
    # (after wait). uptime = now - spawned_at.
    #   iter1: spawned_at=0, now=0     -> uptime 0  (< healthy) -> backoff 1.0
    #   iter2: spawned_at=0, now=100   -> uptime 100 (>= healthy) -> ladder RESET
    times = [0.0, 0.0, 0.0, 100.0]
    state = {"i": 0}

    def _clock() -> float:
        value = times[min(state["i"], len(times) - 1)]
        state["i"] += 1
        return value

    sup, _spawns = _make_supervisor(crash_threshold=1000, healthy_uptime=60.0, clock=_clock)

    calls = {"n": 0}

    def _sleep(delay: float) -> bool:
        calls["n"] += 1
        if calls["n"] >= 2:
            sup._stop.set()
            return True
        return False

    sup._sleep = _sleep
    sup._child = sup._spawn()
    sup._monitor_loop()

    # First death: uptime 0 -> backoff 1.0. Second death: uptime 100 (>=60) ->
    # ladder reset -> backoff 1.0 again (not 2.0).
    assert sup.restart_delays == [1.0, 1.0]


def test_crash_storm_breaker_stops_restarting_and_leaves_web_alone() -> None:
    sup, spawns = _make_supervisor(crash_threshold=3)
    sup._child = sup._spawn()
    sup._monitor_loop()
    assert sup.breaker_tripped is True
    # 3 deaths trip it: 1 initial + 2 restarts, then no more respawns.
    assert len(spawns) == 3
    # The supervisor thread returned; the web server (not modelled here) is
    # untouched -- nothing in stop-path was invoked, no exception escaped.


def test_stop_terminates_and_reaps_a_live_child() -> None:
    sup, _spawns = _make_supervisor()

    class _Graceful(_FakeChild):
        def poll(self):
            return None  # still running

        def wait(self, timeout=None):
            return 0  # exits promptly on SIGTERM

    child = _Graceful()
    sup._terminate_child(child, grace=1.0)
    assert child.terminated is True
    assert child.killed is False  # graceful SIGTERM was enough


def test_stop_sigkills_a_stubborn_child_after_grace() -> None:
    sup, _spawns = _make_supervisor()

    class _Stubborn(_FakeChild):
        def __init__(self):
            super().__init__()

        def poll(self):
            return None if not self.killed else -9

        def wait(self, timeout=None):
            if not self.killed:
                raise subprocess.TimeoutExpired(cmd="worker", timeout=timeout)
            return -9

    child = _Stubborn()
    sup._terminate_child(child, grace=0.01)
    assert child.terminated is True
    assert child.killed is True  # SIGTERM timed out -> escalated to SIGKILL


def test_stop_is_idempotent_and_safe_without_a_child() -> None:
    sup, _spawns = _make_supervisor()
    sup.stop()  # never started -> no child, must not raise
    sup.stop()  # twice -> still fine


# =========================================================================== #
# WorkerHealthHandler -- liveness contract (no subprocess)
# =========================================================================== #
class _AliveThread:
    def is_alive(self) -> bool:
        return True


class _DeadThread:
    def is_alive(self) -> bool:
        return False


def _serve_worker_health():
    from http.server import ThreadingHTTPServer

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.WorkerHealthHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, thread


def _get(port: int, path: str) -> int:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def test_worker_healthz_503_when_scheduler_dead(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_GMAIL_SYNC_SCHEDULER_THREAD", None)
    httpd, _thread = _serve_worker_health()
    try:
        port = httpd.server_address[1]
        assert _get(port, "/healthz") == 503  # never-started scheduler
        monkeypatch.setattr(server, "_GMAIL_SYNC_SCHEDULER_THREAD", _DeadThread())
        assert _get(port, "/healthz") == 503  # died scheduler
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_worker_healthz_200_when_scheduler_alive_and_404_otherwise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server, "_GMAIL_SYNC_SCHEDULER_THREAD", _AliveThread())
    httpd, _thread = _serve_worker_health()
    try:
        port = httpd.server_address[1]
        assert _get(port, "/healthz") == 200
        # The worker must NEVER serve app routes.
        assert _get(port, "/api/matters") == 404
        assert _get(port, "/") == 404
    finally:
        httpd.shutdown()
        httpd.server_close()


# =========================================================================== #
# No fork on bare import (guard the test suite against itself)
# =========================================================================== #
@_skip_subprocess
def test_importing_server_module_spawns_no_worker() -> None:
    # A process that merely imports the server module (as pytest itself does)
    # must NEVER fork a worker -- the split may only happen on the serve
    # entrypoint. The importer just imports + sleeps; THIS test process (not the
    # importer) inspects the importer's children, so there is no ps-counts-itself
    # artifact.
    importer = subprocess.Popen(
        [sys.executable, "-c", "import time; import nda_automation.server; time.sleep(4)"],
        cwd=str(REPO_ROOT),
    )
    try:
        time.sleep(2.0)  # a stray fork would have appeared by now
        assert importer.poll() is None, "importer exited unexpectedly"
        assert _child_pids(importer.pid) == [], "bare import of the server module spawned a child"
    finally:
        importer.terminate()
        importer.wait(timeout=10)


# =========================================================================== #
# Real-subprocess integration helpers
# =========================================================================== #
def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _base_env(data_dir: str) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("NDA_", "GOOGLE_", "GMAIL_", "OPENROUTER", "ANTHROPIC", "DOCUSIGN"))
        and key != "PORT"
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


def _child_pids(parent_pid: int) -> list[int]:
    out = subprocess.run(["ps", "-eo", "pid=,ppid="], capture_output=True, text=True)
    pids: list[int] = []
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == str(parent_pid):
            try:
                pids.append(int(parts[0]))
            except ValueError:
                continue
    return pids


def _pid_alive(pid: int) -> bool:
    return bool(subprocess.run(["ps", "-o", "pid=", "-p", str(pid)], capture_output=True, text=True).stdout.strip())


def _http_get(port: int, path: str) -> int:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code
    except (urllib.error.URLError, ConnectionError, socket.timeout, OSError):
        return -1


class _Server:
    def __init__(self, *, role: str, extra_env: dict[str, str] | None = None) -> None:
        self.data_dir = tempfile.mkdtemp(prefix=f"supervisor-{role}-")
        self.port = _free_port()
        env = _base_env(self.data_dir)
        if extra_env:
            env.update(extra_env)
        self._lines: list[str] = []
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "nda_automation.server", "--host", "127.0.0.1",
             "--port", str(self.port), "--role", role],
            cwd=str(REPO_ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._lines.append(line)

    def stdout(self) -> str:
        return "".join(self._lines)

    def wait_healthz(self, timeout: float = BOOT_TIMEOUT_SECONDS) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise AssertionError(f"exited rc={self.proc.returncode}:\n{self.stdout()}")
            if _http_get(self.port, "/healthz") == 200:
                return
            time.sleep(0.15)
        raise AssertionError(f"no /healthz within {timeout:.0f}s:\n{self.stdout()}")

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)

    def __enter__(self) -> "_Server":
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


# =========================================================================== #
# Real-subprocess integration -- role gating + supervisor lifecycle
# =========================================================================== #
@_skip_subprocess
def test_role_all_spawns_exactly_one_worker_child() -> None:
    with _Server(role="all") as sv:
        sv.wait_healthz()
        assert _http_get(sv.port, "/") == 200, "web parent must serve the app"
        assert _http_get(sv.port, "/api/matters") == 200
        # Give the child a moment to spawn + tick.
        deadline = time.monotonic() + 10
        kids: list[int] = []
        while time.monotonic() < deadline:
            kids = _child_pids(sv.proc.pid)
            if kids:
                break
            time.sleep(0.2)
        assert len(kids) == 1, f"role=all must fork exactly one worker child, saw {kids}"
        # The scheduler runs in the CHILD (its telemetry line reaches the shared stdout).
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and '"telemetry_snapshot"' not in sv.stdout():
            time.sleep(0.2)
        assert '"telemetry_snapshot"' in sv.stdout(), sv.stdout()


@_skip_subprocess
def test_worker_process_disabled_spawns_no_child() -> None:
    with _Server(role="all", extra_env={"NDA_WORKER_PROCESS": "0"}) as sv:
        sv.wait_healthz()
        assert _http_get(sv.port, "/") == 200
        time.sleep(3.0)  # a child would have spawned by now
        assert _child_pids(sv.proc.pid) == [], "NDA_WORKER_PROCESS=0 must not fork a worker"
        # Monolith: the scheduler runs IN the web process (telemetry appears).
        assert '"telemetry_snapshot"' in sv.stdout(), sv.stdout()


@_skip_subprocess
def test_role_web_starts_no_scheduler_and_no_child() -> None:
    with _Server(role="web") as sv:
        sv.wait_healthz()
        assert _http_get(sv.port, "/") == 200
        assert _http_get(sv.port, "/api/matters") == 200
        time.sleep(4.0)
        assert _child_pids(sv.proc.pid) == [], "role=web must never fork a worker"
        assert '"telemetry_snapshot"' not in sv.stdout(), (
            "role=web must never start the scheduler:\n" + sv.stdout()
        )


@_skip_subprocess
def test_role_worker_binds_only_healthz_no_app_port() -> None:
    with _Server(role="worker") as sv:
        sv.wait_healthz()
        assert _http_get(sv.port, "/api/matters") == 404, "worker must not serve app routes"
        assert _http_get(sv.port, "/") == 404
        assert _http_get(sv.port, "/healthz") == 200


@_skip_subprocess
def test_sigterm_to_parent_reaps_child_no_orphan() -> None:
    sv = _Server(role="all")
    try:
        sv.wait_healthz()
        deadline = time.monotonic() + 10
        kids: list[int] = []
        while time.monotonic() < deadline:
            kids = _child_pids(sv.proc.pid)
            if kids:
                break
            time.sleep(0.2)
        assert kids, "no worker child was spawned"
        child = kids[0]

        sv.proc.send_signal(signal.SIGTERM)
        sv.proc.wait(timeout=20)
        assert sv.proc.returncode is not None

        # The child must be gone within a few seconds (reaped by the parent).
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and _pid_alive(child):
            time.sleep(0.2)
        assert not _pid_alive(child), f"worker child {child} orphaned after parent SIGTERM"
    finally:
        sv.stop()


@_skip_subprocess
def test_parent_sigkill_child_does_not_survive() -> None:
    sv = _Server(role="all")
    try:
        sv.wait_healthz()
        deadline = time.monotonic() + 10
        kids: list[int] = []
        while time.monotonic() < deadline:
            kids = _child_pids(sv.proc.pid)
            if kids:
                break
            time.sleep(0.2)
        assert kids, "no worker child was spawned"
        child = kids[0]

        # Hard-kill the parent: no signal handler runs. The child must NOT
        # outlive it -- the parent-death watchdog (getppid poll) exits it.
        sv.proc.send_signal(signal.SIGKILL)
        sv.proc.wait(timeout=10)

        deadline = time.monotonic() + 8  # watchdog polls ~1s
        while time.monotonic() < deadline and _pid_alive(child):
            time.sleep(0.2)
        assert not _pid_alive(child), (
            f"worker child {child} survived a SIGKILL of the parent (orphan poller)"
        )
    finally:
        sv.stop()


@_skip_subprocess
def test_supervisor_restarts_a_dying_real_child_with_backoff() -> None:
    """The REAL monitor loop respawns a REAL (immediately-exiting) subprocess.

    Proves the wiring end-to-end: distinct child pids across restarts, an
    increasing backoff, and a BOUNDED restart count over the window (a tight
    loop would produce dozens in the same span).
    """
    spawned: list[subprocess.Popen] = []

    def _spawn() -> subprocess.Popen:
        proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(3)"])
        spawned.append(proc)
        return proc

    sup = server.WorkerSupervisor(
        spawn=_spawn,
        backoff_initial=0.3,
        backoff_cap=2.0,
        crash_threshold=1000,  # don't let the breaker end the test early
    )
    try:
        sup.start()
        time.sleep(2.5)
    finally:
        sup.stop()  # ALWAYS stop the monitor + reap, even if an assert throws

    pids = [proc.pid for proc in spawned]
    assert len(pids) >= 2, "supervisor should have restarted the dying child"
    assert len(pids) < 15, f"restart count {len(pids)} looks like a tight loop"
    assert len(set(pids)) == len(pids), "each restart must be a fresh process"
    # Strictly-increasing backoff prefix (until the cap), never a flat 0.
    assert sup.restart_delays == sorted(sup.restart_delays)
    assert sup.restart_delays and sup.restart_delays[0] == 0.3
    # No survivor: every spawned child (all sys.exit(3)) is dead + reaped.
    for proc in spawned:
        assert proc.poll() is not None, f"spawned child {proc.pid} still alive after stop()"


# =========================================================================== #
# Cross-process matter-store safety (exercise the existing flock)
# =========================================================================== #
def _make_docx(paragraphs: list[str]) -> bytes:
    body = "".join(
        f"<w:p><w:r><w:t>{escape_xml(paragraph)}</w:t></w:r></w:p>" for paragraph in paragraphs
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


_CONCURRENT_WRITER = """
import os, sys
from nda_automation import matter_store

tag = sys.argv[1]
count = int(sys.argv[2])
docx = sys.stdin.buffer.read()
for i in range(count):
    matter_store.create_matter(
        source_filename=f"{tag}-{i}.docx",
        document_bytes=docx,
        extracted_text="MUTUAL NON-DISCLOSURE AGREEMENT",
        review_result={"status": "not_reviewed", "clauses": []},
        triage={},
        source_type="manual_upload",
        board_column="in_review",
        owner_user_id="concurrency-tester",
    )
print("DONE", tag, flush=True)
"""


@_skip_subprocess
def test_two_processes_write_matter_store_concurrently_without_corruption() -> None:
    docx = _make_docx(["MUTUAL NON-DISCLOSURE AGREEMENT", "Governed by England and Wales."])
    per_writer = 8
    with tempfile.TemporaryDirectory(prefix="supervisor-store-race-") as data_dir:
        env = _base_env(data_dir)

        def _writer(tag: str) -> subprocess.Popen:
            return subprocess.Popen(
                [sys.executable, "-c", _CONCURRENT_WRITER, tag, str(per_writer)],
                cwd=str(REPO_ROOT), env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=False,
            )

        writers = [_writer("A"), _writer("B")]
        outputs = []
        try:
            for writer in writers:
                out, _ = writer.communicate(input=docx, timeout=180)
                outputs.append((writer.returncode, out.decode("utf-8", "replace")))
        finally:
            # No survivor: kill + reap any writer still alive (e.g. a timeout).
            for writer in writers:
                if writer.poll() is None:
                    writer.kill()
                    writer.wait(timeout=10)
        for rc, out in outputs:
            assert rc == 0, f"concurrent writer failed rc={rc}:\n{out}"

        # Read the store back from a THIRD fresh process (paths are resolved at
        # import time from NDA_DATA_DIR) and assert every write survived intact.
        reader = subprocess.run(
            [sys.executable, "-c",
             "from nda_automation import matter_store;"
             "ms = matter_store.list_matters('concurrency-tester');"
             "print(len(ms))"],
            cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=60,
        )
        assert reader.returncode == 0, reader.stderr
        total = int(reader.stdout.strip().splitlines()[-1])
        assert total == 2 * per_writer, (
            f"expected {2 * per_writer} matters after concurrent writes, got {total} "
            "(lost update / corruption under the store flock)"
        )
