# nda-automation

A focused NDA hard-clause review portal.

The app supports direct NDA review, native `.docx` redline export, and a lightweight Repository board for imported matters. The Repository can import `.docx` and text-based `.pdf` NDA attachments from a configured inbound Gmail account, while outbound redline sends use the configured outbound Gmail role and require an explicit confirmation click.

You can paste NDA text directly, upload a plain text file, upload a `.docx` Word document or text-based `.pdf` for one-off review, or import a `.docx`/`.pdf` into the Repository for matter-based review. Scanned image-only PDFs need OCR before review.

## Run locally

Requires Python 3.9 or newer.

```bash
python3 -m nda_automation.server --port 8787
```

Then open:

```text
http://127.0.0.1:8787
```

## Test

```bash
python3 -m unittest discover -s tests
```

Frontend behavior tests run the real app in Chromium and cover review view modes, viewer editing, redline rendering, and DOCX export:

```bash
npm install
npm run test:frontend
```

## Gmail roles

Install the optional Gmail dependencies before using the connector:

```bash
python3 -m pip install ".[gmail]"
```

The Gmail integration reads OAuth token files from environment variables:

```bash
export NDA_GMAIL_INBOUND_TOKEN_PATH=/path/to/inbound-token.json
export NDA_GMAIL_OUTBOUND_TOKEN_PATH=/path/to/outbound-token.json
```

Inbound sync imports recent `.docx` and text-based `.pdf` attachments with NDA/confidentiality-related subject terms into the `Gmail Demo` Repository lane. Outbound send generates the same Word redline/report used by download/export, then emails it back to the matter sender only after `Send Redline` is confirmed.

## Current checks

- Mutual NDA obligations
- Broad confidential information definition
- Approved governing law
- Term and ordinary confidentiality survival up to five years
- No non-circumvention or substitute-purpose exclusivity
- Complete execution block

## Review output

The backend splits each uploaded document into numbered paragraphs (`p1`, `p2`, `p3`) and returns clause results with backend-identified paragraph evidence, issue labels, fix text, and review-only proposed redlines. DOCX uploads preserve the source Word paragraph index; PDF uploads preserve extracted page metadata. The frontend uses backend paragraph IDs for highlighting and clause navigation instead of guessing locally.

PDF extraction also reports basic quality metadata, including page counts, pages without extractable text, extracted character/paragraph counts, repeated header/footer removal, and warnings when extraction looks sparse or degraded.

Repository imports preserve the original uploaded `.docx` so matter exports can generate native Word tracked changes against the source document. PDF matter exports generate a Word review report because PDFs cannot be patched with native Word tracked changes. If a Repository matter is re-reviewed as edited text, export switches to the normal review-report flow rather than reusing stale stored matter results.

## Policy decisions to confirm

- Confidentiality residuals and reverse-engineering terms are flagged only when they appear in exclusion-context paragraphs.
- DOCX paragraph alignment fails the whole review if any extracted paragraph cannot be aligned to the source text.
