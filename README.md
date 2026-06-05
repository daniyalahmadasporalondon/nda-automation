# nda-automation

A repository-first NDA review and redline workstation for hard-clause review, matter intake, AI-first legal assessment, and Gmail-based NDA workflows.

The app imports `.docx` files and text-based PDFs through manual upload or Gmail, stores them as Repository matters, and reviews them against a configurable playbook. It returns pass / review / fail clause findings with structured evidence, AI assessment metadata, deterministic comparison data, proposed fixes, and exportable Word redlines. Repository matters preserve uploaded source documents so `.docx` matters can be exported with native Word tracked changes.

The primary UI flow is now matter intake -> reviewer workstation -> human confirmation -> DOCX export/send. The old standalone `Review NDA` toolbar action has been removed; reviews are generated during matter import and refreshed when a stored review is stale.

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
NDA_AI_PROVIDER=gemini
NDA_AI_MODEL=gemini-3.5-flash
# Active review defaults to AI-first + fail-closed. Leave these unset unless pinning runtime.
NDA_ACTIVE_REVIEW_ENGINE=
NDA_AI_FIRST_FALLBACK_MODE=
GEMINI_API_KEY="your-gemini-api-key"
```

Then start the app:

```bash
python3 -m nda_automation.server --port 8787
```

You can also paste/save the AI key from **Admin -> AI** after the app is running. Saved local keys are stored in ignored app data and are not committed to Git.

### 4. Connect Gmail inbound/outbound

For hosted/private deployments, sign in with Google and connect Gmail from **Admin -> Email**. The app stores Gmail OAuth tokens per signed-in user under durable app data, so one user's Gmail account is not shared with another user.

For local development with shared token files, place OAuth token JSON files outside Git, then point the app at them in `.env`:

```bash
NDA_GMAIL_INBOUND_TOKEN_PATH="/absolute/path/to/inbound-token.json"
NDA_GMAIL_OUTBOUND_TOKEN_PATH="/absolute/path/to/outbound-token.json"
```

For local development only, the app also checks these ignored paths:

```text
data/gmail/inbound-token.json
data/gmail/outbound-token.json
```

Use **Admin -> Email** to confirm whether inbound sync and outbound send are ready. Gmail remains disabled until a signed-in user connects Gmail or legacy local token files are configured and readable by the service.

### 5. Open the app

```text
http://127.0.0.1:8787/
```

Do not commit real API keys, `.env` files, or Gmail token JSON files. Share those credentials separately through a secure internal channel.

## Features

- Import and review `.docx` Word documents and text-based PDFs through manual upload, Repository, or Gmail intake.
- Run AI-first hard-clause assessment with paragraph-level evidence, reason codes, citations, confidence metadata, and review-state decisions.
- Keep deterministic review available for comparison, explicit fallback, audit metadata, and backend validation.
- Preserve contract structure, resolve clause/section references, and classify legal concepts before checker evaluation.
- Run AI semantic second opinions and AI draft-fix validation from the Review panel.
- Generate Word review reports and source `.docx` redlines with tracked changes.
- Import uploaded matters into a Repository board with review state, source documents, redline drafts, and stage tracking.
- Sync inbound Gmail NDA attachments into the Repository when Gmail is configured.
- Send outbound Gmail redlines only after an explicit confirmation action.
- Edit the review playbook from the Playbook tab with schema validation, redline-template previews, allowed placeholders, generated Governing Law options, version history, and restore.
- Keep AI/runtime controls, Gmail settings, deployment status, telemetry, and backup operations in Admin.
- Keep methodology and checker/AI explanation in Guide.
- Inspect deployment status and non-sensitive telemetry from auth-gated admin endpoints.
- Download a sensitive matter backup from an auth-gated backup endpoint.

## Product Areas

- **Review Workstation**: selected matter review, clause checklist, structure view, AI evidence, evidence navigation, viewer edits, reviewed toggles, DOCX export, and outbound send confirmation.
- **Repository**: imported matters, board lanes, stored source documents, redline drafts, and matter review views.
- **Playbook**: editable clause policy, safe playbook fields, Governing Law jurisdiction options, term/survival caps, redline-template preview/validation, policy version history, and restore.
- **Admin**: AI-first runtime controls, provider/API-key status, Email/Gmail connection state/settings, deployment status, telemetry, and matter backup.
- **Guide**: read-only methodology, document-processing model, checker explainability, shared structure/reference/concept layers, and AI assessment guide.
- **Gmail workflows**: inbound attachment import and outbound redline reply/send.

## Review Architecture

The active review path is:

1. **Playbook and document structure** define the clause requirements, approved positions, source paragraphs, headings, sections, and references.
2. **AI-first legal assessment** applies the playbook to the selected source paragraphs and produces the saved clause verdicts, issue types, rationale, citations, confidence, and proposed redlines.
3. **Deterministic review and comparison** remain available for audit, explicit fallback, reason-code comparison, stale-review refresh guards, and operational validation.
4. **Reviewer UI** presents the final clause cards, right-panel analysis, insertable redline options, comments, include/ignore choices, and per-clause reviewed toggles for human sign-off.

The active review engine defaults to **AI-first** with **fail closed** behavior. If AI is unavailable and fallback mode is `fail_closed`, new review creation returns an error instead of silently substituting deterministic results. Admin can change the active engine and AI-first fallback mode at runtime from **Admin -> AI** when those values are not pinned by environment variables. Runtime changes are audit-logged without secrets, and env-pinned values are reported as read-only operational warnings.

Set `NDA_ACTIVE_REVIEW_ENGINE=deterministic` only when deployment configuration should force deterministic review as the saved `review_result` for new reviews. Set `NDA_AI_FIRST_FALLBACK_MODE=deterministic` only when AI-first failures should fall back to deterministic review and record that fallback in metadata. Leave the fallback unset or set `fail_closed` when missing AI output should block review creation. Set `NDA_AI_FIRST_REVIEW_ENABLED=true` to store AI-first shadow/comparison output while a deterministic matter review is created elsewhere.

## Playbook, Admin, and Guide

The operational surfaces are intentionally split:

- **Playbook** changes review policy. It is the only place to edit safe playbook fields such as clause name, stance, preferred position, check trigger, term/survival cap, permitted longer-survival carve-outs, and Governing Law jurisdictions.
- **Admin** changes runtime operations. It manages AI provider/key status, active review engine, AI-first fallback behavior, Gmail toggles/search/sync cadence, deployment health, telemetry, and backups.
- **Guide** explains methodology. It is read-only and documents how document structure, reference resolution, concept classification, deterministic validation, and AI-first assessment fit together.

Playbook saves are validated before they are written. The frontend blocks unknown redline-template placeholders, and the backend validates the full playbook schema and redline templates again before saving. Every Playbook save records a version-history snapshot; restore is disabled while there are unsaved drafts.

Governing Law is edited through approved jurisdiction options and draft phrases. It does not use a free redline-template field. The UI shows generated insertable redline text for each approved jurisdiction.

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

In the browser, use **Upload** for manual `.docx`/PDF intake, **Repository** to open stored matter reviews, **Review** to inspect and edit the current matter, **Playbook** to edit review policy, **Admin** to manage AI/Gmail/runtime settings, and **Guide** to read methodology. The Review workstation no longer has a standalone `Review NDA` action.

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
- `NDA_USERS_PATH`: optional path for user/session/sync history storage; hosted deployments should keep this on durable storage, for example `/var/data/users.json`.
- `NDA_REQUIRE_AUTH`: set to `true` to require login.
- `NDA_ALLOWED_HOSTS`: comma-separated allowed request hostnames. Set this to the Render hostname, for example `your-service.onrender.com`.
- `NDA_GOOGLE_OAUTH_CLIENT_ID` and `NDA_GOOGLE_OAUTH_CLIENT_SECRET`: Google login credentials for per-user identity.
- `NDA_GOOGLE_OAUTH_REDIRECT_URI`: fixed redirect URI for hosted Google login, for example `https://your-service.onrender.com/auth/google/callback`.
- `NDA_GMAIL_OAUTH_REDIRECT_URI`: fixed redirect URI for hosted Gmail connection, for example `https://your-service.onrender.com/auth/gmail/callback`.
- `NDA_AUTH_USERNAME` and `NDA_AUTH_PASSWORD`: optional HTTP Basic auth fallback credentials.
- `NDA_RATE_LIMIT_PER_MINUTE`: positive integer request limit for expensive endpoints, or `0` for trusted local testing.
- `NDA_GMAIL_INBOUND_TOKEN_PATH`: legacy shared OAuth token file for local inbound Gmail sync. Leave unset for hosted per-user Gmail.
- `NDA_GMAIL_OUTBOUND_TOKEN_PATH`: legacy shared OAuth token file for local outbound Gmail sends. Leave unset for hosted per-user Gmail.
- `NDA_AI_REVIEW_ENABLED`: enables provider-backed AI review when true.
- `NDA_AI_PROVIDER`: `gemini`.
- `NDA_AI_MODEL`: provider model name. Use `gemini-3.5-flash` for the current hosted Gemini setup, or another model your key can access.
- `GEMINI_API_KEY`: server-side Gemini AI review key.
- `GROQ_API_KEY`: server-side Groq key for Qwen Gmail attachment selection.
- `NDA_GMAIL_TRIAGE_MODEL`: Gmail triage model name, for example `qwen/qwen3-32b`.
- `NDA_ACTIVE_REVIEW_ENGINE`: optional environment pin for `ai_first` or `deterministic`.
- `NDA_AI_FIRST_FALLBACK_MODE`: optional environment pin for `fail_closed` or `deterministic`.
- `NDA_AI_FIRST_REVIEW_ENABLED`: stores AI-first shadow/comparison results when enabled.
- `NDA_ALLOW_EPHEMERAL_DATA`: set to `true` only for short-lived public demos using ephemeral storage.

