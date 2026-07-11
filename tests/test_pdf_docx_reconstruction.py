from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest
from docx import Document

from nda_automation import pdf_docx_reconstruction, redline_export_service
from nda_automation.redline_actions import REDLINE_REPLACE_PARAGRAPH


class AvailablePdfDocxConverter:
    name = "fake-pdf2docx"

    def is_available(self):
        return True

    def convert_pdf_to_docx(self, source_path: Path, output_path: Path) -> None:
        assert source_path.read_bytes().startswith(b"%PDF-")
        output_path.write_bytes(make_valid_docx())


class UnavailablePdfDocxConverter:
    name = "fake-unavailable"

    def is_available(self):
        return False

    def convert_pdf_to_docx(self, source_path: Path, output_path: Path) -> None:
        raise AssertionError("unavailable converter should not be invoked")


class BrokenPdfDocxConverter:
    name = "fake-broken"

    def is_available(self):
        return True

    def convert_pdf_to_docx(self, source_path: Path, output_path: Path) -> None:
        output_path.write_bytes(b"not a docx")


class TextPdfDocxConverter:
    name = "fake-pdf2docx"

    def __init__(self, text: str):
        self.text = text

    def is_available(self):
        return True

    def convert_pdf_to_docx(self, source_path: Path, output_path: Path) -> None:
        assert source_path.read_bytes().startswith(b"%PDF-")
        output_path.write_bytes(make_valid_docx(self.text))


class PdfMatterRepository:
    def __init__(self, matter: dict, source_bytes: bytes):
        self.matter = matter
        self.source_bytes = source_bytes

    def get_matter(self, matter_id: str, *, owner_user_id: str = ""):
        if matter_id == self.matter["id"]:
            return self.matter
        return None

    def get_source_document_bytes(self, matter: dict) -> bytes | None:
        return self.source_bytes


def test_converter_health_reports_available_converter():
    health = pdf_docx_reconstruction.converter_health(AvailablePdfDocxConverter())

    assert health["available"] is True
    assert health["converter"] == "fake-pdf2docx"
    assert "available" in health["message"]
    assert health["mode"] == "pdf_to_docx_reconstruction"
    assert health["fidelity"] == {
        "source": "pdf",
        "output": "docx",
        "mode": "best_effort_pdf_to_docx_reconstruction",
        "visual_fidelity": "best_effort",
        "faithful_visual_source": "original_pdf_page_preview",
        "message": pdf_docx_reconstruction.PDF_DOCX_RECONSTRUCTION_FIDELITY_MESSAGE,
    }


def test_converter_health_reports_unavailable_converter():
    health = pdf_docx_reconstruction.converter_health(UnavailablePdfDocxConverter())

    assert health["available"] is False
    assert health["converter"] == "fake-unavailable"
    assert health["mode"] == "pdf_to_docx_reconstruction"
    assert health["fidelity"]["visual_fidelity"] == "best_effort"
    assert "pdf2docx" in health["message"]


def test_reconstruct_pdf_to_docx_uses_converter_and_validates_output():
    reconstructed = pdf_docx_reconstruction.reconstruct_pdf_to_docx(
        b"%PDF-1.7\nsource\n%%EOF\n",
        "RD Agreement - Real Transfer Ltd  P.S.S.pdf",
        converter=AvailablePdfDocxConverter(),
    )

    assert reconstructed.data.startswith(b"PK")
    assert reconstructed.filename == "RD-Agreement---Real-Transfer-Ltd--P-S-S.docx"
    assert reconstructed.content_type == pdf_docx_reconstruction.DOCX_CONTENT_TYPE
    assert reconstructed.headers == {
        "X-PDF-DOCX-Reconstruction": pdf_docx_reconstruction.PDF_DOCX_RECONSTRUCTION_HEADER,
        "X-PDF-DOCX-Converter": "fake-pdf2docx",
    }


def test_reconstruct_pdf_to_docx_reports_unavailable_converter():
    try:
        pdf_docx_reconstruction.reconstruct_pdf_to_docx(
            b"%PDF-1.7\nsource\n%%EOF\n",
            "source.pdf",
            converter=UnavailablePdfDocxConverter(),
        )
    except pdf_docx_reconstruction.PdfDocxReconstructionUnavailableError as error:
        assert "pdf2docx" in str(error)
    else:
        raise AssertionError("expected unavailable converter error")


def test_reconstruct_pdf_to_docx_rejects_invalid_output():
    try:
        pdf_docx_reconstruction.reconstruct_pdf_to_docx(
            b"%PDF-1.7\nsource\n%%EOF\n",
            "source.pdf",
            converter=BrokenPdfDocxConverter(),
        )
    except pdf_docx_reconstruction.PdfDocxReconstructionFailedError as error:
        assert "valid Word document" in str(error)
    else:
        raise AssertionError("expected failed reconstruction error")


