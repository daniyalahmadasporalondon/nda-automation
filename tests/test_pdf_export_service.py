from pathlib import Path

from tests.test_pdf_docx_reconstruction import make_valid_docx

from nda_automation import document_rendering, pdf_docx_reconstruction, pdf_export_service


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


class AvailablePdfDocxConverter:
    name = "fake-pdf2docx"

    def is_available(self):
        return True

    def convert_pdf_to_docx(self, source_path: Path, output_path: Path):
        assert source_path.read_bytes().startswith(b"%PDF-")
        output_path.write_bytes(make_valid_docx())


class UnavailablePdfDocxConverter:
    name = "fake-unavailable-pdf2docx"

    def is_available(self):
        return False

    def convert_pdf_to_docx(self, source_path: Path, output_path: Path):
        raise AssertionError("unavailable PDF-to-DOCX converter should not be called")


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


def test_public_matter_document_downloads_exposes_source_and_reviewed_format_choices():
    downloads = pdf_export_service.public_matter_document_downloads(
        {
            "id": "matter 1",
            "source_filename": "Acme NDA.docx",
            "source_type": "generated",
            "status": "approved",
        },
        converter=AvailableConverter(),
    )

    source = downloads["source"]["formats"]
    assert downloads["source"]["label"] == "Generated document"
    assert source["docx"] == {
        "format": "docx",
        "available": True,
        "filename": "Acme NDA.docx",
        "content_type": document_rendering.DOCX_CONTENT_TYPE,
        "download_url": "/api/matters/matter%201/source",
    }
    assert source["pdf"]["available"] is True
    assert source["pdf"]["download_url"] == "/api/matters/matter%201/source-pdf"
    assert source["pdf"]["filename"] == "Acme-NDA.pdf"
    assert source["pdf"]["converter"]["available"] is True

    reviewed = downloads["reviewed"]["formats"]
    assert reviewed["docx"]["available"] is True
    assert reviewed["docx"]["download_url"] == "/api/matters/matter%201/reviewed-docx"
    assert reviewed["docx"]["filename"] == "Acme-NDA-redlined.docx"
    assert reviewed["pdf"]["available"] is True
    assert reviewed["pdf"]["download_url"] == "/api/matters/matter%201/reviewed-pdf"
    assert reviewed["pdf"]["filename"] == "Acme-NDA-redlined.pdf"


def test_public_matter_document_downloads_disables_converted_pdf_when_converter_unavailable():
    downloads = pdf_export_service.public_matter_document_downloads(
        {
            "id": "matter-2",
            "source_filename": "Counterparty NDA.docx",
            "status": "approved",
        },
        converter=UnavailableConverter(),
    )

    source_pdf = downloads["source"]["formats"]["pdf"]
    reviewed_pdf = downloads["reviewed"]["formats"]["pdf"]
    assert source_pdf["available"] is False
    assert "download_url" not in source_pdf
    assert "LibreOffice/soffice" in source_pdf["unavailable_reason"]
    assert source_pdf["converter"]["available"] is False
    assert reviewed_pdf["available"] is False
    assert "download_url" not in reviewed_pdf
    assert "LibreOffice/soffice" in reviewed_pdf["unavailable_reason"]


def test_public_matter_document_downloads_preserves_original_pdf_and_blocks_fake_docx():
    downloads = pdf_export_service.public_matter_document_downloads(
        {
            "id": "matter-3",
            "source_filename": "Signed NDA.pdf",
            "status": "approved",
        },
        converter=UnavailableConverter(),
        pdf_docx_converter=UnavailablePdfDocxConverter(),
    )

    source = downloads["source"]["formats"]
    assert source["pdf"]["available"] is True
    assert source["pdf"]["download_url"] == "/api/matters/matter-3/source-pdf"
    assert source["pdf"]["filename"] == "Signed-NDA.pdf"
    assert source["docx"]["available"] is False
    assert "pdf2docx" in source["docx"]["unavailable_reason"]

    reviewed = downloads["reviewed"]["formats"]
    assert reviewed["docx"]["available"] is False
    assert reviewed["pdf"]["available"] is False
    assert "pdf2docx" in reviewed["docx"]["unavailable_reason"]
    assert "pdf2docx" in reviewed["pdf"]["unavailable_reason"]
    assert "download_url" not in reviewed["docx"]
    assert "download_url" not in reviewed["pdf"]