Optional semantic fallback:

```bash
export NDA_SEMANTIC_EVALUATOR=module.path[:callable_name]
```

The callable is lazy-loaded only when configured and receives keyword arguments `text`, `normalized`, `clause`, `paragraphs`, and `current_result`. It should return `None` or a small decision dictionary such as:

```json
{"status": "match", "reason": "...", "matched_paragraph_ids": ["p1"]}
```

The deterministic core remains stdlib-first. Provider-backed AI review runs only when configured; if the active engine is AI-first and no provider/key is configured, review creation fails closed unless runtime settings or environment variables switch the active engine/fallback behavior.

Optional AI semantic review:

```bash
export NDA_AI_REVIEW_ENABLED=true
export NDA_AI_PROVIDER=gemini
export NDA_AI_MODEL=gemini-3.5-flash
export GEMINI_API_KEY=...
```

Supported providers are Gemini for NDA review and Groq/Qwen for Gmail triage. Admins can save a local Gemini API key from the AI tab; saved keys are stored under ignored app data and are not returned to the browser.

## Deploy

The app needs a Python web service because the static frontend calls API routes served by `nda_automation.server`.

This repo includes a Render blueprint:

```bash
render.yaml
```

The checked-in blueprint is configured for a short-lived free Render demo: it
uses the free service plan and ephemeral `/tmp` storage so the app can boot
without a paid persistent disk. This is suitable for showing AI review,
redlines, Gmail receive, and Gmail send in a live session. Data, user sessions,
Gmail tokens, imported matters, drafts, and exports can disappear on restart,
redeploy, or service sleep.

