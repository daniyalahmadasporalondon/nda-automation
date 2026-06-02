# nda-automation

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

## Current checks

- Mutual NDA obligations
- Broad confidential information definition
- Approved governing law
- Term and ordinary confidentiality survival up to five years
- No non-circumvention or substitute-purpose exclusivity
- Complete execution block

## Review output

The backend splits each uploaded document into numbered paragraphs (`p1`, `p2`, `p3`) and returns clause results with backend-identified paragraph evidence, issue labels, fix text, and review-only proposed redlines. DOCX uploads preserve the source Word paragraph index. The frontend uses backend paragraph IDs for highlighting and clause navigation instead of guessing locally.

## Policy decisions to confirm

- Confidentiality residuals and reverse-engineering terms are flagged only when they appear in exclusion-context paragraphs.
- DOCX paragraph alignment fails the whole review if any extracted paragraph cannot be aligned to the source text.
