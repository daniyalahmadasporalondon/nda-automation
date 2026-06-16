# nda-automation

**An AI-first NDA review, redline, and generation workstation.**

aspora's contract desk imports NDAs from manual upload or Gmail, reviews them against a configurable playbook with grounded, fail-closed AI legal assessment, proposes Word tracked-change redlines, generates first-party NDAs from your own signing entities, and sends documents back to counterparties — all from one repository-first workspace.

> Python 3.9+ · stdlib `http.server` backend · vanilla-JS frontend · AI review on **Grok 4.3 via OpenRouter** (`x-ai/grok-4.3`)

---

## Contents

- [What it is](#what-it-is)
- [The app at a glance](#the-app-at-a-glance)
- [Key features](#key-features)
- [How review works](#how-review-works)
- [PDF extraction and memory](#pdf-extraction-and-memory)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Gmail: one connection, both directions](#gmail-one-connection-both-directions)
- [Gmail polling toggle (sync\_enabled)](#gmail-polling-toggle-sync_enabled)
- [Gmail import throttle (import\_limit)](#gmail-import-throttle-import_limit)
- [Gmail processed-message ledger](#gmail-processed-message-ledger)
- [Deployment](#deployment)
- [Security & data](#security--data)
- [Testing](#testing)
- [Project layout](#project-layout)
- [Roadmap](#roadmap)

---

## What it is

The app imports `.docx` Word files and text-based PDFs, stores them as **Repository matters**, and reviews each one against an editable **Playbook**. It returns pass / review / fail clause findings with paragraph-level evidence, AI assessment metadata, deterministic comparison data, proposed fixes, and exportable Word redlines. DOCX matters preserve their original source document so redlines export as **native Word tracked changes**.

The primary flow is:

```
intake (upload / Gmail / generate)  →  AI-first review  →  human confirmation  →  DOCX export / Gmail send
```

Reviews are generated at intake and refreshed when a stored review goes stale. There is no standalone "Review NDA" button — the workstation always operates on a real matter.

## The app at a glance

The product is a single-page app with seven tabs:

| Tab | What it's for |
| --- | --- |
| **Dashboard** | A command center: at-a-glance pipeline counts (Inbox / In Review / Reviewed / Sent), an AI-assisted search bar (deterministic chips → natural-language → counterparty grouping), and in-app toasts when a new inbound NDA arrives. |
| **Generator** | Draft a first-party NDA from one of your signing entities + counterparty details, governing-law options, and term. Output is checked by an independent gen-verify gate before it can be saved. |
| **Repository** | The matter board (Inbox / In Review / Reviewed / Sent / Generated) with stored source documents, redline drafts, and per-matter review views. |
| **Review** | The reviewer workstation: clause checklist, contract-structure view, AI evidence and rationale, in-viewer tracked editing, per-clause reviewed toggles, DOCX export, and confirmed outbound send. |
| **Playbook** | Edit review policy with schema validation, redline-template previews, allowed placeholders, generated governing-law options, version history, and restore. |
| **Admin** | AI runtime + provider/key, Gmail connection + settings, Drive, deployment health, telemetry, and matter backup. |
| **Guide** | Read-only methodology: how structure, reference resolution, concept classification, deterministic validation, and AI-first assessment fit together. |

## Key features

**Intake & repository**
- Import `.docx` and text-based PDFs via manual upload, the Repository, or Gmail.
- Store matters with source documents, review state, redline drafts, and stage tracking; DOCX sources are preserved for native tracked-change export.

**AI-first review**
- Grounded, paragraph-level legal assessment with reason codes, citations, confidence, and review-state decisions.
- An adversarial AI **verifier** second pass that catches polarity/negation mistakes before a verdict is finalized.
- Deterministic review kept available for comparison, explicit fallback, audit metadata, and backend validation.
- Contract-structure parsing, clause/section reference resolution, and legal-concept classification before evaluation.

**Redlines & export**
- Word review reports and source `.docx` redlines with tracked changes; free-form in-viewer edits export as tracked changes too.
- Stale-review guards prevent exporting against changed source text.

**Dashboard & search**
- Pipeline stat cards computed from real matters, click-through to the board.
- AI search bar in phases: deterministic filter chips, natural-language → validated filter spec, and counterparty grouping / document relationships. The AI only parses or summarizes — code runs every query over real data, so result lists are never fabricated.
- In-app notifications when the Gmail scheduler imports a new inbound NDA.

**NDA generation**
- Generate NDAs from a registry of signing entities and approved governing-law jurisdictions, gated by an independent verifier so a generated document is checked against its own playbook before it ships.

**Gmail & Drive**
- One Gmail connection serves both inbound import and outbound send (see below). Save reviewed documents to Google Drive.

**Operations**
- Editable playbook with versioned history, runtime AI controls, deployment status, non-sensitive telemetry, and an auth-gated matter backup.

## How review works

The active review path:

1. **Playbook + document structure** define clause requirements, approved positions, source paragraphs, headings, sections, and references.
2. **AI-first assessment** applies the playbook to the selected source paragraphs and produces the saved clause verdicts, issue types, rationale, citations, confidence, and proposed redlines. Output is held to a **grounding contract**: ungrounded fails are rejected and ungrounded pass/review verdicts are downgraded.
3. **AI verifier** re-checks flagged clauses adversarially (e.g. "shall not be restricted from dealing" is freedom-preserving, not a prohibition) and can rewrite a decision before it is finalized.
4. **Deterministic review + comparison** remain available for audit, generation/internal force-engine checks, reason-code comparison, and stale-review refresh guards.
5. **Reviewer UI** presents the final clause cards, right-panel analysis, insertable redline options, comments, include/ignore choices, and per-clause reviewed toggles for human sign-off.

The active review engine is **AI-first** and **fails closed**: if AI is unavailable, new review creation returns an error instead of silently substituting deterministic results. Admin can view the runtime from **Admin → AI** and deterministic review remains internal-only for explicit generation checks.

## PDF extraction and memory

PDF support requires the `pdf` extra (`pypdf` / `PyMuPDF` / `pdf2docx`). The two libraries play distinct, non-overlapping roles:

| Library | Role |
| --- | --- |
| **pypdf** | Text extraction, paragraph segmentation, geometry (baseline y, font size, indentation). The sole source of clause text sent to the AI reviewer. |
| **PyMuPDF (fitz)** | Visual profile only: detects coloured text, drawings, and embedded images so the UI can decide whether a source preview is needed. Never used for text extraction. |

**Memory behavior in the visual profile.** PyMuPDF's `page.get_text("dict")` with default flags materialises the decoded pixel bytes of every embedded image into the per-page dict — on an image-heavy PDF this single transient dominates the worker's peak RSS (~50 MB for a 3.8 MB media-rich PDF, vs ~0.2 MB without). Because the visual profile only needs image *presence*, not pixel data, the profile strips `TEXT_PRESERVE_IMAGES` from the text-dict flags and counts images via the lightweight `page.get_image_info()` call instead. The result is the same signal at roughly 250× lower peak memory, which keeps inbound PDF reviews well within the 2 GB Render worker. On PyMuPDF builds that lack the expected flag constants the profile degrades gracefully to the default text-dict behaviour rather than crashing.

Text-based PDFs (the common case) produce no images at all and are unaffected by the above. **Scanned PDFs** (no embedded text layer) are rejected at intake with "No readable text was found in the PDF" — OCR is not currently wired in.

## Quick start

Requires **Python 3.9+**. DOCX review and export work out of the box (`python-docx` is a core dependency); PDF and Gmail are optional extras.

```bash
# 1. Install (with optional PDF + Gmail support)
python3 -m pip install -e ".[pdf,gmail]"

# 2. Configure local credentials
cp .env.example .env          # then fill in keys; .env is gitignored
set -a; source .env; set +a

# 3. Run
python3 -m nda_automation.server --port 8787
```

Then open <http://127.0.0.1:8787>.

Minimum `.env` to enable AI review:

```bash
NDA_AI_REVIEW_ENABLED=true
NDA_AI_PROVIDER=openrouter
NDA_AI_MODEL=x-ai/grok-4.3
OPENROUTER_API_KEY="your-openrouter-api-key"
```

You can also paste/save the OpenRouter key from **Admin → AI** after the app is running; saved keys are stored under ignored app data and are never returned to the browser. Connect Gmail and Drive from **Admin** after signing in with Google.
The optional adversarial verifier uses the same OpenRouter key but is an independent second-opinion pass; set `NDA_AI_VERIFIER=true` and `NDA_AI_VERIFIER_MODEL=deepseek/deepseek-v4-pro` to run it with DeepSeek V4 Pro while keeping the main reviewer on Grok.

**Optional extras**

```bash
python3 -m pip install -e ".[pdf]"        # PDF intake + PDF-to-DOCX reconstruction (pypdf / PyMuPDF / pdf2docx)
python3 -m pip install -e ".[gmail]"      # Gmail connector (Google API client)
python3 -m pip install -e ".[pdf,gmail]"  # both (what the Render blueprint installs)
```

Without the `pdf` extra, PDF uploads fail with a clear "PDF support is not installed" error. PDF-to-DOCX
Word reconstruction is also unavailable without `pdf2docx`; download contracts and export routes report that
explicitly instead of serving extracted text as a fake Word conversion.

## Configuration

Common environment variables:

| Variable | Purpose |
| --- | --- |
| `NDA_DATA_DIR` | Matter records, source uploads, app settings, Gmail sync state. |
| `NDA_EXPORTS_DIR` | Persisted export downloads. |
| `NDA_USERS_PATH` | User/session/sync-history storage (e.g. `/var/data/users.json` in prod). |
| `NDA_REQUIRE_AUTH` | `true` to require login (auto-required on non-loopback binds). |
| `NDA_ALLOWED_HOSTS` | Comma-separated allowed request hostnames (e.g. your Render host). |
| `NDA_GOOGLE_OAUTH_CLIENT_ID` / `_SECRET` | Google login credentials for per-user identity. |
| `NDA_GOOGLE_OAUTH_REDIRECT_URI` | Fixed redirect, e.g. `https://your-service.onrender.com/auth/google/callback`. |
| `NDA_GMAIL_OAUTH_REDIRECT_URI` | Fixed Gmail redirect, e.g. `.../auth/gmail/callback`. |
| `NDA_DOCUSIGN_CLIENT_ID` / `NDA_DOCUSIGN_CLIENT_SECRET` | DocuSign **integration key** (OAuth client id) + secret key (Apps & Keys). Required for "Connect DocuSign". |
| `NDA_DOCUSIGN_OAUTH_REDIRECT_URI` | Fixed DocuSign redirect, e.g. `.../auth/docusign/callback`. Must match the URI registered on the integration key. |
| `NDA_DOCUSIGN_AUTH_SERVER` | `demo` (default → `account-d.docusign.com`, free dev accounts) or `production` (`account.docusign.com`). |
| `NDA_DOCUSIGN_CONNECT_HMAC_KEY` | Optional. DocuSign Connect webhook HMAC secret; when set, the `/api/docusign/webhook` callback is signature-verified. |
| `NDA_AUTH_USERNAME` / `_PASSWORD` | Optional HTTP Basic fallback. |
| `NDA_RATE_LIMIT_PER_MINUTE` | Request cap for expensive endpoints (`0` for trusted local testing). |
| `NDA_AI_REVIEW_ENABLED` | Enables provider-backed AI review. |
| `OPENROUTER_API_KEY` | Server-side OpenRouter key for review + Gmail attachment selection. |
| `NDA_AI_PROVIDER` / `NDA_AI_MODEL` | `openrouter` / `x-ai/grok-4.3`. |
| `NDA_AI_VERIFIER` / `NDA_AI_VERIFIER_MODEL` | Optional independent adversarial verifier; default model is `deepseek/deepseek-v4-pro` and it uses the same OpenRouter key. |
| `NDA_GMAIL_IMPORT_LIMIT` | Env-level default for the per-poll new-message cap (default `20`, hard ceiling `40`). The Admin → Gmail panel exposes an `import_limit` setting that overrides this at runtime without a redeploy. See [Gmail import throttle (import\_limit)](#gmail-import-throttle-import_limit). |
| `NDA_GMAIL_TRIAGE_MODEL` | Gmail attachment-selector model (picks the NDA attachment, e.g. `x-ai/grok-4.3`). |
| `NDA_GMAIL_INTAKE_MODEL` | Gmail NDA-intake classifier model; default `deepseek/deepseek-v4-flash`. Optional and reuses `OPENROUTER_API_KEY`; with no key it falls back to the deterministic intake gate. |
| `NDA_ACTIVE_REVIEW_ENGINE` | Review runtime pin; inbound review resolves to `ai_first`. Deterministic remains internal-only for explicit generation `force_engine` paths. |
| `NDA_AI_FIRST_REVIEW_ENABLED` | Store AI-first shadow/comparison output. |
| `NDA_ADMIN_USERS` | Comma-separated emails granted admin. |
| `NDA_ALLOW_EPHEMERAL_DATA` | `true` only for short-lived demos on ephemeral storage. |
| `NDA_GMAIL_INBOUND_TOKEN_PATH` / `_OUTBOUND_TOKEN_PATH` | Legacy shared token files for local Gmail; leave unset for hosted per-user Gmail. |
| `NDA_GMAIL_SERVER_INBOUND` | Opt-in (`true`/`1`) for the legacy server-level inbound token fallback. When **no** user has connected Gmail, the scheduler runs the shared/env token only if this is set. Default off ⇒ disconnecting the last account stops the scheduled inbound sync. |

**Optional semantic fallback** — a lazy-loaded callable invoked only when configured:

```bash
export NDA_SEMANTIC_EVALUATOR=module.path[:callable_name]
```

It receives `text`, `normalized`, `clause`, `paragraphs`, and `current_result`, and returns `None` or a small decision dict like `{"status": "match", "reason": "...", "matched_paragraph_ids": ["p1"]}`.

## Gmail: one connection, both directions

Inbound import and outbound send share a **single Gmail login**. One **Connect Gmail** grants `gmail.readonly + gmail.send + gmail.metadata` in a single consent, and the backend saves both role tokens from that one grant (each narrowed to its own scopes on refresh). Admin exposes **one** Connect/Disconnect action and **one** on/off toggle; `drive.file` stays a separate connection.

- A server-side scheduler imports recent `.docx`/`.pdf` NDA attachments (matched by subject/content terms) into the Repository on a configurable cadence (Admin → Email).
- Outbound send generates the same Word redline/report used by export, opens a confirmation composer, and emails it back to the matter sender **only after the operator confirms the exact recipient address** — guarding against a spoofed `Reply-To` redirecting a redline.
- Gmail web access and Gmail API access are separate: the browser can be logged in while the API token is missing, expired, or rate-limited. The app records recent sync/send failures and backs off after a temporary lockout.

For local shared-token development you can still point at token files (`NDA_GMAIL_INBOUND_TOKEN_PATH` / `_OUTBOUND_TOKEN_PATH`, or ignored `data/gmail/{inbound,outbound}-token.json`). For hosted deployments, leave those unset so one user's token never becomes a shared mailbox fallback. When no user is connected, the scheduled inbound sync polls a server/env token **only** if `NDA_GMAIL_SERVER_INBOUND` is explicitly enabled — so **Disconnect Gmail** (which removes the last user's token) actually stops the scheduled inbound sync rather than silently falling back to a leftover shared token.

## Gmail polling toggle (sync_enabled)

> **Note to reconciler:** `sync_enabled` is documented here from the feature spec. The backend setting name and the Admin → Gmail panel UI for it live in `feature/gmail-sync-controls-backend` and `feature/gmail-sync-controls-frontend`, which are not yet committed. Reconcile the setting name, default, and API field against the code before shipping this doc.

The Admin → Gmail panel exposes a **polling toggle** (`sync_enabled`) that pauses or resumes the scheduled inbound sync without disconnecting the Gmail account.

**Why this matters.** The old way to stop polling was to click **Disconnect Gmail**, which also revoked the OAuth token. Reconnecting required a fresh OAuth consent and triggered a catch-up burst at the next poll. The `sync_enabled` toggle keeps the token intact and simply stops the scheduler from running the Gmail API calls — no re-consent, no burst.

**How the scheduler obeys it.** On every tick the Gmail sync scheduler reads the persisted Gmail settings before deciding whether to run. When `sync_enabled` is `false`, the scheduler skips the poll entirely and sleeps until the next tick. The current tick's scheduled interval and the sync-frequency setting are unchanged; the toggle only gates whether actual Gmail API work happens.

**Operator workflow — pause and resume:**

1. Open **Admin → Gmail** (top-right admin menu → Email).
2. Find the **Polling** control. Click **Pause** (or the toggle) to set `sync_enabled = false`. The panel reflects the paused state immediately and no further scheduled polls execute.
3. To resume, click **Resume** (or the toggle again). The scheduler picks up on the next scheduled tick; no sync runs retroactively for the paused window.

**Emergency stop vs. polling pause.** The global kill-switch `NDA_INBOUND_AI_REVIEW_ENABLED=false` (env var, requires redeploy) disables the AI review worker but does not stop the Gmail poll from importing messages. `sync_enabled = false` stops the poll itself. For a complete stop during an incident, set both.

**Relationship to Disconnect.** Disconnecting Gmail (`/api/gmail/disconnect`) removes the OAuth token and also resets `inbound_enabled` / `outbound_enabled` to their defaults. The `sync_enabled` toggle does not touch the token; it is the right tool for operational pauses (e.g. maintenance window, cost control) where you intend to resume polling soon.

## Gmail import throttle (import_limit)

> **Note to reconciler:** The admin-UI `import_limit` field (runtime-settable from the Gmail panel without a redeploy) is documented here from the feature spec. That field lives in `feature/gmail-sync-controls-backend` / `-frontend`, which are not yet committed. Reconcile the `import_limit` setting name, valid range, and API field name against the code. The env-default and clamp behaviour described below are already live in the base branch.

`NDA_GMAIL_IMPORT_LIMIT` (default `20`, hard ceiling `40`) sets the env-level default for the number of **new** inbound messages the scheduler hands to the heavy import path per poll cycle. "Heavy import" means: Pro-model attachment selection, Flash intake classification, PyMuPDF visual profiling, and attachment download + text extraction — the work that dominates the 2 GB Render worker's peak RSS.

**Runtime override via the Admin panel.** The Admin → Gmail panel exposes an `import_limit` field (integer, 1–40) that operators can adjust at runtime without a redeploy. The running server reads this value from the persisted Gmail settings on each poll. The env var `NDA_GMAIL_IMPORT_LIMIT` sets the initial default; the Admin field then overrides it. Both paths enforce the same hard ceiling of `40` to keep per-poll Gmail API quota consumption within the ~6,000 quota-units-per-minute budget.

**Why the ceiling is 40.** Each `messages.get()` call costs ~5 Gmail quota units. A per-poll new-work batch of 40 messages drives at most ~200 units against the per-user per-minute budget of ~6,000 — leaving headroom for list probes and retries. Values above 40 are silently clamped; setting `NDA_GMAIL_IMPORT_LIMIT=60` or submitting `import_limit: 60` via the Admin API both result in an effective limit of `40`.

**Why it matters on (re)connect.** Gmail's inbound query has no already-imported exclusion and applies no label or archive, so the first poll after connecting (or reconnecting) would otherwise attempt to catch up the full 90-day backlog in one burst — a one-time spike that can OOM the worker. `NDA_GMAIL_IMPORT_LIMIT` keeps each burst small.

**How catch-up still makes progress.** The [processed-message ledger](#gmail-processed-message-ledger) records each imported message's `internalDate` as a persistent drain cursor. Already-imported messages are skipped without counting against the limit, so each subsequent poll advances to the next un-imported (older) batch until the backlog is drained.

```
NDA_GMAIL_IMPORT_LIMIT=20   # default: gentle catch-up, ~20 new NDAs per poll cycle
                             # hard ceiling 40; raise via Admin → Gmail → Import limit
```

## Gmail processed-message ledger

The scheduler maintains a **persistent per-owner drain cursor** — a low-water-mark on Gmail's server-assigned `internalDate` (epoch milliseconds) for the oldest message the catch-up scan has reached. It is stored in `$NDA_DATA_DIR/gmail_inbound_cursors.json`.

**What problem this solves.** Gmail's inbound query (`in:inbox`) has no server-side exclusion for already-imported messages (the integration holds only the `gmail.readonly` scope, so it cannot apply a label or archive). The same newest messages re-surface on every poll. Without the cursor, once the already-imported prefix exceeded the scan window the scheduler would exhaust its entire per-poll budget inside already-imported messages, find zero new work, and stall permanently — silently dropping the remainder of the backlog until messages aged out at 90 days.

**How the cursor works.** Each poll runs two bounded passes sharing a single `import_limit` new-work budget:

1. **Head pass** — a small window over the base query (newest-first) to ingest mail that arrived since the last poll.
2. **Drain pass** — the same base query date-bounded `before:<cursor>` so the already-drained newest prefix never re-surfaces. The cursor descends to the oldest message examined, and the next poll resumes right below it.

Once the backlog is fully drained, the cursor resets and future polls run head-only. Forward progress is guaranteed: each drain poll advances the cursor to an older message or exhausts the backlog.

**Dedup guard before AI triage.** Independently of the drain cursor, each candidate message is checked against a per-import dedup index (`message_attachments_all_already_imported`) **before** any AI triage or intake calls. Messages already present in the Repository are skipped without triggering the Pro attachment-selector, the Flash intake classifier, or any PDF extraction. This prevents re-classification cost even if the cursor resets or a message re-surfaces through a different query path.

**Telemetry counters.** The following counters are exposed via the observability endpoint and the telemetry health summary:

| Counter | What it counts |
|---|---|
| `inbound_ai_review_skipped_already_reviewed` | Inbound AI review jobs skipped because the matter already has an AI review result (avoids redundant re-reviews). |
| `inbound_ai_review_scheduled` | (*Planned — not yet in base branch; will be emitted by `feature/gmail-processed-ledger`.*) Messages that passed the dedup check and were handed to the AI review queue. |

**Durability.** `gmail_inbound_cursors.json` is stored under `NDA_DATA_DIR`, which on Render Standard is a mounted persistent disk (`/var/data`). On ephemeral storage the cursor resets on each restart, but the dedup index (`matters.json`) also persists there, so already-imported messages are still skipped via the dedup guard even without a cursor.

## DocuSign: send for signature (real e-signature)

After a matter is approved, the finalized NDA can be sent for signature through the **real DocuSign eSignature API** — the user clicks **Connect DocuSign**, completes a real DocuSign login (OAuth Authorization Code Grant), and from then on envelopes, status, the executed PDF and voids are all live DocuSign calls. There is no simulated/demo client in the running app; the only test double lives in the unit tests.

By default both signers are added at the **same routing order (parallel signing)** — either side can sign in any order. Pass `signing_order: "sequential"` in the request to enforce a sequence.

### Free developer-account quickstart

The auth server defaults to the **DocuSign demo environment** (`account-d.docusign.com` for auth; the eSignature API base URI, e.g. `https://demo.docusign.net`, is resolved per-account from `/oauth/userinfo`), so a **free DocuSign developer account works out of the box**. Demo-account envelopes are watermarked but fully functional end to end.

1. Create a **free DocuSign developer account** at <https://developers.docusign.com>.
2. In **Apps & Keys**, create an **integration key** (this is your OAuth client id) and generate a **secret key**. Add your **redirect URI** (e.g. `https://your-host/auth/docusign/callback`) to the integration key's list of redirect URIs.
3. Set the env vars:
   ```bash
   export NDA_DOCUSIGN_CLIENT_ID=<integration key>
   export NDA_DOCUSIGN_CLIENT_SECRET=<secret key>
   export NDA_DOCUSIGN_OAUTH_REDIRECT_URI=https://your-host/auth/docusign/callback
   export NDA_DOCUSIGN_AUTH_SERVER=demo            # default; flip to `production` for live
   # optional, recommended when you wire the Connect webhook:
   export NDA_DOCUSIGN_CONNECT_HMAC_KEY=<connect hmac secret>
   ```
4. Restart the app, click **Connect DocuSign**, complete the real DocuSign login, then send a matter for signature → a real (demo-watermarked) envelope is created.

**Going to production:** flip `NDA_DOCUSIGN_AUTH_SERVER=production` (uses `account.docusign.com`; the live API base URI is again resolved per-account from userinfo), promote your integration key through DocuSign's Go-Live review, and reconnect.

### Endpoints

| Method + path | Purpose |
| --- | --- |
| `GET /api/docusign/status` | Connection state: `connected`, `configured`, `production`, `account_label`. |
| `POST /api/docusign/connect` | Start real OAuth; returns `{authorization_url}` to redirect to. |
| `GET /auth/docusign/callback` | OAuth callback: exchanges the code, resolves account id + base URI, stores the token. |
| `POST /api/docusign/disconnect` | Removes the signed-in user's DocuSign token. |
| `POST /api/matters/<id>/send-for-signature` | Body `{signers?, signing_order?}` → creates + sends a real envelope; returns `{envelope_id, status}`. |
| `GET /api/matters/<id>/signature-status` | Live envelope status; on `completed` captures the executed PDF as the matter's `signed` artifact. |
| `GET /api/matters/<id>/signed-document` | Downloads the executed combined PDF. |
| `POST /api/docusign/webhook` | DocuSign Connect callback (HMAC-verified when a key is set); on `completed` stores the signed artifact and marks the matter fully signed. |

> **Note:** the integration is built and unit-tested against fakes, but the final live click-login → real-envelope round trip requires your DocuSign developer credentials and is the user's verification step.

## Deployment

The app needs a Python web service because the static frontend calls API routes served by `nda_automation.server`. This repo ships a Render blueprint (`render.yaml`).

The Render blueprint uses the repo `Dockerfile` instead of the native Python runtime so the Review faithful-source preview can install system rendering dependencies: LibreOffice for DOCX-to-PDF conversion, fontconfig, and metric-compatible Calibri/Cambria substitutes (Carlito/Caladea) plus Liberation/Noto/DejaVu fonts. Without those OS packages, DOCX page-image previews and PDF exports fall back to an unavailable-state message instead of silently flattening layout.

The checked-in blueprint targets a **short-lived free demo**: free plan + ephemeral `/tmp` storage so it boots without a paid disk. Data, sessions, Gmail tokens, matters, drafts, and exports can disappear on restart/redeploy/sleep.

For a real private beta, switch to a paid plan with a persistent disk at `/var/data`, set `NDA_DATA_DIR=/var/data`, `NDA_USERS_PATH=/var/data/users.json`, `NDA_EXPORTS_DIR=/var/data/exports`, and remove `NDA_ALLOW_EPHEMERAL_DATA=true`.

Production start command:

```bash
python -m nda_automation.server --host 0.0.0.0 --port $PORT
```

- Public deployments require authentication. Non-loopback binds auto-require auth; if auth is required but no login method is configured, the server refuses to start.
- Unauthenticated routes are limited to `/healthz`, `/login`, `/api/auth/status`, `/auth/google/start`, `/auth/google/callback`, `/api/auth/logout`, and the DocuSign Connect webhook `/api/docusign/webhook` (a server-to-server callback authenticated by its HMAC signature, not a session).
- Configure these redirect URIs in the Google Cloud OAuth client (and matching env vars): `.../auth/google/callback` and `.../auth/gmail/callback`. Configure `.../auth/docusign/callback` on the DocuSign integration key.
- The server refuses to start on non-loopback hosts when `NDA_DATA_DIR` is missing or points at ephemeral storage, unless `NDA_ALLOW_EPHEMERAL_DATA=true`.

Authenticated admins can inspect `/api/deployment/status`, `/api/auth/status`, `/api/telemetry`, and download `/api/matters/export` (metadata + stored-document manifest; no embedded source bytes).

## Security & data

- Matter list/detail responses expose **metadata only**; extracted NDA text, review results, and redline drafts are returned only through the auth-gated review workflow.
- **Outbound recipient confirmation** is required before any send (the resolved address must match the operator-confirmed one).
- **CSRF/Origin** checks and proxy-aware **rate limiting** protect expensive review, upload, export, send, and backup endpoints.
- **Prompt-injection defenses**: untrusted email/attachment text is neutralized (control chars stripped, line-start role markers defanged) before reaching any AI prompt, and the Gmail attachment selector intersects model output against a candidate-id allow-list so a selector LLM can't be steered.
- Atomic JSON saves `fsync` file contents and parent directories; DOCX XML parsing rejects unsupported DTD/entity declarations and strips invalid XML characters.
- Uploaded source documents and matter backups are sensitive legal work product — protect them accordingly.

## Testing

```bash
python3 -m pip install -e ".[pdf,gmail]" && python3 -m pip install pytest && npm install
```

```bash
# Backend (unit + eval gate)
python3 -m pytest -q

# Frontend (pure modules + real-app Playwright behavior suite)
npm run test:frontend:utils
npm run test:frontend

# Lint
ruff check nda_automation tests
```

The Playwright suite runs the real app in Chromium and covers repository/matter loading, review view modes, viewer editing, redline rendering, Gmail/admin surfaces, dashboard search, notifications, and DOCX export. The backend suite includes a counsel-style **eval gate** over review cases.

## Project layout

```
nda_automation/            # Python backend (stdlib http.server)
├── server.py              # HTTP server, routing, auth, Gmail sync scheduler
├── routes/                # Request handlers: matters, review, generation,
│                          #   gmail, drive, playbook, admin, auth, dashboard, …
├── ai_*.py                # AI review pipeline: assessor, contract, prompt,
│                          #   verifier, first-review, grounding
├── checker.py, playbook_rules.py, prohibited_positions.py   # deterministic engine
├── contract_structure.py, reference_resolver.py, concept_classifier.py  # structure layer
├── matter_store.py, matter_repository.py, artifact_registry.py, workflow.py  # data model
├── gmail_integration.py, drive_integration.py, google_identity.py  # integrations
├── nda_generation*.py, entity_registry.py     # NDA generation + signing entities
├── docx_*.py, pdf_*.py, redline_*.py, *_export.py  # document I/O, redlines, export
├── csrf.py, rate_limit.py, http_auth.py, untrusted_text.py  # security
└── dashboard_search_intent.py, matter_summary.py, telemetry.py  # dashboard + ops
static/                    # Vanilla-JS frontend (no build step)
├── index.html, app.js, styles.css
├── js/                    # Controllers: repository, review-workstation, admin-*,
│                          #   dashboard-search, notifications, auth-session, …
└── js/modules/            # Pure, unit-tested ES modules
tests/                     # pytest (backend) + Playwright/.mjs (frontend)
docs/                      # Methodology and design notes
playbook.json              # Tracked review policy (runtime/history are derived + gitignored)
render.yaml                # Render deployment blueprint
```

## Roadmap

- Matter activity timeline across imports, reviews, draft saves, exports, sends, stage changes, and Gmail sync events.
- Counsel-labelled evaluation set comparing AI-first verdicts, deterministic results, and final human-reviewed outcomes.
- Review decision memory for recurring counterparty positions.
- Continued visual modernization across Repository, Review, and Admin, plus Gmail onboarding and recovery guidance.

---

_aspora — internal NDA review tooling. Not legal advice._
