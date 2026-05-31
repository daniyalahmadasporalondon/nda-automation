from __future__ import annotations

import re
from datetime import datetime, timezone
from io import BytesIO
from typing import Dict, Iterable, List, Tuple
from zipfile import ZIP_DEFLATED, ZipFile

from .redline_actions import (
    REDLINE_DELETE_PARAGRAPH,
    REDLINE_INSERT_AFTER_PARAGRAPH,
    REDLINE_REPLACE_PARAGRAPH,
)

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
INLINE_DIFF_CELL_LIMIT = 40000
INLINE_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*|[^\sA-Za-z0-9]")

ClauseResult = Dict[str, object]
Paragraph = Dict[str, object]
RedlineEdit = Dict[str, object]
ReviewResult = Dict[str, object]


def build_review_report_docx(review_result: ReviewResult, title: str = "NDA Review") -> bytes:
    checked_at = str(review_result.get("checked_at", ""))
    paragraphs = [
        _paragraph("NDA Redline", style="Title"),
        _paragraph(f"Matter: {title or 'Untitled NDA'}", style="Subtitle"),
        _paragraph(
            "The Redlined NDA section contains native Word tracked changes. Review Notes are explanatory only. "
            "Track Changes is also enabled for any edits made after opening the file.",
            style="Note",
        ),
        _paragraph("Redlined NDA", style="Heading1"),
        *_redlined_nda_section(review_result),
        _paragraph("Review Notes", style="Heading1"),
        _paragraph(f"Overall status: {_status_label(str(review_result.get('overall_status', '')))}"),
        _paragraph(f"Requirements passed: {review_result.get('requirements_passed', 0)}"),
        _paragraph(f"Requirements failed: {review_result.get('requirements_failed', 0)}"),
        _paragraph(f"Checked at: {checked_at}"),
        _paragraph("Clause Findings", style="Heading1"),
    ]

    redlines_by_clause = _redlines_by_clause(review_result.get("redline_edits", []))
    for clause in review_result.get("clauses", []):
        if not isinstance(clause, dict):
            continue
        paragraphs.extend(_clause_section(clause, redlines_by_clause.get(str(clause.get("id")), [])))

    document_xml = _document_xml("".join(paragraphs))

    with BytesIO() as output:
        with ZipFile(output, "w", ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", _content_types_xml())
            archive.writestr("_rels/.rels", _package_rels_xml())
            archive.writestr("docProps/core.xml", _core_properties_xml(title or "NDA Review"))
            archive.writestr("docProps/app.xml", _app_properties_xml())
            archive.writestr("word/document.xml", document_xml)
            archive.writestr("word/styles.xml", _styles_xml())
            archive.writestr("word/settings.xml", _settings_xml())
            archive.writestr("word/_rels/document.xml.rels", _document_rels_xml())
        return output.getvalue()


def _redlined_nda_section(review_result: ReviewResult) -> List[str]:
    paragraphs = review_result.get("paragraphs", [])
    if not isinstance(paragraphs, list) or not paragraphs:
        return [_paragraph("No source paragraphs available.")]

    redlines_by_paragraph = _redlines_by_paragraph(review_result.get("redline_edits", []))
    output: List[str] = []
    revision_id = 1

    for paragraph in paragraphs:
        if not isinstance(paragraph, dict):
            continue

        paragraph_id = str(paragraph.get("id", ""))
        paragraph_text = str(paragraph.get("text", ""))
        edits = redlines_by_paragraph.get(paragraph_id, [])
        primary_edit = next((edit for edit in edits if edit.get("action") != REDLINE_INSERT_AFTER_PARAGRAPH), None)

        if primary_edit and primary_edit.get("action") == REDLINE_REPLACE_PARAGRAPH:
            paragraph_xml, revision_id = _tracked_replace_paragraph(
                str(primary_edit.get("original_text") or paragraph_text),
                str(primary_edit.get("replacement_text") or ""),
                revision_id,
            )
            output.append(paragraph_xml)
        elif primary_edit and primary_edit.get("action") == REDLINE_DELETE_PARAGRAPH:
            output.append(_tracked_delete_paragraph(str(primary_edit.get("original_text") or paragraph_text), revision_id))
            revision_id += 1
        else:
            output.append(_paragraph(paragraph_text))

        for insertion in [edit for edit in edits if edit.get("action") == REDLINE_INSERT_AFTER_PARAGRAPH]:
            for insert_paragraph in _tracked_insert_paragraphs(str(insertion.get("insert_text") or insertion.get("replacement_text") or ""), revision_id):
                output.append(insert_paragraph)
                revision_id += 1

    return output


def _clause_section(clause: ClauseResult, redlines: List[RedlineEdit]) -> List[str]:
    status = "PASS" if clause.get("passes") else "CHECK"
    output = [
        _paragraph(f"{clause.get('name', 'Clause')} - {status}", style="Heading2"),
        _label_value("Requirement", clause.get("requirement")),
        _label_value("Issue type", clause.get("issue_label")),
        _label_value("Why", clause.get("reason") or clause.get("finding")),
        _label_value("What to fix", clause.get("what_to_fix")),
        _label_value("Exact paragraph", clause.get("matched_text") or "No matching paragraph identified."),
    ]

    if redlines:
        output.append(_paragraph("Proposed Redline", style="Heading3"))
        for redline in redlines:
            output.extend(_redline_section(redline))
    return output


def _redline_section(redline: RedlineEdit) -> List[str]:
    action = str(redline.get("action_label") or "Proposed edit")
    paragraph_id = str(redline.get("paragraph_id") or "")
    paragraph_index = str(redline.get("paragraph_index") or "")
    source_index = redline.get("source_index")
    target_parts = []
    if paragraph_id:
        target_parts.append(paragraph_id)
    if paragraph_index:
        target_parts.append(f"review paragraph {paragraph_index}")
    if source_index is not None:
        target_parts.append(f"source paragraph {source_index}")
    target = " / ".join(target_parts)
    output = [
        _label_value("Action", action),
        _label_value("Target", target or "No paragraph target identified."),
    ]

    if redline.get("action") == REDLINE_INSERT_AFTER_PARAGRAPH:
        output.append(_label_value("Anchor paragraph", redline.get("anchor_text")))
        output.append(_label_value("Insert text", redline.get("insert_text") or redline.get("replacement_text")))
    elif redline.get("action") == REDLINE_DELETE_PARAGRAPH:
        output.append(_label_value("Remove paragraph", redline.get("original_text")))
    else:
        output.append(_label_value("Original paragraph", redline.get("original_text")))
        output.append(_label_value("Replacement text", redline.get("replacement_text")))

    template_options = redline.get("template_options", [])
    if isinstance(template_options, list) and template_options:
        output.append(_paragraph("Template options", style="Heading3"))
        for option in template_options:
            if not isinstance(option, dict):
                continue
            default = " (default)" if option.get("selected") else ""
            label = f"{option.get('label', 'Option')}{default}"
            option_text = option.get("text") or option.get("replacement_text") or option.get("insert_text")
            output.append(_label_value(label, option_text))
    return output


def _redlines_by_clause(redlines: object) -> Dict[str, List[RedlineEdit]]:
    grouped: Dict[str, List[RedlineEdit]] = {}
    if not isinstance(redlines, list):
        return grouped

    for redline in redlines:
        if not isinstance(redline, dict):
            continue
        clause_id = str(redline.get("clause_id", ""))
        grouped.setdefault(clause_id, []).append(redline)
    return grouped


def _redlines_by_paragraph(redlines: object) -> Dict[str, List[RedlineEdit]]:
    grouped: Dict[str, List[RedlineEdit]] = {}
    if not isinstance(redlines, list):
        return grouped

    for redline in redlines:
        if not isinstance(redline, dict):
            continue
        paragraph_id = str(redline.get("paragraph_id", ""))
        grouped.setdefault(paragraph_id, []).append(redline)
    return grouped


def _label_value(label: str, value: object) -> str:
    text = str(value or "")
    return _paragraph(label, bold=True) + _paragraph(text or "None.")


def _paragraph(text: str, style: str | None = None, bold: bool = False) -> str:
    style_xml = f'<w:pPr><w:pStyle w:val="{_escape_attr(style)}"/></w:pPr>' if style else ""
    return f"<w:p>{style_xml}{_run(text, bold=bold)}</w:p>"


def _run(text: str, bold: bool = False) -> str:
    run_props = "<w:rPr><w:b/></w:rPr>" if bold else ""
    parts = []
    for index, line in enumerate(str(text).split("\n")):
        if index:
            parts.append("<w:br/>")
        parts.append(f'<w:t xml:space="preserve">{_escape_xml(line)}</w:t>')
    return f"<w:r>{run_props}{''.join(parts)}</w:r>"


def _tracked_delete_paragraph(text: str, revision_id: int) -> str:
    return f"<w:p>{_tracked_delete(text, revision_id)}</w:p>"


def _tracked_insert_paragraph(text: str, revision_id: int) -> str:
    return f"<w:p>{_tracked_insert(text, revision_id)}</w:p>"


def _tracked_replace_paragraph(original: str, replacement: str, first_revision_id: int) -> Tuple[str, int]:
    runs: List[str] = []
    revision_id = first_revision_id
    current_type = ""
    current_parts: List[str] = []
    previous_original_token = ""
    previous_accepted_token = ""

    def flush_current() -> None:
        nonlocal revision_id, current_type, current_parts
        if not current_parts:
            return
        text = "".join(current_parts)
        if current_type == "delete":
            runs.append(_tracked_delete(text, revision_id))
            revision_id += 1
        elif current_type == "insert":
            runs.append(_tracked_insert(text, revision_id))
            revision_id += 1
        else:
            runs.append(_run(text))
        current_parts = []

    for operation_type, token in _diff_text_operations(original, replacement):
        if operation_type != current_type:
            flush_current()
            current_type = operation_type
        if operation_type == "delete":
            prefix = " " if _needs_inline_space(previous_original_token, token) else ""
            previous_original_token = token
        elif operation_type == "insert":
            prefix = " " if _needs_inline_space(previous_accepted_token, token) else ""
            previous_accepted_token = token
        else:
            prefix = (
                " "
                if _needs_inline_space(previous_original_token, token)
                or _needs_inline_space(previous_accepted_token, token)
                else ""
            )
            previous_original_token = token
            previous_accepted_token = token
        current_parts.append(f"{prefix}{token}")

    flush_current()
    return f"<w:p>{''.join(runs)}</w:p>", revision_id


def _tracked_insert_paragraphs(text: str, first_revision_id: int) -> List[str]:
    blocks = [block for block in str(text).split("\n\n") if block.strip()]
    if not blocks:
        blocks = [str(text)]
    return [
        _tracked_insert_paragraph(block, first_revision_id + index)
        for index, block in enumerate(blocks)
    ]


def _tracked_delete(text: str, revision_id: int) -> str:
    return f'<w:del {_revision_attrs(revision_id)}>{_deleted_run(text)}</w:del>'


def _tracked_insert(text: str, revision_id: int) -> str:
    return f'<w:ins {_revision_attrs(revision_id)}>{_run(text)}</w:ins>'


def _deleted_run(text: str) -> str:
    parts = []
    for index, line in enumerate(str(text).split("\n")):
        if index:
            parts.append("<w:br/>")
        parts.append(f'<w:delText xml:space="preserve">{_escape_xml(line)}</w:delText>')
    return f"<w:r>{''.join(parts)}</w:r>"


def _diff_text_operations(original: str, replacement: str) -> List[Tuple[str, str]]:
    old_tokens = _tokenize_inline_diff(original)
    new_tokens = _tokenize_inline_diff(replacement)
    if not old_tokens:
        return [("insert", token) for token in new_tokens]
    if not new_tokens:
        return [("delete", token) for token in old_tokens]
    if len(old_tokens) * len(new_tokens) > INLINE_DIFF_CELL_LIMIT:
        return [("delete", token) for token in old_tokens] + [("insert", token) for token in new_tokens]
    return _diff_token_operations(old_tokens, new_tokens)


def _tokenize_inline_diff(text: str) -> List[str]:
    return INLINE_TOKEN_PATTERN.findall(str(text or ""))


def _diff_token_operations(old_tokens: List[str], new_tokens: List[str]) -> List[Tuple[str, str]]:
    row_count = len(old_tokens) + 1
    column_count = len(new_tokens) + 1
    dp = [[0] * column_count for _ in range(row_count)]

    for old_index in range(len(old_tokens) - 1, -1, -1):
        for new_index in range(len(new_tokens) - 1, -1, -1):
            if old_tokens[old_index] == new_tokens[new_index]:
                dp[old_index][new_index] = dp[old_index + 1][new_index + 1] + 1
            else:
                dp[old_index][new_index] = max(dp[old_index + 1][new_index], dp[old_index][new_index + 1])

    operations: List[Tuple[str, str]] = []
    old_index = 0
    new_index = 0
    while old_index < len(old_tokens) and new_index < len(new_tokens):
        if old_tokens[old_index] == new_tokens[new_index]:
            operations.append(("same", old_tokens[old_index]))
            old_index += 1
            new_index += 1
        elif dp[old_index + 1][new_index] >= dp[old_index][new_index + 1]:
            operations.append(("delete", old_tokens[old_index]))
            old_index += 1
        else:
            operations.append(("insert", new_tokens[new_index]))
            new_index += 1

    while old_index < len(old_tokens):
        operations.append(("delete", old_tokens[old_index]))
        old_index += 1
    while new_index < len(new_tokens):
        operations.append(("insert", new_tokens[new_index]))
        new_index += 1
    return operations


def _needs_inline_space(previous_token: str, token: str) -> bool:
    if not previous_token:
        return False
    if re.match(r"^[,.;:!?%)]$", token):
        return False
    if re.match(r"^[(]$", previous_token):
        return False
    return True


def _revision_attrs(revision_id: int) -> str:
    timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return f'w:id="{revision_id}" w:author="nda-automation" w:date="{timestamp}"'


def _status_label(status: str) -> str:
    if status == "meets_requirements":
        return "Meets requirements"
    if status == "does_not_meet_requirements":
        return "Does not meet requirements"
    return status or "Unknown"


def _document_xml(body_xml: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{W_NS}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <w:body>
    {body_xml}
    <w:sectPr>
      <w:pgSz w:w="12240" w:h="15840"/>
      <w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" w:header="720" w:footer="720" w:gutter="0"/>
    </w:sectPr>
  </w:body>
</w:document>"""


def _settings_xml() -> str:
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:settings xmlns:w="{W_NS}">
  <w:revisionView w:markup="1" w:comments="1" w:insDel="1" w:formatting="1" w:inkAnnotations="1"/>
  <w:trackRevisions/>
</w:settings>"""


def _styles_xml() -> str:
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="{W_NS}">
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal">
    <w:name w:val="Normal"/>
    <w:qFormat/>
    <w:pPr><w:spacing w:after="140" w:line="276" w:lineRule="auto"/></w:pPr>
    <w:rPr><w:rFonts w:ascii="Aptos" w:hAnsi="Aptos"/><w:sz w:val="22"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Title">
    <w:name w:val="Title"/>
    <w:basedOn w:val="Normal"/>
    <w:qFormat/>
    <w:pPr><w:spacing w:after="120"/></w:pPr>
    <w:rPr><w:b/><w:rFonts w:ascii="Aptos Display" w:hAnsi="Aptos Display"/><w:sz w:val="40"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Subtitle">
    <w:name w:val="Subtitle"/>
    <w:basedOn w:val="Normal"/>
    <w:qFormat/>
    <w:pPr><w:spacing w:after="220"/></w:pPr>
    <w:rPr><w:color w:val="44546A"/><w:sz w:val="24"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading1">
    <w:name w:val="heading 1"/>
    <w:basedOn w:val="Normal"/>
    <w:qFormat/>
    <w:pPr><w:spacing w:before="300" w:after="120"/></w:pPr>
    <w:rPr><w:b/><w:sz w:val="30"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading2">
    <w:name w:val="heading 2"/>
    <w:basedOn w:val="Normal"/>
    <w:qFormat/>
    <w:pPr><w:spacing w:before="240" w:after="100"/></w:pPr>
    <w:rPr><w:b/><w:sz w:val="26"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading3">
    <w:name w:val="heading 3"/>
    <w:basedOn w:val="Normal"/>
    <w:qFormat/>
    <w:pPr><w:spacing w:before="160" w:after="80"/></w:pPr>
    <w:rPr><w:b/><w:color w:val="5523B2"/><w:sz w:val="23"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Note">
    <w:name w:val="Note"/>
    <w:basedOn w:val="Normal"/>
    <w:pPr><w:spacing w:after="220"/></w:pPr>
    <w:rPr><w:i/><w:color w:val="44546A"/></w:rPr>
  </w:style>
</w:styles>"""


def _content_types_xml() -> str:
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="{DOCX_MIME}.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
  <Override PartName="/word/settings.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>"""


def _package_rels_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>"""


def _document_rels_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>"""


def _core_properties_xml(title: str) -> str:
    created = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
  xmlns:dc="http://purl.org/dc/elements/1.1/"
  xmlns:dcterms="http://purl.org/dc/terms/"
  xmlns:dcmitype="http://purl.org/dc/dcmitype/"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>{_escape_xml(title)} redline report</dc:title>
  <dc:creator>nda-automation</dc:creator>
  <cp:lastModifiedBy>nda-automation</cp:lastModifiedBy>
  <dcterms:created xsi:type="dcterms:W3CDTF">{created}</dcterms:created>
  <dcterms:modified xsi:type="dcterms:W3CDTF">{created}</dcterms:modified>
</cp:coreProperties>"""


def _app_properties_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
  xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>nda-automation</Application>
</Properties>"""


def _escape_xml(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _escape_attr(value: str) -> str:
    return _escape_xml(value)
