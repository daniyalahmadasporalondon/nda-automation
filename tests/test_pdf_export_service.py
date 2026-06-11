from pathlib import Path

from nda_automation import document_rendering, pdf_export_service


class AvailableConverter:
    name = "fake-available"

    def is_available(self):
        return True

    def convert_docx_to_pdf(self, source_path: Path, output_dir: Path, *, timeout_seconds: int):
        output = output_dir / "source.pdf"
        output.write_bytes(b"%PDF-1.7\nfake\n%%EOF\n")
        return output


class UnavailableConverter:
    name = "fake-unavailable"

    def is_available(self):
        return False

    def convert_docx_to_pdf(self, source_path: Path, output_dir: Path, *, timeout_seconds: int):
        raise AssertionError("unavailable converter should not be called")


def test_converter_health_reports_available_converter():
    assert pdf_export_service.converter_health(AvailableConverter()) == {
        "available": True,
        "converter": "fake-available",
        "message": "DOCX to PDF export is available.",
    }


def test_converter_health_reports_unavailable_converter():
    health = pdf_export_service.converter_health(UnavailableConverter())

    assert health["available"] is False
    assert health["converter"] == "fake-unavailable"
    assert "LibreOffice/soffice" in health["message"]


def test_pdf_download_filename_sanitizes_source_name():
    assert pdf_export_service.pdf_download_filename("RD Agreement - Real Transfer Ltd  P.S.S.docx") == (
        "RD-Agreement---Real-Transfer-Ltd--P-S-S.pdf"
    )
    assert pdf_export_service.pdf_download_filename("") == "document.pdf"


def test_public_matter_pdf_export_exposes_download_when_ready(tmp_path):
    pdf_path = tmp_path / "document.pdf"
    pdf_path.write_bytes(b"%PDF-1.7\n%%EOF\n")
    rendered = document_rendering.RenderedDocument(
        status=document_rendering.READY_STATUS,
        cache_key="render-key",
        source_sha256="source-sha",
        source_kind="docx",
        cache_dir=tmp_path,
        pdf_path=pdf_path,
    )

    payload = pdf_export_service.public_matter_pdf_export(
        "matter 1",
        rendered,
        matter={"source_filename": "Acme NDA.docx"},
    )

    assert payload["status"] == document_rendering.READY_STATUS
    assert payload["download_url"] == "/api/matters/matter%201/source-pdf"
    assert payload["filename"] == "Acme-NDA.pdf"
    assert payload["source_label"] == "Converted DOCX"
