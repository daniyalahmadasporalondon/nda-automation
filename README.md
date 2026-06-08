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
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Gmail: one connection, both directions](#gmail-one-connection-both-directions)
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
4. **Deterministic review + comparison** remain available for audit, explicit engine selection, reason-code comparison, and stale-review refresh guards.
5. **Reviewer UI** presents the final clause cards, right-panel analysis, insertable redline options, comments, include/ignore choices, and per-clause reviewed toggles for human sign-off.

The active engine defaults to **AI-first** and **fails closed**: if AI is unavailable, new review creation returns an error instead of silently substituting deterministic results. Admins can switch the active engine at runtime from **Admin → AI** when it is not pinned by environment variables; runtime changes are audit-logged without secrets.

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

**Optional extras**

```bash
python3 -m pip install -e ".[pdf]"        # PDF intake (pypdf / PyMuPDF)
python3 -m pip install -e ".[gmail]"      # Gmail connector (Google API client)
python3 -m pip install -e ".[pdf,gmail]"  # both (what the Render blueprint installs)
```

Without the `pdf` extra, PDF uploads fail with a clear "PDF support is not installed" error.

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
| `NDA_AUTH_USERNAME` / `_PASSWORD` | Optional HTTP Basic fallback. |
| `NDA_RATE_LIMIT_PER_MINUTE` | Request cap for expensive endpoints (`0` for trusted local testing). |
| `NDA_AI_REVIEW_ENABLED` | Enables provider-backed AI review. |
| `OPENROUTER_API_KEY` | Server-side OpenRouter key for review + Gmail attachment selection. |
| `NDA_AI_PROVIDER` / `NDA_AI_MODEL` | `openrouter` / `x-ai/grok-4.3`. |
| `NDA_GMAIL_TRIAGE_MODEL` | Gmail triage model (e.g. `x-ai/grok-4.3`). |
| `NDA_ACTIVE_REVIEW_ENGINE` | Optional pin: `ai_first` or `deterministic`. |
| `NDA_AI_FIRST_REVIEW_ENABLED` | Store AI-first shadow/comparison output. |
| `NDA_ADMIN_USERS` | Comma-separated emails granted admin. |
| `NDA_ALLOW_EPHEMERAL_DATA` | `true` only for short-lived demos on ephemeral storage. |
| `NDA_GMAIL_INBOUND_TOKEN_PATH` / `_OUTBOUND_TOKEN_PATH` | Legacy shared token files for local Gmail; leave unset for hosted per-user Gmail. |

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

For local shared-token development you can still point at token files (`NDA_GMAIL_INBOUND_TOKEN_PATH` / `_OUTBOUND_TOKEN_PATH`, or ignored `data/gmail/{inbound,outbound}-token.json`). For hosted deployments, leave those unset so one user's token never becomes a shared mailbox fallback.

## Deployment

The app needs a Python web service because the static frontend calls API routes served by `nda_automation.server`. This repo ships a Render blueprint (`render.yaml`).

The checked-in blueprint targets a **short-lived free demo**: free plan + ephemeral `/tmp` storage so it boots without a paid disk. Data, sessions, Gmail tokens, matters, drafts, and exports can disappear on restart/redeploy/sleep.

For a real private beta, switch to a paid plan with a persistent disk at `/var/data`, set `NDA_DATA_DIR=/var/data`, `NDA_USERS_PATH=/var/data/users.json`, `NDA_EXPORTS_DIR=/var/data/exports`, and remove `NDA_ALLOW_EPHEMERAL_DATA=true`.

Production start command:

```bash
python -m nda_automation.server --host 0.0.0.0 --port $PORT
```

- Public deployments require authentication. Non-loopback binds auto-require auth; if auth is required but no login method is configured, the server refuses to start.
- Unauthenticated routes are limited to `/healthz`, `/login`, `/api/auth/status`, `/auth/google/start`, `/auth/google/callback`, and `/api/auth/logout`.
- Configure these redirect URIs in the Google Cloud OAuth client (and matching env vars): `.../auth/google/callback` and `.../auth/gmail/callback`.
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
