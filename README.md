# nda-automation

A focused NDA review and redline workstation for hard-clause review, matter intake, AI-assisted semantic review, and Gmail-based NDA workflows.

The app reviews pasted text, `.docx` files, and text-based PDFs against a configurable playbook. It returns pass / review / fail clause findings with structured evidence, reason codes, audit traces, proposed fixes, and exportable Word redlines. Repository matters preserve uploaded source documents so `.docx` matters can be exported with native Word tracked changes.

## Connect APIs First

The repository does not include API keys or Gmail OAuth tokens. To run the full product locally, each user should connect their own AI and Gmail credentials after cloning the repo.

### 1. Install full local dependencies

```bash
python3 -m pip install -e ".[pdf,gmail]"
```

### 2. Create local environment config

Copy the template and fill in local credentials:

```bash
cp .env.example .env
```

Load the file before starting the app:

```bash
set -a
source .env
set +a
```

The real `.env` file is ignored by Git.

### 3. Connect AI review

In `.env`, set the AI provider/model and API key:

```bash
NDA_AI_REVIEW_ENABLED=true
NDA_AI_PROVIDER=alibaba
NDA_AI_MODEL=qwen3.5-plus
ALIBABA_API_KEY="your-alibaba-api-key"
```

Then start the app:

```bash
python3 -m nda_automation.server --port 8787
```

You can also paste/save the AI key from **Admin -> AI** after the app is running. Saved local keys are stored in ignored app data and are not committed to Git.

### 4. Connect Gmail inbound/outbound

Place OAuth token JSON files outside Git, then point the app at them in `.env`:

```bash
NDA_GMAIL_INBOUND_TOKEN_PATH="/absolute/path/to/inbound-token.json"
NDA_GMAIL_OUTBOUND_TOKEN_PATH="/absolute/path/to/outbound-token.json"
```

For local development only, the app also checks these ignored paths:

```text
data/gmail/inbound-token.json
data/gmail/outbound-token.json
```

Use **Admin -> Email** to confirm whether inbound sync and outbound send are ready. Gmail remains disabled until token files are configured and readable by the service.

### 5. Open the app

```text
http://127.0.0.1:8787/
```

Do not commit real API keys, `.env` files, or Gmail token JSON files. Share those credentials separately through a secure internal channel.

## Features

- Review pasted NDA text, plain text files, `.docx` Word documents, and text-based PDFs.
- Detect required and prohibited hard clauses with paragraph-level evidence, reason codes, and review-state decisions.
- Preserve contract structure, resolve clause/section references, and classify legal concepts before checker evaluation.
- Run optional AI semantic second opinions and AI draft-fix validation from the Review panel.
- Generate Word review reports and source `.docx` redlines with tracked changes.
- Import uploaded matters into a Repository board with review state, source documents, redline drafts, and stage tracking.
- Sync inbound Gmail NDA attachments into the Repository when Gmail is configured.
- Send outbound Gmail redlines only after an explicit confirmation action.
- Edit the review playbook and inspect checker/AI logic from the Admin interface.
- Inspect deployment status and non-sensitive telemetry from auth-gated admin endpoints.
- Download a sensitive matter backup from an auth-gated backup endpoint.

## Product Areas

- **Review Workstation**: one-off text/document review, clause checklist, structure view, AI evidence, evidence navigation, viewer edits, and DOCX export.
- **Repository**: imported matters, board lanes, stored source documents, redline drafts, and matter review views.
- **Admin**: playbook editor, deterministic engine explainability, AI controls, Email/Gmail connection state/settings, deployment status, telemetry, and matter backup.
- **Gmail workflows**: inbound attachment import and outbound redline reply/send.

## Review Architecture

The review system is layered:

1. **Deterministic Python rules engine** applies the playbook and produces pass / review / fail decisions.
2. **Contract structure map** identifies headings, sections, articles, clauses, and paragraph ranges.
3. **Reference resolver** maps references such as "clauses 2, 3, 4 and 5" or hybrid identifiers such as `10b`.
4. **Concept classifier** tags paragraphs and sections with legal concepts so checks can reason beyond raw keywords.
5. **AI semantic review** can provide clause-specific second opinions and draft-fix validation with confidence, citations, and suggested fixes.
6. **Review-state arbiter** escalates disagreement, low confidence, invalid citations, or checker uncertainty to human review.

