"""One canonical body-paragraph numbering for DOCX, shared by extraction and export.

The redline pipeline has two index spaces that MUST agree:

* ``docx_text._extract_main_document_paragraphs`` mints the ``source_index`` carried
  on each review paragraph (and therefore on each redline).
* ``docx_export._indexed_source_paragraphs`` numbers the physical ``<w:p>`` the
  export anchors a redline into.

Historically these were two independent reimplementations of "number every body
``<w:p>`` in document order". They agreed only by coincidence of parallel logic, so
a future edit to one walker (table flattening, drawing-paragraph handling, an
empty-paragraph filter) could silently desynchronise them -- and a redline on a
DUPLICATE clause would then anchor to the WRONG twin while the coverage gate still
passed.

The single source of truth is :func:`nda_automation.docx_text.iter_indexed_body_paragraphs`.
This module re-exports it under a stable name so the export side can depend on it
without importing the rest of ``docx_text``'s extraction surface, and so the shared
contract has one obvious home in the codebase.
"""
from __future__ import annotations

from .docx_text import IndexedBodyParagraph, iter_indexed_body_paragraphs

# Stable public aliases for the export/anchoring side.
IndexedParagraph = IndexedBodyParagraph
iter_body_paragraphs = iter_indexed_body_paragraphs

__all__ = ["IndexedParagraph", "IndexedBodyParagraph", "iter_body_paragraphs", "iter_indexed_body_paragraphs"]
