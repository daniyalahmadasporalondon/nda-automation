# nda-automation

A focused NDA hard-clause review portal.

The app supports direct NDA review, native `.docx` redline export, and a lightweight Repository board for imported matters. The current Repository flow uses a local `Gmail Demo` intake lane only; it is intentionally not a live Gmail or Drive integration yet.

You can paste NDA text directly, upload a plain text file, upload a `.docx` Word document for one-off review, or import a `.docx` into the Repository for matter-based review.

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

## Current checks

- Mutual NDA obligations
- Broad confidential information definition
- Approved governing law
- Term and ordinary confidentiality survival up to five years
- No non-circumvention or substitute-purpose exclusivity
- Complete execution block

## Review output

The backend splits each uploaded document into numbered paragraphs (`p1`, `p2`, `p3`) and returns clause results with backend-identified paragraph evidence, issue labels, fix text, and review-only proposed redlines. DOCX uploads preserve the source Word paragraph index. The frontend uses backend paragraph IDs for highlighting and clause navigation instead of guessing locally.

Repository imports preserve the original uploaded `.docx` so matter exports can generate native Word tracked changes against the source document. If a Repository matter is re-reviewed as edited text, export switches to the normal review-report flow rather than reusing stale stored matter results.

## Policy decisions to confirm

- Confidentiality residuals and reverse-engineering terms are flagged only when they appear in exclusion-context paragraphs.
- DOCX paragraph alignment fails the whole review if any extracted paragraph cannot be aligned to the source text.