The deterministic layer remains the primary foundation. AI is optional and must cite source text before its result is used. The next intended hardening step is to make semantic AI review fully blind to Python's deterministic verdict, then compare the two results after the AI response returns.

## Run Locally

Requires Python 3.9 or newer.

```bash
python3 -m nda_automation.server --port 8787
```

Then open:

```text
http://127.0.0.1:8787
```

Local development can use the default local data directory. Public or non-loopback deployments must use durable storage and authentication.

## Optional Dependencies

The core review server is intentionally stdlib-first. Optional capabilities are installed with extras:

```bash
python3 -m pip install -e ".[pdf]"
python3 -m pip install -e ".[gmail]"
python3 -m pip install -e ".[pdf,gmail]"
```

- PDF intake uses `pypdf`.
- Gmail intake/send uses the Google API packages.
- Without the `pdf` extra, PDF uploads fail with a clear "PDF support is not installed" error.
- The Render blueprint installs `.[pdf,gmail]` because the hosted product enables both PDF and Gmail workflows.

## Configuration

Common environment variables:

- `NDA_DATA_DIR`: directory for matter records, source uploads, app settings, and Gmail sync state.
- `NDA_EXPORTS_DIR`: directory for persisted export downloads.
- `NDA_REQUIRE_AUTH`: set to `true` to require HTTP Basic auth.
- `NDA_AUTH_USERNAME` and `NDA_AUTH_PASSWORD`: Basic auth credentials.
- `NDA_RATE_LIMIT_PER_MINUTE`: positive integer request limit for expensive endpoints, or `0` for trusted local testing.
- `NDA_GMAIL_INBOUND_TOKEN_PATH`: OAuth token file for inbound Gmail sync.
- `NDA_GMAIL_OUTBOUND_TOKEN_PATH`: OAuth token file for outbound Gmail sends.
- `NDA_ALLOW_EPHEMERAL_DATA`: set to `true` only for short-lived public demos using ephemeral storage.

Optional semantic fallback:

```bash
export NDA_SEMANTIC_EVALUATOR=module.path[:callable_name]
```

The callable is lazy-loaded only when configured and receives keyword arguments `text`, `normalized`, `clause`, `paragraphs`, and `current_result`. It should return `None` or a small decision dictionary such as:

```json
{"status": "match", "reason": "...", "matched_paragraph_ids": ["p1"]}
```

The deterministic core remains stdlib-first. External AI review is optional and only runs when configured.

Optional AI semantic review:

```bash
export NDA_AI_REVIEW_ENABLED=true
export NDA_AI_PROVIDER=alibaba
export NDA_AI_MODEL=qwen3.5-plus
export ALIBABA_API_KEY=sk-...
```

Supported providers are `gemini`, `openrouter`, and `alibaba`. Alibaba/Qwen uses the Singapore OpenAI-compatible endpoint at `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` and currently defaults to `qwen3.5-plus`. Admins can save a local API key from the AI tab; saved keys are stored under ignored app data and are not returned to the browser.

## Deploy

The app needs a Python web service because the static frontend calls API routes served by `nda_automation.server`.

This repo includes a Render blueprint:

```bash
render.yaml
```

The production start command is:

```bash
python -m nda_automation.server --host 0.0.0.0 --port $PORT
```

Public deployments require HTTP Basic authentication. Non-loopback binds such as `0.0.0.0` require auth automatically, and the Render blueprint sets `NDA_REQUIRE_AUTH=true`. If auth is required but credentials are missing, the server refuses to start.

The only unauthenticated route is `/healthz` for platform health checks.

The Render blueprint uses a persistent disk mounted at `/var/data`. `NDA_DATA_DIR` and `NDA_EXPORTS_DIR` must point at durable storage for public deployments because Repository matters include extracted NDA text, uploaded source documents, review results, redline drafts, app settings, and Gmail sync state. The server refuses to start on non-loopback hosts when `NDA_DATA_DIR` is missing or points at ephemeral storage such as `/tmp`, unless `NDA_ALLOW_EPHEMERAL_DATA=true` is set for a short-lived demo.