For a real private beta, switch the service back to a paid plan with a
persistent disk mounted at `/var/data`, set `NDA_DATA_DIR=/var/data`,
`NDA_USERS_PATH=/var/data/users.json`, `NDA_EXPORTS_DIR=/var/data/exports`, and
remove `NDA_ALLOW_EPHEMERAL_DATA=true`.

The production start command is:

```bash
python -m nda_automation.server --host 0.0.0.0 --port $PORT
```

Public deployments require authentication. Non-loopback binds such as `0.0.0.0` require auth automatically, and the Render blueprint sets `NDA_REQUIRE_AUTH=true`. Configure Google OAuth for per-user login, or HTTP Basic as a temporary fallback. If auth is required but no login method is configured, the server refuses to start.

Unauthenticated routes are limited to `/healthz`, `/login`, `/api/auth/status`, `/auth/google/start`, `/auth/google/callback`, and `/api/auth/logout`.

Signed-in Google users can connect Gmail at `/auth/gmail/start`. The Gmail OAuth flow stores per-user tokens under durable data storage, not in the legacy global Gmail token paths. For hosted per-user Gmail, leave `NDA_GMAIL_INBOUND_TOKEN_PATH` and `NDA_GMAIL_OUTBOUND_TOKEN_PATH` unset so one user's Gmail token cannot become a shared mailbox fallback.

For Render, configure these Google OAuth redirect URIs in the Google Cloud OAuth client and set the matching env vars:

- `https://your-service.onrender.com/auth/google/callback`
- `https://your-service.onrender.com/auth/gmail/callback`

