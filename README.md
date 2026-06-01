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

## Dependency policy

The core NDA review server is intentionally stdlib-only. Optional integrations are installed
only when the corresponding capability is enabled:

- PDF intake uses `pypdf` and requires `python3 -m pip install ".[pdf]"`.
- Gmail intake/send uses the Google API packages and requires `python3 -m pip install ".[gmail]"`.

Without the `pdf` extra, PDF uploads fail with a "PDF support is not installed" error instead
of treating the user's file as invalid. The Render blueprint installs `.[pdf,gmail]`
deliberately because the hosted product enables both PDF intake and Gmail workflows.

## Deploy

The app needs a Python web service because the static frontend calls the local API routes served by `nda_automation.server`.

This repo includes a Render blueprint:

```bash
render.yaml
```

The production start command is:

```bash
python -m nda_automation.server --host 0.0.0.0 --port $PORT
```

Public deployments require HTTP Basic authentication. Non-loopback binds such as `0.0.0.0`
require auth automatically, and the Render blueprint also sets `NDA_REQUIRE_AUTH=true`.
Set `NDA_AUTH_USERNAME` and `NDA_AUTH_PASSWORD` before using a hosted service; if auth is
required but credentials are missing, the server refuses to start. The only unauthenticated
route is `/healthz` for platform health checks. Repository matter API responses expose only
metadata; extracted text, review results, and redline drafts are available only through the
auth-gated matter review workflow.

The Render blueprint uses a paid web service with a persistent disk mounted at `/var/data`.
`NDA_DATA_DIR` and `NDA_EXPORTS_DIR` must point at durable storage for a public deployment,
because Repository matters include extracted NDA text, uploaded source documents, review
results, redline drafts, app settings, and Gmail sync state. The server refuses to start on
non-loopback hosts when `NDA_DATA_DIR` is missing or points at ephemeral storage such as
`/tmp`, unless `NDA_ALLOW_EPHEMERAL_DATA=true` is set for a short-lived demo.

The hosted blueprint also sets `NDA_RATE_LIMIT_PER_MINUTE=120` for expensive endpoints such
as review, document upload, matter import, DOCX export, Gmail send, and matter backup. Set it
to a different positive integer for your deployment, or `0` only for trusted local testing.

Authenticated admins can download a sensitive JSON backup from `/api/matters/export`. The
backup includes full matter records plus a stored-document manifest; it does not embed the
uploaded source document bytes.

Authenticated admins can also check `/api/deployment/status` for the live auth, storage,
health-check, and rate-limit shape, and `/api/telemetry` for non-sensitive counters such as
review request counts, export failures, Gmail sync failures, and rate-limit hits.

Gmail will stay disabled until the deployed service has `NDA_GMAIL_INBOUND_TOKEN_PATH` and `NDA_GMAIL_OUTBOUND_TOKEN_PATH` configured with token files available to the service.

## Test

```bash
python3 -m pip install -e ".[pdf]"
python3 -m unittest discover -s tests
```

Frontend behavior tests run the real app in Chromium and cover review view modes, viewer editing, redline rendering, and DOCX export:

```bash
npm install
npm run test:frontend
```

## Gmail roles

Install the optional Gmail dependencies before using the connector. If Gmail should import
PDF attachments too, install both extras:

```bash
python3 -m pip install ".[gmail]"
python3 -m pip install ".[pdf,gmail]"
```

The Gmail integration reads OAuth token files from environment variables:

```bash
export NDA_GMAIL_INBOUND_TOKEN_PATH=/path/to/inbound-token.json
export NDA_GMAIL_OUTBOUND_TOKEN_PATH=/path/to/outbound-token.json
```

For local development, the app also checks ignored project-local token files:
`data/gmail/inbound-token.json` and `data/gmail/outbound-token.json`.

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
