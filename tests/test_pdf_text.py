import unittest
from io import BytesIO

from pypdf import PdfWriter

from nda_automation.pdf_text import PdfExtractionError, extract_pdf_document, extract_pdf_paragraphs, extract_pdf_text


class PdfTextTests(unittest.TestCase):
    def test_extracts_text_from_pdf(self):
        data = make_pdf("This Agreement shall be governed by the laws of California.")

        extraction = extract_pdf_document(data)
        paragraphs = extraction.paragraphs

        self.assertEqual(len(paragraphs), 1)
        self.assertEqual(paragraphs[0]["source_part"], "pdf")
        self.assertEqual(paragraphs[0]["page_number"], 1)
        self.assertIn("California", paragraphs[0]["text"])
        self.assertEqual(extraction.quality["page_count"], 1)
        self.assertEqual(extraction.quality["pages_with_text"], 1)
        self.assertEqual(extraction.quality["pages_without_text"], 0)
        self.assertEqual(extraction.quality["extracted_paragraphs"], 1)
        self.assertIn("California", extract_pdf_text(data))

    def test_reconstructs_wrapped_clause_paragraphs(self):
        data = make_pdf_lines([
            "1. Definitions",
            "Confidential Information means non-public information",
            "and business plans disclosed by either party.",
            "2. Term",
            "The confidentiality obligations survive for five years.",
        ])

        paragraphs = extract_pdf_paragraphs(data)

        self.assertEqual(
            [paragraph["text"] for paragraph in paragraphs],
            [
                "1. Definitions Confidential Information means non-public information and business plans disclosed by either party.",
                "2. Term The confidentiality obligations survive for five years.",
            ],
        )

    def test_removes_repeated_pdf_headers_and_page_numbers(self):
        data = make_pdf_pages([
            [
                "Acme Mutual NDA",
                "1. Definitions",
                "Confidential Information means technical information.",
                "1",
            ],
            [
                "Acme Mutual NDA",
                "2. Term",
                "The obligations survive for three years.",
                "2",
            ],
        ])

        extraction = extract_pdf_document(data)
        extracted_text = "\n\n".join(paragraph["text"] for paragraph in extraction.paragraphs)

        self.assertNotIn("Acme Mutual NDA", extracted_text)
        self.assertNotIn("\n\n1\n\n", f"\n\n{extracted_text}\n\n")
        self.assertEqual(extraction.quality["repeated_margin_lines_removed"], 1)

    def test_quality_report_warns_when_some_pages_have_no_text(self):
        data = make_pdf_pages([
            ["This Agreement shall be governed by the laws of California."],
            [],
        ])

        extraction = extract_pdf_document(data)

        self.assertEqual(extraction.quality["page_count"], 2)
        self.assertEqual(extraction.quality["pages_without_text"], 1)
        warning_types = {warning["type"] for warning in extraction.quality["warnings"]}
        self.assertIn("pdf_pages_without_text", warning_types)

    def test_rejects_pdf_without_extractable_text(self):
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        with BytesIO() as output:
            writer.write(output)
            data = output.getvalue()

        with self.assertRaisesRegex(PdfExtractionError, "No readable text"):
            extract_pdf_paragraphs(data)

    def test_rejects_non_pdf_bytes(self):
        with self.assertRaisesRegex(PdfExtractionError, "not a valid PDF"):
            extract_pdf_paragraphs(b"not a pdf")


def make_pdf(text):
    return make_pdf_pages([[text]])


def make_pdf_lines(lines):
    return make_pdf_pages([lines])


def make_pdf_pages(pages):
    object_count = 3 + len(pages) * 2
    kids = " ".join(f"{3 + index * 2} 0 R" for index in range(len(pages)))
    objects = [
        f"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
        f"2 0 obj << /Type /Pages /Kids [{kids}] /Count {len(pages)} >> endobj\n",
    ]
    for index, lines in enumerate(pages):
        page_object_number = 3 + index * 2
        content_object_number = page_object_number + 1
        objects.append(
            f"{page_object_number} 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {object_count} 0 R >> >> /Contents {content_object_number} 0 R >> endobj\n"
        )
        stream = _pdf_text_stream(lines)
        objects.append(f"{content_object_number} 0 obj << /Length {len(stream.encode('latin-1'))} >> stream\n{stream}endstream endobj\n")
    objects.append(f"{object_count} 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n")
    return _pdf_package(objects)


def _pdf_text_stream(lines):
    if not lines:
        return ""
    operations = ["BT /F1 12 Tf 14 TL 72 720 Td"]
    for index, line in enumerate(lines):
        if index:
            operations.append("T*")
        operations.append(f"({_escape_pdf_text(line)}) Tj")
    operations.append("ET")
    return " ".join(operations) + "\n"


def _escape_pdf_text(text):
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return escaped


def _pdf_package(objects):
    with BytesIO() as output:
        output.write(b"%PDF-1.4\n")
        offsets = [0]
        for pdf_object in objects:
            offsets.append(output.tell())
            output.write(pdf_object.encode("latin-1"))
        xref_offset = output.tell()
        output.write(f"xref\n0 {len(objects) + 1}\n".encode("latin-1"))
        output.write(b"0000000000 65535 f \n")
        for offset in offsets[1:]:
            output.write(f"{offset:010d} 00000 n \n".encode("latin-1"))
        output.write(f"trailer << /Root 1 0 R /Size {len(objects) + 1} >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("latin-1"))
        return output.getvalue()