Production Render deployments should use a persistent disk mounted at `/var/data`. `NDA_DATA_DIR` and `NDA_EXPORTS_DIR` must point at durable storage for public deployments because Repository matters include extracted NDA text, uploaded source documents, review results, redline drafts, app settings, and Gmail sync state. The server refuses to start on non-loopback hosts when `NDA_DATA_DIR` is missing or points at ephemeral storage such as `/tmp`, unless `NDA_ALLOW_EPHEMERAL_DATA=true` is set for a short-lived demo.

Authenticated admins can check:

- `/api/deployment/status`: auth, Google identity, allowed host, OAuth redirect, storage, per-user Gmail mode, AI env, Gmail triage, health-check, and rate-limit shape.
- `/api/auth/status`: current login state and public auth configuration.
- `/api/telemetry`: non-sensitive counters such as review requests, export failures, Gmail sync failures, runtime-setting changes, and rate-limit hits.
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

For hosted/private deployments, users connect Gmail through Google OAuth after login. The app stores per-user Gmail tokens under durable data storage and uses the signed-in user's token for inbound sync and outbound sends.

For local shared-token development, configure token files:

```bash
export NDA_GMAIL_INBOUND_TOKEN_PATH=/path/to/inbound-token.json
export NDA_GMAIL_OUTBOUND_TOKEN_PATH=/path/to/outbound-token.json
```

For local development, the app also checks ignored project-local token files:

```text
data/gmail/inbound-token.json
data/gmail/outbound-token.json
```

Inbound sync imports recent `.docx` and text-based `.pdf` attachments with NDA/confidentiality-related subject terms into the Repository for the current owner. Outbound send generates the same Word redline/report used by download/export, opens a confirmation composer, then emails it back to the matter sender only after confirmation.

Gmail remains disabled until a user connects Gmail or legacy token files are configured and readable by the service. Gmail web access and Gmail API access are separate: the browser can still be logged in while the API token is missing, expired, rate-limited, or blocked by quota. The app records recent Gmail sync/send failures and backs off repeated sync attempts when the Gmail API reports a temporary lockout.

## Current Checks

- Mutual NDA obligations.
- Broad Confidential Information definition.
- Standard Confidential Information exclusions, including qualified independent-development carve-outs.
- Approved governing law, defaulting to India, Delaware, England and Wales, or DIFC unless the Playbook is edited.
- Term and ordinary confidentiality survival up to the Playbook cap, defaulting to five years.
- No non-circumvention or substitute-purpose exclusivity.
- Complete execution/signature block.

## Review Output

The backend splits each uploaded document into numbered paragraphs such as `p1`, `p2`, and `p3`. Clause results include backend-identified paragraph evidence, structured evidence records, AI assessment metadata, deterministic comparison metadata where available, reason codes, audit traces, issue labels, fix text, and proposed redlines. The frontend uses backend paragraph IDs for highlighting and clause navigation instead of guessing locally.

DOCX uploads preserve the source Word paragraph index. PDF uploads preserve extracted page metadata. PDF extraction reports basic quality metadata, including page counts, pages without extractable text, extracted character/paragraph counts, repeated header/footer removal, and sparse-extraction warnings.

Repository imports preserve the original uploaded `.docx` so matter exports can generate native Word tracked changes against the source document. PDF matter exports generate a Word review report because PDFs cannot be patched with native Word tracked changes.

## Test

Install the test extras you need:

```bash
python3 -m pip install -e ".[pdf,gmail]"
python3 -m pip install pytest
npm install
```

Run backend tests:

```bash
NDA_ACTIVE_REVIEW_ENGINE=deterministic python3 -m pytest -q
```

Run frontend behavior tests:

```bash
npm run test:frontend:utils
npm run test:frontend
```

Frontend tests run the real app in Chromium and cover repository/matter review loading, review view modes, viewer editing, redline rendering, Gmail/admin surfaces, and DOCX export behavior.

## Roadmap

- Matter activity timeline for imports, reviews, draft saves, exports, sends, stage changes, and Gmail sync events.
- Counsel-labelled evaluation set for comparing AI-first verdicts, deterministic results, and final human-reviewed outcomes.
- Matter search and filtering by counterparty, sender, filename, status, issue, date, stage, and source.
- Review decision memory for recurring counterparty positions.
- Continued Gmail onboarding polish, visual refinement, and recovery guidance.