Authenticated admins can check:

- `/api/deployment/status`: auth, storage, health-check, and rate-limit shape.
- `/api/telemetry`: non-sensitive counters such as review requests, export failures, Gmail sync failures, and rate-limit hits.
- `/api/matters/export`: sensitive JSON backup of matter records plus stored-document manifest. It does not embed uploaded source document bytes.

## Security and Data Notes

- Matter list/detail API responses expose metadata only.
- Extracted NDA text, review results, and redline drafts are returned only through the auth-gated matter review workflow.
- Public deployments are rate-limited on expensive review, upload, export, Gmail send, and backup endpoints.
- Atomic JSON saves fsync file contents and parent directories for durability.
- DOCX XML parsing rejects unsupported DTD/entity declarations.
- DOCX export strips invalid XML characters and protects against malformed tracked-change markup.
- Uploaded source documents and matter backups are sensitive and should be protected as legal work product.

## Gmail Roles

Install Gmail dependencies before using the connector:

```bash
python3 -m pip install -e ".[gmail]"
```

If Gmail should import PDF attachments too:

```bash
python3 -m pip install -e ".[pdf,gmail]"
```

Configure token files:

```bash
export NDA_GMAIL_INBOUND_TOKEN_PATH=/path/to/inbound-token.json
export NDA_GMAIL_OUTBOUND_TOKEN_PATH=/path/to/outbound-token.json
```

For local development, the app also checks ignored project-local token files:

```text
data/gmail/inbound-token.json
data/gmail/outbound-token.json
```

Inbound sync imports recent `.docx` and text-based `.pdf` attachments with NDA/confidentiality-related subject terms into the Repository. Outbound send generates the same Word redline/report used by download/export, then emails it back to the matter sender only after confirmation.

Gmail remains disabled until token files are configured and readable by the service.

## Current Checks

- Mutual NDA obligations.
- Broad Confidential Information definition.
- Standard Confidential Information exclusions, including qualified independent-development carve-outs.
- Approved governing law: India, Delaware, England and Wales, or DIFC.
- Term and ordinary confidentiality survival up to five years.
- No non-circumvention or substitute-purpose exclusivity.
- Complete execution/signature block.

## Review Output

The backend splits each uploaded document into numbered paragraphs such as `p1`, `p2`, and `p3`. Clause results include backend-identified paragraph evidence, structured evidence records, reason codes, audit traces, issue labels, fix text, and proposed redlines. The frontend uses backend paragraph IDs for highlighting and clause navigation instead of guessing locally.

DOCX uploads preserve the source Word paragraph index. PDF uploads preserve extracted page metadata. PDF extraction reports basic quality metadata, including page counts, pages without extractable text, extracted character/paragraph counts, repeated header/footer removal, and sparse-extraction warnings.

Repository imports preserve the original uploaded `.docx` so matter exports can generate native Word tracked changes against the source document. PDF matter exports generate a Word review report because PDFs cannot be patched with native Word tracked changes.

## Test

Install the test extras you need:

```bash
python3 -m pip install -e ".[pdf]"
```

Run backend tests:

```bash
PYTHONPATH=. pytest -q
```

Run frontend behavior tests:

```bash
NODE_PATH=/Users/daniyalahmad/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules \
PYTHON=/opt/anaconda3/bin/python \
/Users/daniyalahmad/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node tests/frontend/review-workstation.cjs
```

Frontend tests run the real app in Chromium and cover review view modes, viewer editing, redline rendering, Gmail/admin surfaces, and DOCX export behavior.

## Roadmap

- Matter activity timeline for imports, reviews, draft saves, exports, sends, stage changes, and Gmail sync events.
- Export/send preflight that summarizes selected redlines, manual edits, recipient, filename, and source-text warnings.
- Matter search and filtering by counterparty, sender, filename, status, issue, date, stage, and source.
- Review decision memory for recurring counterparty positions.
- Continued Gmail onboarding polish and recovery guidance.
