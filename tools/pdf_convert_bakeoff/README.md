# PDF → DOCX Conversion Bake-off

A **dev/eval tool** (NOT wired into prod) that compares PDF→DOCX conversion
engines on real NDA PDFs across three scoring layers:

1. **Intrinsic fidelity** — an LLM vision judge scores each converted DOCX vs the
   source PDF (tables, headings, reading order, paragraph integrity, text
   accuracy, logo/image presence — 0–5 each).
2. **Downstream effect (the key metric)** — each converted DOCX is run through the
   **real shipped review/structure/redline pipeline** (`review_nda_with_active_engine`,
   `contract_structure`, the `map_paragraphs_to_reconstruction` redline aligner) to
   measure whether the conversion makes the production system work better: clauses
   extracted, structure sections, and **redline-anchor success rate**.
3. **Operational** — latency, success rate, output size aggregated per engine, with
   clearly-labeled placeholders for cost/doc and data-handling terms.

Nothing here is imported by the application server. All external credentials come
from **environment variables only** — never hardcoded, never logged.

## Engines

| Engine | Env vars (ENV ONLY) | Needs key? |
|---|---|---|
| `pdf2docx` (baseline) | — | No — reuses the in-repo `reconstruct_pdf_to_docx` |
| `adobe` (PDF Services Export PDF) | `ADOBE_CLIENT_ID`, `ADOBE_CLIENT_SECRET` | Yes |
| `cloudmersive` | `CLOUDMERSIVE_API_KEY` (+ optional `CLOUDMERSIVE_BASE_URL`) | Yes, or self-hosted |
| `ilovepdf` (iLoveAPI PDF→Word) | `ILOVEPDF_PUBLIC_KEY`, `ILOVEPDF_SECRET_KEY` | Yes (see caveat) |

Each adapter **skips gracefully** (`SKIPPED: missing <ENV>`) when its creds are
absent, so the harness runs end-to-end on just the `pdf2docx` baseline today,
before any keys exist.

### Self-hosted Cloudmersive

Cloudmersive ships a self-hostable container exposing the identical
`/convert/pdf/to/docx` path. Point the adapter at it:

```bash
export CLOUDMERSIVE_BASE_URL=http://localhost:8080   # your container
# CLOUDMERSIVE_API_KEY optional when the container is configured key-free
```

When `CLOUDMERSIVE_BASE_URL` is set to a non-default host, the adapter runs even
without an API key.

### iLovePDF caveat (doc-lookup finding)

The iLoveAPI **developer REST `process` API does not publicly expose a PDF→Word
tool.** Verified against `www.iloveapi.com/docs/api-reference` and the official
`ilovepdf/ilovepdf-php` SDK: the documented tools are compress, extract, htmlpdf,
imagepdf, merge, `officepdf` (Office→PDF, the *wrong* direction), pagenumber,
pdfa, pdfjpg, pdfocr, protect, repair, rotate, split, unlock, validatepdfa,
editpdf, watermark, pdfmarkdown, summarize, pdfextract, splitsmart, formsdetect.
PDF→Word exists in the iLovePDF **consumer web app** but is not a task class in
the developer SDK.

The adapter implements the **full correct async flow** (auth → start → upload →
process → download) and parameterises the tool via `ILOVEPDF_TOOL` (default
`pdfword`). If/when iLoveAPI ships the tool — or if your account has it enabled —
it works immediately; otherwise it surfaces the API's tool error per (doc, engine)
rather than silently emitting a wrong-direction result. Override:

```bash
export ILOVEPDF_TOOL=pdfword   # try whatever your account exposes
```

## Setup

The baseline needs the repo's PDF extra (`fitz` / `pdf2docx`); the intrinsic judge
also uses `fitz` to render PDF pages. Use a venv with the extra installed:

```bash
python3.13 -m venv .venv-bakeoff
.venv-bakeoff/bin/pip install -e ".[pdf]"
```

Set whichever engine keys you have (or none):

```bash
export ADOBE_CLIENT_ID=...        ADOBE_CLIENT_SECRET=...
export CLOUDMERSIVE_API_KEY=...   # or CLOUDMERSIVE_BASE_URL=http://localhost:8080
export ILOVEPDF_PUBLIC_KEY=...    ILOVEPDF_SECRET_KEY=...
export OPENROUTER_API_KEY=...     # only for the intrinsic LLM judge
```

A local `.env` is git-ignored; **never commit keys.**

## Drop corpus PDFs in

Put real NDA PDFs in `tools/pdf_convert_bakeoff/corpus/`. The outputs dir and
corpus contents are git-ignored (only `.gitkeep` is tracked) so no large binaries
or client docs get committed.

## Run the stages

```bash
PY=.venv-bakeoff/bin/python

# 1. Convert: every available engine on every corpus PDF -> outputs/<doc>__<engine>.docx
$PY -m tools.pdf_convert_bakeoff.runner convert \
    --corpus tools/pdf_convert_bakeoff/corpus \
    --out    tools/pdf_convert_bakeoff/outputs
#    -> outputs/results.json + a summary table

# 2. Downstream (the key metric): run each converted DOCX through the real pipeline
$PY -m tools.pdf_convert_bakeoff.score_downstream --out tools/pdf_convert_bakeoff/outputs
#    -> outputs/downstream_scores.json

# 3. Intrinsic fidelity (needs OPENROUTER_API_KEY; skips cleanly otherwise)
$PY -m tools.pdf_convert_bakeoff.score_intrinsic \
    --out tools/pdf_convert_bakeoff/outputs \
    --corpus tools/pdf_convert_bakeoff/corpus \
    --model anthropic/claude-opus-4.8-fast      # configurable; or BAKEOFF_JUDGE_MODEL
#    -> outputs/intrinsic_scores.json

# 4. Aggregate all three layers into one per-engine report
$PY -m tools.pdf_convert_bakeoff.report --out tools/pdf_convert_bakeoff/outputs
#    -> outputs/report.json + a comparison table
```

## Read the results

- `results.json` — per-(doc, engine) status / latency / output size / error.
- `downstream_scores.json` — per-DOCX clauses extracted, structure sections,
  and **`anchor_success_rate`** (the redline-placement metric).
- `intrinsic_scores.json` — per-DOCX 0–5 rubric scores + judge notes.
- `report.json` — per-engine aggregate across all three layers. The
  `operational.cost_per_doc_usd` and `operational.data_handling` fields are
  **placeholders** to fill from each vendor's pricing / DPA.

The headline comparison is the **`ANCHOR`** column (avg redline-anchor success):
a higher rate means the production redline export's fail-closed anchor gate can
place more accepted changes, i.e. that conversion makes the real system work
better.

## Verified API flows

- **Adobe PDF Services Export PDF**: `POST /token` (form: client_id, client_secret)
  → `POST /assets` (json `mediaType`) → PUT bytes to `uploadUri` →
  `POST /operation/exportpdf` (json `assetID`, `targetFormat: docx`) → 201 +
  `location` header → poll until `status: done` → GET `asset.downloadUri`.
  Base overridable via `ADOBE_PDF_SERVICES_BASE_URL`.
- **Cloudmersive**: `POST {base}/convert/pdf/to/docx`, header `Apikey`,
  multipart field `inputFile`, returns raw DOCX bytes. Same path self-hosted.
- **iLovePDF**: `POST /v1/auth` (public_key → JWT) → `GET /v1/start/{tool}` →
  `POST {server}/v1/upload` → `POST {server}/v1/process` → `GET {server}/v1/download/{task}`.
  See the caveat above re: the PDF→Word tool name.
