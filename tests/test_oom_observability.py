"""Tests for the prod OOM memory/queue observability helpers.

Covers the stdlib-only RSS / cgroup-limit probes in ``process_memory``, the
``memory_headroom`` / ``disk_headroom`` / ``data_dir_boot_count`` deployment-status
checks, the telemetry gauges, and the review-queue-depth gauge in the status
payload. The cgroup/RSS/disk reads are mocked so the suite is deterministic on any
platform.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from nda_automation import deployment, ingestion_service, matter_store, process_memory, telemetry


def setup_function(_function):
    telemetry.reset()
    process_memory._reset_limit_cache_for_tests()


def teardown_function(_function):
    telemetry.reset()
    process_memory._reset_limit_cache_for_tests()


# --------------------------------------------------------------------------- #
# process_memory: current_rss_bytes
# --------------------------------------------------------------------------- #


def test_current_rss_prefers_proc_statm_pages_times_pagesize():
    # statm field 2 (resident pages) = 100; page size 4096 -> 409600 bytes.
    fake_statm = "200 100 50 1 0 30 0\n"
    with patch("builtins.open", _fake_open(fake_statm)):
        with patch("os.sysconf", return_value=4096):
            assert process_memory._rss_from_proc_statm() == 100 * 4096


def test_current_rss_falls_back_to_getrusage_when_proc_absent():
    with patch.object(process_memory, "_rss_from_proc_statm", return_value=None):
        with patch.object(process_memory, "_rss_from_getrusage", return_value=123456):
            assert process_memory.current_rss_bytes() == 123456


def test_getrusage_branches_bytes_on_macos_kib_on_linux():
    class _RU:
        ru_maxrss = 2048

    with patch.object(process_memory.resource, "getrusage", return_value=_RU()):
        with patch("sys.platform", "darwin"):
            assert process_memory._rss_from_getrusage() == 2048  # already bytes
        with patch("sys.platform", "linux"):
            assert process_memory._rss_from_getrusage() == 2048 * 1024  # KiB -> bytes


def test_current_rss_returns_none_when_all_sources_fail():
    with patch.object(process_memory, "_rss_from_proc_statm", return_value=None):
        with patch.object(process_memory, "_rss_from_getrusage", return_value=None):
            assert process_memory.current_rss_bytes() is None


def test_proc_statm_read_error_returns_none():
    with patch("builtins.open", side_effect=OSError):
        assert process_memory._rss_from_proc_statm() is None


# --------------------------------------------------------------------------- #
# process_memory: container_memory_limit_bytes
# --------------------------------------------------------------------------- #


def test_container_limit_reads_cgroup_v2_first():
    with patch.object(process_memory, "_read_pseudo_file", side_effect=_pseudo({
        process_memory._CGROUP_V2_MEMORY_MAX: "2147483648",
    })):
        assert process_memory.container_memory_limit_bytes() == 2147483648


def test_container_limit_falls_back_to_cgroup_v1():
    def _reader(path):
        if path == process_memory._CGROUP_V2_MEMORY_MAX:
            return None  # v2 absent
        if path == process_memory._CGROUP_V1_MEMORY_LIMIT:
            return "536870912"
        return None

    with patch.object(process_memory, "_read_pseudo_file", side_effect=_reader):
        assert process_memory.container_memory_limit_bytes() == 536870912


def test_container_limit_v2_max_sentinel_is_unknown():
    with patch.object(process_memory, "_read_pseudo_file", side_effect=_pseudo({
        process_memory._CGROUP_V2_MEMORY_MAX: "max",
    })):
        assert process_memory.container_memory_limit_bytes() is None


def test_container_limit_v1_unlimited_sentinel_is_unknown():
    huge = str(1 << 63)
    with patch.object(process_memory, "_read_pseudo_file", side_effect=_pseudo({
        process_memory._CGROUP_V1_MEMORY_LIMIT: huge,
    })):
        assert process_memory.container_memory_limit_bytes() is None


def test_container_limit_unreadable_is_none_and_cached():
    calls = {"n": 0}

    def _reader(_path):
        calls["n"] += 1
        return None

    with patch.object(process_memory, "_read_pseudo_file", side_effect=_reader):
        assert process_memory.container_memory_limit_bytes() is None
        # Second call must hit the cache, not re-read the pseudo-files.
        assert process_memory.container_memory_limit_bytes() is None
    assert calls["n"] == 2  # one v2 + one v1 read on the first call only


def test_memory_usage_assembles_headroom_and_fraction():
    with patch.object(process_memory, "current_rss_bytes", return_value=800):
        with patch.object(process_memory, "container_memory_limit_bytes", return_value=1000):
            usage = process_memory.memory_usage()
    assert usage == {
        "rss_bytes": 800,
        "limit_bytes": 1000,
        "headroom_bytes": 200,
        "used_fraction": 0.8,
    }


def test_memory_usage_unknown_limit_leaves_derived_none():
    with patch.object(process_memory, "current_rss_bytes", return_value=800):
        with patch.object(process_memory, "container_memory_limit_bytes", return_value=None):
            usage = process_memory.memory_usage()
    assert usage["rss_bytes"] == 800
    assert usage["limit_bytes"] is None
    assert usage["headroom_bytes"] is None
    assert usage["used_fraction"] is None


# --------------------------------------------------------------------------- #
# deployment: memory_headroom check (asymmetric)
# --------------------------------------------------------------------------- #


def test_memory_headroom_unknown_limit_is_advisory_ok():
    check = deployment._deployment_memory_headroom_check({
        "rss_bytes": 800, "limit_bytes": None, "headroom_bytes": None, "used_fraction": None,
    })
    assert check["ok"] is True
    assert "unknown" in check["message"].lower()


def test_memory_headroom_below_warn_is_ok():
    check = deployment._deployment_memory_headroom_check({
        "rss_bytes": 500, "limit_bytes": 1000, "headroom_bytes": 500, "used_fraction": 0.5,
    })
    assert check["ok"] is True
    assert "50%" in check["message"]


def test_memory_headroom_at_or_above_warn_is_red():
    check = deployment._deployment_memory_headroom_check({
        "rss_bytes": 900, "limit_bytes": 1000, "headroom_bytes": 100, "used_fraction": 0.9,
    })
    assert check["ok"] is False
    assert "90%" in check["message"]


# --------------------------------------------------------------------------- #
# deployment: disk_headroom check (asymmetric, scoped to existing NDA_DATA_DIR)
# --------------------------------------------------------------------------- #


def test_disk_usage_unknown_without_data_dir_env(tmp_path):
    with patch.dict(os.environ, {"NDA_DATA_DIR": ""}):
        with patch.object(matter_store, "DATA_DIR", tmp_path):
            disk = deployment._data_dir_disk_usage()
    assert disk["total_bytes"] is None
    assert disk["used_fraction"] is None


def test_disk_usage_unknown_when_dir_absent():
    # NDA_DATA_DIR set but pointing at a non-existent mount (the unmounted /var/data
    # case) -> stays unknown, never reads the host root.
    with patch.dict(os.environ, {"NDA_DATA_DIR": "/var/data"}):
        with patch.object(matter_store, "DATA_DIR", Path("/var/data/does-not-exist-xyz")):
            disk = deployment._data_dir_disk_usage()
    assert disk["total_bytes"] is None
    assert disk["used_fraction"] is None


def test_disk_usage_reports_when_dir_exists(tmp_path):
    class _Usage:
        total, used, free = 1_000, 600, 400

    with patch.dict(os.environ, {"NDA_DATA_DIR": str(tmp_path)}):
        with patch.object(matter_store, "DATA_DIR", tmp_path):
            with patch.object(deployment.shutil, "disk_usage", return_value=_Usage()):
                disk = deployment._data_dir_disk_usage()
    assert disk == {
        "total_bytes": 1000, "used_bytes": 600, "free_bytes": 400, "used_fraction": 0.6,
    }


def test_disk_headroom_unknown_is_advisory_ok():
    check = deployment._deployment_disk_headroom_check({"used_fraction": None})
    assert check["ok"] is True
    assert "unknown" in check["message"].lower()


def test_disk_headroom_full_is_red():
    check = deployment._deployment_disk_headroom_check({"used_fraction": 0.9})
    assert check["ok"] is False
    assert "90%" in check["message"]


def test_disk_headroom_below_warn_is_ok():
    check = deployment._deployment_disk_headroom_check({"used_fraction": 0.5})
    assert check["ok"] is True


# --------------------------------------------------------------------------- #
# deployment: inbound kill-switch echo + boot-count check
# --------------------------------------------------------------------------- #


def test_boot_count_check_zero_is_unknown_ok():
    check = deployment._deployment_boot_count_check(0)
    assert check["ok"] is True
    assert check["boot_count"] == 0


def test_boot_count_check_positive_reports_count():
    check = deployment._deployment_boot_count_check(7)
    assert check["ok"] is True
    assert check["boot_count"] == 7
    assert "7" in check["message"]


# --------------------------------------------------------------------------- #
# deployment: full status payload carries the new blocks + checks
# --------------------------------------------------------------------------- #


def test_deployment_status_exposes_memory_disk_and_review_observability():
    with patch.object(process_memory, "current_rss_bytes", return_value=500_000_000):
        with patch.object(process_memory, "container_memory_limit_bytes", return_value=2_000_000_000):
            dep = deployment._deployment_status_for_host("127.0.0.1")

    assert dep["memory"]["rss_bytes"] == 500_000_000
    assert dep["memory"]["limit_bytes"] == 2_000_000_000
    assert dep["memory"]["used_fraction"] == 0.25
    assert "disk" in dep
    assert dep["inbound_review_queue_depth"] is not None
    assert "data_dir_boot_count" in dep

    check_ids = {c["id"] for c in dep["checks"]}
    for expected in ("memory_headroom", "disk_headroom", "data_dir_boot_count"):
        assert expected in check_ids


def test_deployment_status_unknown_memory_does_not_flag_red():
    # macOS/local: no cgroup limit -> memory_headroom must stay ok=True.
    with patch.object(process_memory, "current_rss_bytes", return_value=500_000_000):
        with patch.object(process_memory, "container_memory_limit_bytes", return_value=None):
            dep = deployment._deployment_status_for_host("127.0.0.1")
    mem_check = {c["id"]: c for c in dep["checks"]}["memory_headroom"]
    assert mem_check["ok"] is True


# --------------------------------------------------------------------------- #
# telemetry: gauges
# --------------------------------------------------------------------------- #


def test_set_gauge_overwrites_and_snapshot_exposes_gauges():
    telemetry.set_gauge("peak_rss_mb", 100.0)
    telemetry.set_gauge("peak_rss_mb", 250.5)
    snap = telemetry.snapshot()
    assert snap["gauges"]["peak_rss_mb"] == 250.5


def test_gauge_max_keeps_high_water():
    telemetry.gauge_max("max_rss_mb", 100.0)
    telemetry.gauge_max("max_rss_mb", 80.0)  # lower -> ignored
    telemetry.gauge_max("max_rss_mb", 300.0)  # higher -> kept
    assert telemetry.snapshot()["gauges"]["max_rss_mb"] == 300.0


def test_gauge_rejects_non_finite_values():
    telemetry.set_gauge("g", float("nan"))
    telemetry.set_gauge("g", float("inf"))
    telemetry.set_gauge("g", "not-a-number")  # type: ignore[arg-type]
    assert "g" not in telemetry.snapshot()["gauges"]


def test_reset_clears_gauges():
    telemetry.set_gauge("g", 5.0)
    telemetry.reset()
    assert telemetry.snapshot()["gauges"] == {}


# --------------------------------------------------------------------------- #
# telemetry: inbound-review flags in health_summary
# --------------------------------------------------------------------------- #


def test_health_summary_carries_inbound_review_block():
    health = telemetry.health_summary({
        "inbound_ai_review_completed": 8,
        "inbound_ai_review_failed": 2,
        "inbound_ai_review_queue_full": 0,
    })
    block = health["inbound_review"]
    assert block["completed"] == 8
    assert block["failed"] == 2
    assert block["attempted"] == 10
    assert block["failure_rate"] == 0.2


def test_health_summary_queue_full_raises_warn():
    health = telemetry.health_summary({"inbound_ai_review_queue_full": 1})
    assert health["status"] == "warn"
    assert any("queue" in alert.lower() for alert in health["alerts"])


def test_health_summary_high_inbound_failure_rate_raises_warn():
    health = telemetry.health_summary({
        "inbound_ai_review_completed": 5,
        "inbound_ai_review_failed": 5,  # 50% over 10 attempts
    })
    assert health["status"] == "warn"
    assert any("failure rate" in alert.lower() for alert in health["alerts"])


def test_health_summary_clean_inbound_is_ok():
    health = telemetry.health_summary({
        "inbound_ai_review_completed": 100,
        "inbound_ai_review_failed": 0,
        "inbound_ai_review_queue_full": 0,
    })
    assert health["status"] == "ok"


# --------------------------------------------------------------------------- #
# ingestion: public pool accessors + per-review memory log
# --------------------------------------------------------------------------- #


def test_pool_pending_and_queue_depth_accessors_are_public_and_safe():
    pool = ingestion_service._InboundReviewWorkerPool()
    assert pool.pending_count() == 0
    assert pool.queue_depth() == 0


def test_log_inbound_review_memory_sets_gauges():
    with patch.object(process_memory, "current_rss_bytes", return_value=300 * 1024 * 1024):
        with patch.object(process_memory, "container_memory_limit_bytes", return_value=2048 * 1024 * 1024):
            ingestion_service._log_inbound_review_memory("matter-1")
    gauges = telemetry.snapshot()["gauges"]
    assert gauges["inbound_ai_review_last_peak_rss_mb"] == 300.0
    assert gauges["inbound_ai_review_max_peak_rss_mb"] == 300.0
    assert gauges["inbound_ai_review_last_headroom_mb"] == (2048 - 300)


def test_log_inbound_review_memory_is_noop_when_rss_unreadable():
    with patch.object(process_memory, "current_rss_bytes", return_value=None):
        ingestion_service._log_inbound_review_memory("matter-1")
    assert telemetry.snapshot()["gauges"] == {}


def test_log_inbound_review_memory_never_raises():
    with patch.object(process_memory, "current_rss_bytes", side_effect=RuntimeError("boom")):
        # Must swallow the error -- observability must never break a review.
        ingestion_service._log_inbound_review_memory("matter-1")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _fake_open(contents: str):
    """A patch target for builtins.open returning a context manager over contents."""

    class _Handle:
        def __enter__(self_inner):
            return self_inner

        def __exit__(self_inner, *exc):
            return False

        def read(self_inner):
            return contents

    def _opener(*_args, **_kwargs):
        return _Handle()

    return _opener


def _pseudo(mapping: dict[str, str]):
    """Return a _read_pseudo_file side_effect that maps known paths, else None."""

    def _reader(path):
        return mapping.get(path)

    return _reader