def test_pdf_source_reviewed_docx_reconstructs_pdf_and_applies_tracked_redline(monkeypatch):
    source_text = "This Agreement shall be governed by the laws of California."
    replacement_text = "This Agreement shall be governed by the laws of England and Wales."
    review_result = {
        "paragraphs": [{"id": "p1", "index": 1, "source_index": 1, "text": source_text}],
        "clauses": [],
        "redline_edits": [
            {
                "id": "r1",
                "action": REDLINE_REPLACE_PARAGRAPH,
                "paragraph_id": "p1",
                "source_index": 1,
                "original_text": source_text,
                "replacement_text": replacement_text,
            }
        ],
        "extracted_text": source_text,
    }
    matter = {
        "id": "matter_pdf",
        "source_filename": "source.pdf",
        "review_result": review_result,
        "extracted_text": source_text,
    }
    repository = PdfMatterRepository(matter, b"%PDF-1.7\nsource\n%%EOF\n")

    monkeypatch.setattr(
        redline_export_service.pdf_docx_reconstruction,
        "Pdf2DocxConverter",
        lambda: TextPdfDocxConverter(source_text),
    )
    monkeypatch.setattr(redline_export_service, "review_result_staleness", lambda _review_result: {"stale": False})

    export = redline_export_service.build_matter_redline(
        "matter_pdf",
        repository=repository,
    )

    assert export.filename == "source-reviewed.docx"
    assert export.headers == {
        "X-PDF-DOCX-Reconstruction": pdf_docx_reconstruction.PDF_DOCX_RECONSTRUCTION_HEADER,
        "X-PDF-DOCX-Converter": "fake-pdf2docx",
    }
    with ZipFile(BytesIO(export.data)) as archive:
        document_xml = archive.read("word/document.xml").decode("utf-8")
    assert "<w:del " in document_xml
    assert "<w:ins " in document_xml
    assert "California" in document_xml
    assert "England" in document_xml


def make_valid_docx(text: str = "Reconstructed PDF content") -> bytes:
    document = Document()
    document.add_paragraph(text)
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# Child stderr sanitization (no traceback / tmp paths / log noise leaks out)
# --------------------------------------------------------------------------- #
_RAW_CHILD_STDERR = (
    "[INFO] Start to convert /tmp/nda-pdf-docx-abc123/source.pdf\n"
    "[WARNING] Ignore invalid image\n"
    "Traceback (most recent call last):\n"
    '  File "/private/tmp/nda-pdf-docx-abc123/child.py", line 7, in <module>\n'
    "    converter.convert(output_path, start=0, end=None)\n"
    '  File "/usr/lib/python3.11/site-packages/pdf2docx/converter.py", line 120, in convert\n'
    "    raise ValueError(reason)\n"
    "ValueError: cannot parse page geometry in /tmp/nda-pdf-docx-abc123/source.pdf\n"
)


def test_sanitize_reconstruction_stderr_keeps_only_a_clean_exception_summary():
    detail = pdf_docx_reconstruction._sanitize_reconstruction_stderr(_RAW_CHILD_STDERR)
    # The exception summary survives, with the filesystem path redacted.
    assert detail == "ValueError: cannot parse page geometry in <path>"
    # None of the noise leaks.
    assert "Traceback" not in detail
    assert "[INFO]" not in detail
    assert "[WARNING]" not in detail
    assert "/tmp/" not in detail
    assert "child.py" not in detail
    assert "site-packages" not in detail


def test_sanitize_reconstruction_stderr_returns_empty_for_pure_log_noise():
    raw = "[INFO] Start\n[INFO] Parsing page 1\n[WARNING] nothing useful here\n"
    assert pdf_docx_reconstruction._sanitize_reconstruction_stderr(raw) == ""
    assert pdf_docx_reconstruction._sanitize_reconstruction_stderr("") == ""


def test_convert_surfaces_sanitized_detail_not_raw_stderr(monkeypatch):
    converter = pdf_docx_reconstruction.Pdf2DocxConverter()
    # pdf2docx is not installed in CI; force availability so we reach the run path.
    monkeypatch.setattr(converter, "is_available", lambda: True)
    monkeypatch.setattr(
        pdf_docx_reconstruction,
        "_run_pdf_docx_child",
        lambda *args, **kwargs: (1, b"", _RAW_CHILD_STDERR.encode("utf-8")),
    )

    with pytest.raises(pdf_docx_reconstruction.PdfDocxReconstructionFailedError) as caught:
        converter.convert_pdf_to_docx(Path("/tmp/source.pdf"), Path("/tmp/out.docx"))

    message = str(caught.value)
    assert "ValueError: cannot parse page geometry in <path>" in message
    # The raw traceback / tmp paths / log lines never reach the surfaced error.
    assert "Traceback" not in message
    assert "child.py" not in message
    assert "/tmp/nda-pdf-docx-abc123" not in message
    assert "[INFO]" not in message


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