def test_public_matter_document_downloads_exposes_reconstructed_pdf_docx_when_available():
    downloads = pdf_export_service.public_matter_document_downloads(
        {
            "id": "matter-5",
            "source_filename": "Signed NDA.pdf",
            "status": "approved",
        },
        converter=AvailableConverter(),
        pdf_docx_converter=AvailablePdfDocxConverter(),
    )

    source = downloads["source"]["formats"]
    assert source["pdf"]["available"] is True
    assert source["pdf"]["download_url"] == "/api/matters/matter-5/source-pdf"
    assert source["docx"]["available"] is True
    assert source["docx"]["download_url"] == "/api/matters/matter-5/source-docx"
    assert source["docx"]["filename"] == "Signed-NDA.docx"
    assert source["docx"]["label"] == "Reconstructed Word"
    assert source["docx"]["source_transform"] == "pdf_to_reconstructed_docx"
    assert source["docx"]["fidelity"] == {
        "source": "pdf",
        "output": "docx",
        "mode": "best_effort_pdf_to_docx_reconstruction",
        "visual_fidelity": "best_effort",
        "faithful_visual_source": "original_pdf_page_preview",
        "message": pdf_docx_reconstruction.PDF_DOCX_RECONSTRUCTION_FIDELITY_MESSAGE,
    }
    assert source["docx"]["converter"]["converter"] == "fake-pdf2docx"
    assert source["docx"]["converter"]["fidelity"]["visual_fidelity"] == "best_effort"

    reviewed = downloads["reviewed"]["formats"]
    assert reviewed["docx"]["available"] is True
    assert reviewed["docx"]["download_url"] == "/api/matters/matter-5/reviewed-docx"
    assert reviewed["docx"]["filename"] == "Signed-NDA-reviewed.docx"
    assert reviewed["docx"]["label"] == "Reconstructed reviewed Word"
    assert reviewed["docx"]["source_transform"] == "pdf_to_reconstructed_reviewed_docx"
    assert reviewed["docx"]["fidelity"]["output"] == "reviewed_docx"
    assert reviewed["pdf"]["available"] is True
    assert reviewed["pdf"]["download_url"] == "/api/matters/matter-5/reviewed-pdf"
    assert reviewed["pdf"]["label"] == "PDF from reconstructed Word"
    assert reviewed["pdf"]["source_transform"] == "pdf_to_reconstructed_docx_to_pdf"
    assert reviewed["pdf"]["fidelity"]["output"] == "reviewed_pdf"
    assert reviewed["pdf"]["converter"]["pdf_to_docx"]["converter"] == "fake-pdf2docx"
    assert reviewed["pdf"]["converter"]["docx_to_pdf"]["converter"] == "fake-available"


def test_public_matter_document_downloads_does_not_expose_internal_stored_filename():
    downloads = pdf_export_service.public_matter_document_downloads(
        {
            "id": "matter-4",
            "stored_filename": "internal-secret-name.docx",
            "status": "approved",
        },
        converter=AvailableConverter(),
    )

    flattened = str(downloads)
    assert "internal-secret-name" not in flattened
    assert downloads["source"]["formats"]["docx"]["available"] is False
    assert downloads["source"]["formats"]["pdf"]["filename"] == "document.pdf"
    assert downloads["reviewed"]["formats"]["docx"]["filename"] == "document-redlined.docx"


def test_build_docx_pdf_export_converts_docx_with_available_converter():
    export = pdf_export_service.build_docx_pdf_export(
        b"PK\x03\x04fake-docx",
        "mutual-nda-redlined.docx",
        converter=AvailableConverter(),
    )

    assert export.path.read_bytes().startswith(b"%PDF-1.7")
    assert export.filename == "mutual-nda-redlined.pdf"
    assert export.content_type == pdf_export_service.PDF_EXPORT_MIME
    assert export.headers == {
        "X-PDF-Export-Verified": pdf_export_service.PDF_EXPORT_VERIFICATION_HEADER,
        "X-PDF-Export-Source-Kind": "docx",
    }


def test_build_docx_pdf_export_reports_unavailable_converter():
    try:
        pdf_export_service.build_docx_pdf_export(
            b"PK\x03\x04unavailable-fake-docx",
            "mutual-nda-redlined.docx",
            converter=UnavailableConverter(),
        )
    except pdf_export_service.PdfExportError as error:
        assert error.status == 503
        assert "LibreOffice/soffice" in error.payload["error"]
        assert error.payload["document_pdf_export"]["error_code"] == "converter_unavailable"
        assert error.payload["document_pdf_export"]["filename"] == "mutual-nda-redlined.pdf"
    else:
        raise AssertionError("expected converter-unavailable PDF export error")


