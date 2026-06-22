"""Regenerate the tracked-changes .docx fixture used by the faithful-render test.

The fixture contains BOTH a tracked insertion (w:ins) and a tracked deletion
(w:del) so the headless docx-preview test (tests/frontend/docx-faithful-render.mjs)
can assert that the renderer emits <ins> and <del> DOM nodes when
options.renderChanges is true.

Run:    python3 tests/fixtures/make_tracked_changes_docx.py
Writes: tests/fixtures/tracked-changes-sample.docx

The sentinel strings below ("and shall remain confidential" inside <ins>,
"forever and ever" inside <del>) are exactly what the headless test asserts, so
keep them in sync if you change either side.

Hand-built OOXML (no python-docx dependency) so it stays reproducible and the
tracked-change markup is explicit and reviewable.
"""

from __future__ import annotations

import pathlib
import zipfile

HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE / "tracked-changes-sample.docx"

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>
"""

ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>
"""

# Paragraph 1: a normal run, a tracked INSERTION ("and shall remain confidential"),
# a tracked DELETION ("forever and ever"), then a closing run. Paragraph 2 carries
# no tracked changes (so the test also exercises a plain paragraph). The two
# sentinel phrases are what the headless test asserts appear inside <ins>/<del>.
DOCUMENT = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p>
      <w:r><w:t xml:space="preserve">This Agreement is made between the parties </w:t></w:r>
      <w:ins w:id="1" w:author="Counsel" w:date="2026-06-22T00:00:00Z">
        <w:r><w:t xml:space="preserve">and shall remain confidential </w:t></w:r>
      </w:ins>
      <w:del w:id="2" w:author="Counsel" w:date="2026-06-22T00:00:00Z">
        <w:r><w:delText xml:space="preserve">forever and ever</w:delText></w:r>
      </w:del>
      <w:r><w:t>.</w:t></w:r>
    </w:p>
    <w:p>
      <w:r><w:t>Second paragraph with no tracked changes.</w:t></w:r>
    </w:p>
  </w:body>
</w:document>
"""


def build() -> pathlib.Path:
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES)
        zf.writestr("_rels/.rels", ROOT_RELS)
        zf.writestr("word/document.xml", DOCUMENT)
    return OUT


if __name__ == "__main__":
    path = build()
    print(f"wrote {path} ({path.stat().st_size} bytes)")
