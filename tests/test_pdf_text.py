import unittest
from io import BytesIO

from pypdf import PdfWriter

from nda_automation.pdf_text import PdfExtractionError, extract_pdf_paragraphs, extract_pdf_text


class PdfTextTests(unittest.TestCase):
    def test_extracts_text_from_pdf(self):
        data = make_pdf("This Agreement shall be governed by the laws of California.")

        paragraphs = extract_pdf_paragraphs(data)

        self.assertEqual(len(paragraphs), 1)
        self.assertEqual(paragraphs[0]["source_part"], "pdf")
        self.assertEqual(paragraphs[0]["page_number"], 1)
        self.assertIn("California", paragraphs[0]["text"])
        self.assertIn("California", extract_pdf_text(data))

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
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    objects = [
        "1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
        "2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n",
        "3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >> endobj\n",
        "4 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n",
    ]
    stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET\n"
    objects.append(f"5 0 obj << /Length {len(stream.encode('latin-1'))} >> stream\n{stream}endstream endobj\n")
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