# --------------------------------------------------------------------------- #
# D5: a matter's reviewed exports must stay downloadable once it is approved AND
# after it is executed / fully_signed. Approval mints the reviewed artifact;
# executing is a strictly later, approval-presupposing state, so a SIGNED matter
# must not lose its reviewed redline. Never widen access to an unreviewed matter.
# --------------------------------------------------------------------------- #
def test_matter_reviewed_export_ready_covers_approved_and_executed_states():
    ready = pdf_export_service.matter_reviewed_export_ready
    # Approved (canonical sign-off) -- ready.
    assert ready({"status": "approved"}) is True
    # Approved via timestamp only (no status string) -- ready.
    assert ready({"approved_at": "2026-07-11T00:00:00+00:00"}) is True
    # Executed / fully signed (strictly later than approval) -- STILL ready (D5).
    assert ready({"status": "fully_signed"}) is True
    assert ready({"executed": True}) is True
    assert ready({"executed_at": "2026-07-11T00:00:00+00:00"}) is True
    # Reviewed-but-unapproved / in-flight -- NOT ready (no widening to unreviewed).
    assert ready({"status": "awaiting_human", "review_result": {"clauses": []}}) is False
    assert ready({"status": "active"}) is False
    assert ready({}) is False


def test_public_matter_document_downloads_available_for_executed_matter():
    # A DOCX-source matter that has been EXECUTED (fully_signed) -- NOT status
    # "approved" -- must still offer its reviewed DOCX/PDF downloads (D5).
    downloads = pdf_export_service.public_matter_document_downloads(
        {
            "id": "matter-exec",
            "source_filename": "Acme NDA.docx",
            "status": "fully_signed",
            "executed": True,
        },
        converter=AvailableConverter(),
    )

    reviewed = downloads["reviewed"]["formats"]
    assert reviewed["docx"]["available"] is True
    assert reviewed["docx"]["download_url"] == "/api/matters/matter-exec/reviewed-docx"
    assert reviewed["docx"]["filename"] == "Acme-NDA-redlined.docx"
    assert reviewed["pdf"]["available"] is True
    assert reviewed["pdf"]["download_url"] == "/api/matters/matter-exec/reviewed-pdf"
    # No false "available after approved" copy for an already-executed matter.
    assert "unavailable_reason" not in reviewed["docx"]
    assert "unavailable_reason" not in reviewed["pdf"]


def test_public_matter_document_downloads_blocks_reviewed_before_approval():
    # A reviewed-but-unapproved matter still hides reviewed exports and shows the
    # honest pre-approval copy -- the gate is only extended PAST approval, not before.
    downloads = pdf_export_service.public_matter_document_downloads(
        {
            "id": "matter-pending",
            "source_filename": "Acme NDA.docx",
            "status": "awaiting_human",
            "review_result": {"clauses": []},
        },
        converter=AvailableConverter(),
    )

    reviewed = downloads["reviewed"]["formats"]
    assert reviewed["docx"]["available"] is False
    assert reviewed["pdf"]["available"] is False
    assert "approved" in reviewed["docx"]["unavailable_reason"]
    assert "approved" in reviewed["pdf"]["unavailable_reason"]


def test_build_matter_pdf_source_docx_export_reconstructs_pdf_source():
    class Repository:
        def get_matter(self, matter_id, *, owner_user_id=""):
            return {
                "id": matter_id,
                "source_filename": "Signed NDA.pdf",
            }

        def get_source_document_bytes(self, matter):
            return b"%PDF-1.7\nsource pdf\n%%EOF\n"

    export = pdf_export_service.build_matter_pdf_source_docx_export(
        "matter-6",
        repository=Repository(),
        converter=AvailablePdfDocxConverter(),
    )

    assert export.data.startswith(b"PK")
    assert export.filename == "Signed-NDA.docx"
    assert export.content_type == pdf_docx_reconstruction.DOCX_CONTENT_TYPE
    assert export.headers["X-PDF-DOCX-Reconstruction"] == "pdf2docx"
    assert export.headers["X-PDF-DOCX-Converter"] == "fake-pdf2docx"
