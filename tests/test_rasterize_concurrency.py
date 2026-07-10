"""Defect A: process-wide rasterize concurrency cap.

The per-page pixmap budget + 200-page cap bound a SINGLE render, and the
MatterRenderCoordinator dedupes concurrent renders of the SAME matter -- but
nothing bounded PyMuPDF rasterize ACROSS matters. N users opening N different
matters ran N concurrent full-document rasterize loops, each holding multi-MB
pixmaps + PNG encode buffers: the top OOM risk on the 2 GiB box.

These tests prove the new _RASTERIZE_SEMAPHORE serializes rasterize across
matters and that an acquire-timeout degrades to the RENDERING status the FE
poller already retries on (never a 500, never an unbounded wait).
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from nda_automation import document_rendering
from nda_automation.document_rendering import (
    MAX_CONCURRENT_RASTERIZE_RENDERS,
    RENDERING_STATUS,
    RenderedPdfPageImage,
    render_pdf_to_page_image_manifest,
)


class _ConcurrencyProbeRenderer:
    """Records the max number of rasterize loops running at once."""

    name = "concurrency-probe"

    def __init__(self, ledger: dict) -> None:
        self.ledger = ledger

    def is_available(self) -> bool:
        return True

    def render_pdf_to_page_images(self, pdf_path: Path, output_dir: Path, *, dpi: int):
        lock = self.ledger["lock"]
        with lock:
            self.ledger["current"] += 1
            self.ledger["max"] = max(self.ledger["max"], self.ledger["current"])
        try:
            # Hold the slot long enough that any un-serialized overlap is observed.
            time.sleep(0.05)
            image_path = output_dir / "page-1.png"
            image_path.write_bytes(b"\x89PNG\r\nfake page\n")
            return [
                RenderedPdfPageImage(
                    page_number=1,
                    image_path=image_path,
                    width=10,
                    height=10,
                    dpi=dpi,
                    scale=1.0,
                )
            ]
        finally:
            with lock:
                self.ledger["current"] -= 1


def _write_pdf(tmp_path: Path, name: str) -> Path:
    pdf_path = tmp_path / f"{name}.pdf"
    pdf_path.write_bytes(f"%PDF-1.7\n{name}\n%%EOF\n".encode())
    return pdf_path


def test_rasterize_across_matters_never_exceeds_slot_count(tmp_path):
    n_matters = 6
    ledger = {"lock": threading.Lock(), "current": 0, "max": 0}
    results: list = []
    results_lock = threading.Lock()

    def _run(i: int) -> None:
        # Distinct matter -> distinct cache_key AND distinct pdf: no per-matter
        # dedupe applies, so ONLY the process-wide semaphore can bound concurrency.
        cache_key = f"matter-{i}"
        pdf_path = _write_pdf(tmp_path, cache_key)
        manifest = render_pdf_to_page_image_manifest(
            pdf_path,
            cache_key=cache_key,
            cache_dir=tmp_path / "cache",
            renderer=_ConcurrencyProbeRenderer(ledger),
            dpi=144,
        )
        with results_lock:
            results.append(manifest)

    threads = [threading.Thread(target=_run, args=(i,)) for i in range(n_matters)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == n_matters
    assert all(m.status == document_rendering.READY_STATUS for m in results)
    # The instrumented ceiling never exceeds the configured slot count.
    assert ledger["max"] <= MAX_CONCURRENT_RASTERIZE_RENDERS
    assert ledger["max"] >= 1
    # And the slot is fully released afterwards.
    assert ledger["current"] == 0


def test_rasterize_acquire_timeout_returns_rendering_status_not_500(tmp_path, monkeypatch):
    # Occupy the only slot, then a competing render cannot acquire within the wait
    # window and must degrade to RENDERING (a retry signal) rather than raise.
    monkeypatch.setattr(document_rendering, "RASTERIZE_QUEUE_WAIT_SECONDS", 0.05)
    pdf_path = _write_pdf(tmp_path, "busy-matter")

    acquired = document_rendering._RASTERIZE_SEMAPHORE.acquire(timeout=1.0)
    assert acquired, "test could not acquire the rasterize slot"
    try:
        manifest = render_pdf_to_page_image_manifest(
            pdf_path,
            cache_key="busy-matter",
            cache_dir=tmp_path / "cache",
            renderer=_ConcurrencyProbeRenderer(
                {"lock": threading.Lock(), "current": 0, "max": 0}
            ),
            dpi=144,
        )
    finally:
        document_rendering._RASTERIZE_SEMAPHORE.release()

    assert manifest.status == RENDERING_STATUS
    assert manifest.error_code == "page_render_busy"
    # Nothing was rasterized and no failure metadata was persisted (transient).
    assert manifest.pages == ()


def test_rasterize_slot_released_even_when_render_raises(tmp_path):
    # A rasterize that RAISES must still release the slot, or the box would wedge
    # after the first failure. Prove the slot is reusable after an error.
    class _Boom:
        name = "boom"

        def is_available(self) -> bool:
            return True

        def render_pdf_to_page_images(self, pdf_path, output_dir, *, dpi):
            raise RuntimeError("rasterize blew up")

    pdf_path = _write_pdf(tmp_path, "boom-matter")
    manifest = render_pdf_to_page_image_manifest(
        pdf_path,
        cache_key="boom-matter",
        cache_dir=tmp_path / "cache",
        renderer=_Boom(),
        dpi=144,
    )
    assert manifest.status == document_rendering.ERROR_STATUS

    # The slot must be free again: acquire it immediately without blocking.
    got = document_rendering._RASTERIZE_SEMAPHORE.acquire(timeout=0.5)
    assert got, "slot was not released after a raising rasterize"
    document_rendering._RASTERIZE_SEMAPHORE.release()
