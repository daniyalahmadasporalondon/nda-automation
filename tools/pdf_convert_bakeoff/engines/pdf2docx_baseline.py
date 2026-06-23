"""Baseline engine: the in-repo pdf2docx reconstruction (NO external creds).

Reuses the SHIPPED conversion so the bake-off scores the real production baseline,
not a re-implementation:

  - ``nda_automation.pdf_docx_reconstruction.reconstruct_pdf_to_docx`` performs the
    raw pdf2docx PDF -> DOCX reconstruction (the bytes the app stores / exports).

We deliberately call ``reconstruct_pdf_to_docx`` (not
``convert_pdf_matter_to_docx``) because the latter also maps pypdf review paragraphs
onto the reconstruction and needs a ``MatterSource``; for a pure "PDF in, DOCX out"
bake-off the reconstruction bytes are exactly the artifact every other engine
produces. The downstream scoring stage then runs the SAME review path over every
engine's DOCX, so the comparison stays apples-to-apples.
"""

from __future__ import annotations

from pathlib import Path

NAME = "pdf2docx"
REQUIRED_ENV: tuple[str, ...] = ()  # baseline needs no external keys


def available() -> tuple[bool, str]:
    try:
        import importlib

        importlib.import_module("nda_automation.pdf_docx_reconstruction")
    except Exception as exc:  # pragma: no cover - environment guard
        return False, f"in-repo conversion unimportable ({exc})"
    return True, ""


def convert(pdf_path: Path, out_path: Path) -> None:
    """Reconstruct ``pdf_path`` to a DOCX written at ``out_path``.

    Raises whatever ``reconstruct_pdf_to_docx`` raises (its own
    ``PdfDocxReconstruction*`` subclasses); the runner records the failure
    per-(doc, engine) and continues.
    """
    from nda_automation import pdf_docx_reconstruction

    pdf_bytes = pdf_path.read_bytes()
    reconstructed = pdf_docx_reconstruction.reconstruct_pdf_to_docx(pdf_bytes, pdf_path.name)
    out_path.write_bytes(reconstructed.data)
