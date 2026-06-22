from __future__ import annotations

from io import BytesIO
import os
from pathlib import Path
import sys
import time
import unittest

from docx import Document

from nda_automation import pdf_docx_reconstruction
from nda_automation.pdf_docx_reconstruction import (
    MAX_CONCURRENT_PDF_DOCX_CONVERSIONS,
    PdfDocxReconstructionBusy,
    PdfDocxReconstructionFailedError,
    PdfDocxReconstructionTooLargeError,
    _run_pdf_docx_child,
    reconstruct_pdf_to_docx,
)


def make_valid_docx(text: str = "Reconstructed PDF content") -> bytes:
    document = Document()
    document.add_paragraph(text)
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def make_valid_pdf(page_count: int) -> bytes:
    import fitz  # type: ignore[import-not-found]

    document = fitz.open()
    for _ in range(page_count):
        document.new_page()
    data = document.tobytes()
    document.close()
    return data


class CountingValidConverter:
    name = "fake-counting"

    def __init__(self) -> None:
        self.calls = 0

    def is_available(self) -> bool:
        return True

    def convert_pdf_to_docx(self, source_path: Path, output_path: Path) -> None:
        self.calls += 1
        output_path.write_bytes(make_valid_docx())


class BlockingConverter:
    """Holds the conversion slot open until released, to exhaust the semaphore."""

    name = "fake-blocking"

    def __init__(self, release_event) -> None:
        self.release_event = release_event

    def is_available(self) -> bool:
        return True

    def convert_pdf_to_docx(self, source_path: Path, output_path: Path) -> None:
        self.release_event.wait(timeout=5)
        output_path.write_bytes(make_valid_docx())


class PageCapTests(unittest.TestCase):
    def test_pdf_over_page_cap_is_rejected_before_convert(self):
        converter = CountingValidConverter()
        pdf_bytes = make_valid_pdf(5)
        with self.assertRaises(PdfDocxReconstructionTooLargeError):
            reconstruct_pdf_to_docx(pdf_bytes, "big.pdf", converter=converter, max_pages=3)
        # Rejected on page count; the heavy convert never ran.
        self.assertEqual(converter.calls, 0)

    def test_pdf_within_page_cap_converts(self):
        converter = CountingValidConverter()
        pdf_bytes = make_valid_pdf(2)
        result = reconstruct_pdf_to_docx(pdf_bytes, "ok.pdf", converter=converter, max_pages=3)
        self.assertEqual(converter.calls, 1)
        self.assertTrue(result.data.startswith(b"PK"))

    def test_unparseable_pdf_fails_open_on_page_guard(self):
        # A non-PDF blob cannot be page-counted; the guard fails open so the
        # convert path (and its own validation) still runs.
        converter = CountingValidConverter()
        result = reconstruct_pdf_to_docx(
            b"%PDF-1.7\nnot-really\n%%EOF\n", "weird.pdf", converter=converter, max_pages=1
        )
        self.assertEqual(converter.calls, 1)
        self.assertTrue(result.data.startswith(b"PK"))


class SemaphoreBackpressureTests(unittest.TestCase):
    def test_busy_when_no_conversion_slot_is_available(self):
        semaphore = pdf_docx_reconstruction._PDF_DOCX_CONVERSION_SEMAPHORE
        acquired = [
            semaphore.acquire(blocking=False) for _ in range(MAX_CONCURRENT_PDF_DOCX_CONVERSIONS)
        ]
        original_wait = pdf_docx_reconstruction.PDF_DOCX_QUEUE_WAIT_SECONDS
        pdf_docx_reconstruction.PDF_DOCX_QUEUE_WAIT_SECONDS = 0.05
        try:
            self.assertTrue(all(acquired))
            converter = CountingValidConverter()
            start = time.monotonic()
            with self.assertRaises(PdfDocxReconstructionBusy):
                reconstruct_pdf_to_docx(make_valid_pdf(1), "busy.pdf", converter=converter)
            # Sheds load fast rather than blocking on a full slot.
            self.assertLess(time.monotonic() - start, 2.0)
            # The blocked caller never invoked the converter.
            self.assertEqual(converter.calls, 0)
        finally:
            pdf_docx_reconstruction.PDF_DOCX_QUEUE_WAIT_SECONDS = original_wait
            for ok in acquired:
                if ok:
                    semaphore.release()

    def test_slot_released_after_conversion_error(self):
        class BrokenConverter:
            name = "fake-broken"

            def is_available(self) -> bool:
                return True

            def convert_pdf_to_docx(self, source_path: Path, output_path: Path) -> None:
                output_path.write_bytes(b"not a docx")

        with self.assertRaises(PdfDocxReconstructionFailedError):
            reconstruct_pdf_to_docx(make_valid_pdf(1), "broken.pdf", converter=BrokenConverter())
        # All slots free again after the failure.
        semaphore = pdf_docx_reconstruction._PDF_DOCX_CONVERSION_SEMAPHORE
        grabbed = [
            semaphore.acquire(blocking=False) for _ in range(MAX_CONCURRENT_PDF_DOCX_CONVERSIONS)
        ]
        try:
            self.assertTrue(all(grabbed))
        finally:
            for ok in grabbed:
                if ok:
                    semaphore.release()


class TimeoutAndRlimitTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "process-group kill is POSIX-only")
    def test_timeout_kills_the_whole_process_group(self):
        marker_dir = Path(__import__("tempfile").mkdtemp())
        child_pid_file = marker_dir / "child.pid"
        script = (
            "import os, sys, time, subprocess\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
            f"open({str(child_pid_file)!r}, 'w').write(str(child.pid))\n"
            "time.sleep(30)\n"
        )
        start = time.monotonic()
        with self.assertRaises(PdfDocxReconstructionFailedError) as ctx:
            _run_pdf_docx_child(
                [sys.executable, "-c", script], cwd=str(marker_dir), timeout_seconds=1
            )
        self.assertIn("timed out", str(ctx.exception))
        self.assertLess(time.monotonic() - start, 10.0)
        deadline = time.monotonic() + 5.0
        child_pid = None
        while time.monotonic() < deadline:
            if child_pid_file.is_file():
                try:
                    child_pid = int(child_pid_file.read_text().strip())
                    break
                except ValueError:
                    pass
            time.sleep(0.05)
        self.assertIsNotNone(child_pid, "child PID was never recorded")
        child_alive = True
        for _ in range(100):
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                child_alive = False
                break
            time.sleep(0.05)
        if child_alive:
            try:
                os.kill(child_pid, 9)
            except ProcessLookupError:
                pass
        self.assertFalse(child_alive, "forked child survived the process-group kill")

    @unittest.skipUnless(os.name == "posix", "RLIMIT preexec is POSIX-only")
    def test_preexec_applies_address_space_rlimit_in_child(self):
        import resource as _resource

        original = pdf_docx_reconstruction.PDF_DOCX_MEMORY_LIMIT_BYTES
        pdf_docx_reconstruction.PDF_DOCX_MEMORY_LIMIT_BYTES = 512 * 1024 * 1024
        try:
            probe = "import resource,sys; print(resource.getrlimit(resource.RLIMIT_AS)[0])"
            returncode, stdout_bytes, _stderr = _run_pdf_docx_child(
                [sys.executable, "-c", probe], cwd=os.getcwd(), timeout_seconds=10
            )
        finally:
            pdf_docx_reconstruction.PDF_DOCX_MEMORY_LIMIT_BYTES = original
        self.assertEqual(returncode, 0)
        soft_limit = int(stdout_bytes.decode("utf-8").strip())
        if soft_limit == _resource.RLIM_INFINITY:
            self.skipTest("platform does not honor RLIMIT_AS (e.g. macOS)")
        self.assertLessEqual(soft_limit, 512 * 1024 * 1024)


class CacheTests(unittest.TestCase):
    def test_owner_keyed_cache_reuses_reconstruction(self):
        with __import__("tempfile").TemporaryDirectory() as cache_name:
            cache_dir = Path(cache_name)
            converter = CountingValidConverter()
            pdf_bytes = make_valid_pdf(1)
            first = reconstruct_pdf_to_docx(
                pdf_bytes, "doc.pdf", converter=converter, owner_user_id="user-a", cache_dir=cache_dir
            )
            second = reconstruct_pdf_to_docx(
                pdf_bytes, "doc.pdf", converter=converter, owner_user_id="user-a", cache_dir=cache_dir
            )
            # Converted exactly once; the repeat GET hit the cache.
            self.assertEqual(converter.calls, 1)
            self.assertEqual(first.data, second.data)

    def test_cache_partitions_by_owner(self):
        with __import__("tempfile").TemporaryDirectory() as cache_name:
            cache_dir = Path(cache_name)
            converter = CountingValidConverter()
            pdf_bytes = make_valid_pdf(1)
            reconstruct_pdf_to_docx(
                pdf_bytes, "doc.pdf", converter=converter, owner_user_id="user-a", cache_dir=cache_dir
            )
            reconstruct_pdf_to_docx(
                pdf_bytes, "doc.pdf", converter=converter, owner_user_id="user-b", cache_dir=cache_dir
            )
            # Different tenants never share a cache entry: each converted once.
            self.assertEqual(converter.calls, 2)

    def test_no_cache_when_owner_and_cache_dir_absent(self):
        converter = CountingValidConverter()
        pdf_bytes = make_valid_pdf(1)
        reconstruct_pdf_to_docx(pdf_bytes, "doc.pdf", converter=converter)
        reconstruct_pdf_to_docx(pdf_bytes, "doc.pdf", converter=converter)
        # Legacy callers (no owner/cache) keep their prior non-caching behavior.
        self.assertEqual(converter.calls, 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
