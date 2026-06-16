# Overview Tab — Review Workstation

Build-ready spec for a new **Overview** tab in the Review workstation inspector
(the right-hand `<aside class="studio-playbook">` panel inside `#reviewView`).

Status: spec only. This document describes the feature and a collision-minimizing
file/task map. It does NOT implement anything.

Base commit: `2947b42` (branch `spec/overview-tab`).

---

## 1. The design (converged)

### 1.1 Tab change

The Review workstation inspector currently has three sub-tabs, defined in
`static/index.html` at the `.studio-inspector-tabs` tablist (≈ lines 604–608):

```
[ Clause ] [ Structure ] [ Fill ]
```

Change to:

```
[ Overview ] [ Clause ] [ Structure ]
```

- **Rename** the existing `fill` inspector tab → **Overview**, and **move it
  first**, before `Clause` and `Structure`.
- The old "Fill" tab's *function* (insert/replace Aspora entity legal-name +
  registered-address into the document — `static/js/review-fill.js`) is **not
  deleted**; it is **absorbed into the Overview tab** as the entity-name fill
  control at the top of the Overview body (section 1.2 item 1). The "Fill" label
  disappears; the capability lives on inside Overview.

The inspector-tab machinery is entirely id/`data-review-inspector`-driven and
already generic, so the rename + reorder is a config change, not new plumbing:

- `static/app.js` line 83: `const REVIEW_INSPECTOR_VIEWS = ["clause", "structure", "fill"];`
- `static/app.js` lines 84–88: `REVIEW_INSPECTOR_TITLES = { clause, structure, fill }`
- `static/app.js` `normalizeReviewInspectorView()` / `setReviewInspectorView()` /
  `updateReviewInspectorTabs()` (≈ lines 1857–1874) — no per-tab logic; they
  iterate `[data-review-inspector]`.
- `static/js/review-workstation-rendering.js` `renderStudioDetail()` (≈ line 1420)
  and `renderStudioEmpty()` (≈ line 112) dispatch on `state.reviewInspectorView`:
  `"structure"` → `reviewStructureController.render()`, `"fill"` →
  `reviewFillController.render()`, else clause detail.

> **Decision — keep the internal view key `"fill"` or rename to `"overview"`?**
> Rename to `"overview"` for clarity. It is a small, contained rename (the value
> appears in `REVIEW_INSPECTOR_VIEWS`, `REVIEW_INSPECTOR_TITLES`, the two
> dispatch `=== "fill"` checks, the `data-review-inspector="fill"` attribute, and
> `review-fill.js`'s own `state.reviewInspectorView === "fill"` guards). All
> touch-points are listed in Task T1. Default selected view becomes `"overview"`
> (today `normalizeReviewInspectorView` falls back to `"clause"`; the new default
> for a freshly loaded matter should be `"overview"`).

### 1.2 Overview tab contents (top → bottom)

The Overview body renders into `#studioDetailPanel` (the same panel the Clause /
Structure / Fill views paint into today), via a new `reviewOverviewController`.

1. **Counterparty block.** Counterparty name + the Confirm / "Unconfirmed"
   control + the entity-name fill control.
   - Reuse the existing counterparty-confirmation surface
     (`#studioCounterpartyField` and the `renderCounterpartyConfirmation` /
     `submitCounterpartyOverride` logic in
     `static/js/review-workstation-source.js`). Today that field lives in the
     `#reviewView` header (`.studio-matter-card`, index.html ≈ lines 430–448).
     It **moves into the Overview body**. See §1.3 (header collapse) for what
     stays in the header.
   - The entity-name **fill** control is the absorbed Fill tab: the entity picker
     + "Insert" / "Replace" actions from `static/js/review-fill.js`. This stays a
     self-contained widget; Overview hosts it under the counterparty block.

2. **Matter-facts strip.** A compact horizontal strip:
   `governing law · term · received date`.
   - This is the content that currently collides with the document title in the
     header (`.studio-matter-meta` + the manifest summary). Move it into Overview.
   - Data sources (§4): governing law and term from the matter manifest /
     `governing_law` clause result; received date from
     `received_at || imported_at || created_at || updated_at`.

3. **Clause ROSTER (problems-first).** A vertical list. Each row:
   - **Clause name** (`clauseDisplayName(clause)`).
   - **AI verdict pill** — Pass / Needs Review / Fail, color-coded
     green / amber / red. This is the **AI/backend verdict**, derived **only**
     via `clauseStatus(clause)` (the canonical single-source-of-truth in
     `static/js/modules/clause-status.mjs`). Map:
     `status.passes` → Pass (green), `status.needsReview` → Needs Review (amber),
     `status.fails` → Fail (red), `idle` → Pending (neutral).
   - **Human "reviewed" check** — a separate control/indicator showing whether a
     human has signed off this clause (the "mark reviewed" state). This is a
     **distinct state from the AI verdict** (see §1.4). Toggling it reuses the
     existing `markMatterReviewed({ clauseId })` action
     (`static/js/review-workstation-actions.js` ≈ line 273) /
     `clauseReviewAcknowledged(clauseId)`
     (`static/js/review-workstation-rendering.js` ≈ line 329).
   - **Click-to-jump** — clicking the row selects that clause and switches to the
     **Clause** tab. Reuse `selectReviewClause(clause.id, { jump: true })`
     (`static/js/review-workstation-viewer.js` ≈ line 622), which already sets
     `state.selectedReviewClauseId`, flips `reviewInspectorView` to `"clause"`
     (via `nextClauseSelectionState`), re-renders, and scrolls the document to the
     clause. The roster row's "reviewed" toggle and verdict pill must
     `stopPropagation()` so a click on them does not also trigger the row jump.
   - **Selected-clause highlight** — the row matching
     `state.selectedReviewClauseId` gets a `selected`/`is-selected` modifier
     (mirror the `.studio-clause-item.selected` pattern in the existing lane).
   - **Sort order** — problems first: **Fail**, then **Needs Review**, then
     **Pass** (then Pending). Stable within each band (preserve the clause list's
     natural order inside a band). Sort key derived from `clauseStatus`:
     `fails → 0, needsReview → 1, passes → 2, idle → 3`.

4. **Progress line.** `"2 of 4 clauses reviewed"` plus an optional thin fill bar.
   - This counts **human review sign-offs** (the "mark reviewed" action), **NOT**
     the AI verdict. Denominator = total clauses in the roster
     (`getClauseTotal(clauses)` / `state.reviewClauses.length`); numerator =
     count of clauses where `clauseReviewAcknowledged(clause.id)` is true.
   - This is the gate for Approve (§1.2 item 6 / §1.4).
   - ⚠️ **Semantics change to flag for the backend-data teammate / product**: the
     *current* human-review model only tracks **needs-review** clauses
     (`reviewClauseIds()` filters to `status.needsReview`; `humanReviewAcknowledged()`
     requires every needs-review clause acknowledged; `markMatterReviewed` only
     touches needs-review clause ids). The new Overview progress line and Approve
     gate are specified to count **all** clauses. Two options — pick one in the
     build kickoff and write it into Task T3/T4:
     - **(A) Count all clauses** (literal reading of the design: "2 of 4"). A
       passing clause is "reviewed" the moment the human ticks it; the roster lets
       a human tick any clause, not just needs-review ones. This requires
       extending `markMatterReviewed` / `clauseReviewAcknowledged` /
       `reviewClauseIds` to operate over all clauses (today the acknowledged-map
       default for an untracked clause is `state.selectedMatter.human_reviewed`,
       which would make passing clauses show as reviewed once the matter is
       globally reviewed — acceptable, but confirm).
     - **(B) Count needs-attention clauses only** (denominator = fail + needs-review
       clauses; "2 of 2 reviewed"). Smaller blast radius, matches today's gate
       exactly, but the displayed denominator no longer matches total clause count.
     - **Recommendation: (A)**, because the design text and the Approve gate text
       ("2 of 4 reviewed") both quote the full clause count. The roster's per-row
       "reviewed" check on *every* row (not just amber/red) is the design's intent.

5. **Empty state.** If no AI review has run yet
   (`!hasReviewResults()` — `static/js/review-workstation-rendering.js` ≈ line 397):
   show **"No review yet"** + a **"Refresh with AI"** button.
   - The button reuses the already-built explicit-refresh path: the
     `#studioRefreshReviewButton` handler (`review-workstation-actions.js` ≈ line
     49 / 829), which POSTs `/api/matters/{id}/review-refresh` and reloads. The
     Overview empty-state button should call the same handler function (extract it
     to a named `refreshSelectedReview()` if it is an inline arrow today) rather
     than duplicate the fetch.

6. **Footer.**
   - **Approve Review** button — greyed until the gate passes, **with the gate
     reason shown** (e.g. `"1 clause still fails · 2 of 4 reviewed"`). Reuse the
     existing `#studioApproveReviewButton` +
     `updateApproveReviewControl()` / `approveBlockReasons()` /
     `renderApproveBlockReasons()` (`review-workstation-actions.js` ≈ lines
     1155–1216) and `approveSelectedReview()` (≈ line 1218, POSTs
     `/api/matters/{id}/approve`).
     - ⚠️ Today the only client-side block reason is `stale_playbook`; per-clause
       review does **not** gate Approve. The design requires the gate to also be
       **"all clauses reviewed"**. Add a client block reason (e.g.
       `human_review_incomplete`) to `approveBlockReasons()` when the progress line
       is not complete, and render its human label
       ("N of M clauses reviewed" / "K clause(s) still fail"). Keep
       `state.approveServerBlocks` union intact so the server stays authoritative.
       Coordinate the exact server-side gate codes with the backend-data teammate
       (§4) — the client predictor must not *under*state what the server rejects.
   - **Send for signature** button — reuse `#studioSendForSignatureButton` +
     `syncDocuSignTriggerButton()` (DocuSign trigger already wired in
     `review-workstation-rendering.js` ≈ line 184 and `docusign-send.js`). Move /
     mirror it into the Overview footer.

### 1.3 Header collapse (consequence)

Once counterparty, the matter-facts strip, and the matter actions move into the
Overview tab, the `#reviewView` header (`.studio-matter-card`, index.html ≈ lines
411–460) collapses to **essentially just the document title** (`#studioDocTitle`)
plus the back affordance (`.back-dot`).

What moves OUT of the header into Overview:
- `#studioCounterpartyField` (whole counterparty-confirmation block) → Overview §1.2(1).
- `.studio-matter-meta` (`Counterparty` / `Received` dl) → folded into the
  Overview matter-facts strip §1.2(2). The hidden `#studioCounterpartyMeta`
  email-meta node can be retired or kept hidden.
- `.studio-matter-actions` (Refresh / Reviewed / Approve / Send-for-signature /
  Save-Draft / Reset / Clear) → Approve + Send for signature go to the Overview
  footer §1.2(6). **Save Draft / Reset Draft / Clear** are document-editor
  controls, not review-summary controls — leave them where they make sense
  (keep in the header or move to the doc toolbar; not part of Overview). Confirm
  placement in kickoff; they are out of scope for the Overview footer.

What STAYS in the header: `#studioDocTitle`, `.back-dot`.

> Keep every moved node's **id** stable. The existing handlers in
> `review-workstation-actions.js` / `-source.js` query by id
> (`#studioApproveReviewButton`, `#studioCounterpartyConfirmButton`, etc.). Moving
> a node in the DOM while preserving its id means the existing wiring keeps
> working with zero JS churn. **Do not duplicate ids.** This is the single biggest
> lever for keeping the build low-risk.

### 1.4 AI verdict vs. human "reviewed" — preserve the distinction

Two orthogonal states per clause, both shown on the roster row:

| State | Meaning | Source of truth | Values |
|---|---|---|---|
| **AI verdict** | What the engine/AI decided about the clause | `clauseStatus(clause)` over backend `clause.review_state` / `clause.decision` | Pass / Needs Review / Fail / Pending |
| **Human reviewed** | A human signed off on this clause | `clauseReviewAcknowledged(clause.id)` / `markMatterReviewed` | reviewed / not reviewed |

The progress line and the Approve gate count **human reviewed**, never the AI
verdict. The verdict pill colors the row; the "reviewed" check is independent and
can be ticked on a Pass clause (a Pass that a human has *also* confirmed). Never
collapse the two into one control.

---

## 2. Concrete file / component layout (collision-minimized)

### 2.1 The two unavoidable shared files

These two files are touched by *almost every* sub-task. Single-owner them and
sequence everything else around them (see Task map §3):

- **`static/index.html`** — the inspector tablist + the `#reviewView` header +
  the `<script>` include list (and its `?v=` cache-busts). Owned by **T1**.
- **`static/styles.css`** — all Overview styling. To avoid N agents editing one
  ~218KB file, put the Overview CSS in a **new stylesheet**
  `static/css/overview.css` (owned by **T6**), linked once from `index.html`
  `<head>`. Only minimal, unavoidable edits to `styles.css` (e.g. a header layout
  tweak after nodes leave it) — keep those in T1's header task or T6, not scattered.

### 2.2 New files (prefer new component files over editing shared ones)

| New file | Purpose | Owner |
|---|---|---|
| `static/js/review-overview.js` | `createReviewOverviewController({ state, ... })` → `{ render() }`. Paints the whole Overview body into `#studioDetailPanel`: counterparty block (delegates to existing source.js helpers), matter-facts strip, roster, progress line, empty state, footer. Mirrors the factory shape of `review-fill.js`. Classic (non-module) browser script in the global `<script>` list, like its peers. | **T2** (skeleton + counterparty/facts/entity-fill), **T3** (roster), **T4** (footer/gate) — see ownership note below |
| `static/js/modules/review-overview-model.mjs` | Pure functions, unit-testable without a browser: `sortRosterClauses(clauses, statusFn)`, `reviewProgress(clauses, ackFn)` → `{ reviewed, total }`, `approveGateReasons({ clauses, statusFn, ackFn, stale })`. No DOM. CommonJS-export-guarded like the other `.mjs` modules so node tests can `require`/`import` it. | **T2** creates; **T3/T4** add their pure helpers |
| `static/css/overview.css` | All `.studio-overview-*` styles: roster rows, verdict pills, reviewed check, progress bar, facts strip, footer, empty state. New file → zero `styles.css` contention. | **T6** |
| `tests/frontend/review-overview.cjs` | Playwright end-to-end: tab order/rename, roster sort + jump + selected highlight, progress count, empty-state refresh, approve-gate enable/disable + reason text. Mirrors `tests/frontend/review-workstation.cjs` harness. | **T7** |
| `tests/frontend/review-overview-model.mjs` | Pure-module unit tests for `review-overview-model.mjs` (sort, progress, gate). Mirrors `tests/frontend/utility-modules.mjs`. | **T7** |

> **One-file, multiple-owners caution:** `review-overview.js` is logically one
> controller but is split across T2/T3/T4. To keep them from colliding, T2 lands
> the **skeleton + section seams first** (a `render()` that calls
> `renderCounterpartyAndFacts()`, `renderRoster()`, `renderProgress()`,
> `renderFooter()`, `renderEmptyState()` — with T3/T4's functions stubbed). T3
> and T4 then fill *their own* stub function only. This makes the file's internal
> boundaries explicit and the merges trivial. If you prefer hard isolation,
> promote each section to its own `static/js/review-overview-roster.js` /
> `-footer.js` file — acceptable, but 3 files vs 1 is a judgment call; the
> stub-seam approach is recommended.

### 2.3 Files edited in place (small, owned, non-overlapping)

| File | Edit | Owner |
|---|---|---|
| `static/app.js` | Rename inspector view `fill`→`overview` in `REVIEW_INSPECTOR_VIEWS` + `REVIEW_INSPECTOR_TITLES` (title `"Overview"`); default selected view → `overview`; instantiate `reviewOverviewController` (mirror the `corpusController` / controller-wiring block ≈ lines 99–134); expose it where `renderStudioDetail` can call it. | **T1** |
| `static/js/review-workstation-rendering.js` | In `renderStudioDetail()` (≈1420) and `renderStudioEmpty()` (≈112): replace the `=== "fill"` dispatch with `=== "overview"` → `reviewOverviewController.render()`. | **T1** owns the dispatch swap; **T3** adds roster-refresh via the controller's `refresh()` (no edits here) |
| `static/js/review-fill.js` | Update its own `state.reviewInspectorView === "fill"` self-guards to `"overview"` (≈ line 65 and any others). The entity-fill widget itself is unchanged; Overview embeds it. | **T2** (keeps the entity-fill code with its new host) |
| `static/js/review-workstation-viewer.js` | Default new matters to the Overview view: in `loadMatterIntoReview` / `prepareMatterReviewLoad` (≈884/913), ensure `state.reviewInspectorView` defaults to `"overview"` on load (or rely on `normalizeReviewInspectorView` default). | **T1** |

### 2.4 Files read-only (depended on, not edited)

- `static/js/modules/clause-status.mjs` — `clauseStatus`, `clauseDisplayName`
  (consumed by roster; **do not** re-derive verdicts).
- `static/js/review-workstation-actions.js` — `markMatterReviewed`,
  `updateApproveReviewControl`, `approveBlockReasons`, `approveSelectedReview`.
  T4 *extends* `approveBlockReasons` to add the human-review gate reason; that is
  the one edit here (flagged as a shared touch-point — T4 owns it).
- `static/js/review-workstation-source.js` — `renderCounterpartyConfirmation`,
  `submitCounterpartyOverride`, the counterparty handlers (reused by T2).
- `static/js/repository-actions.js` — `loadMatterIntoReview` plumbing (named in
  the brief; the Overview tab does not edit it, it consumes the loaded matter).
- `static/js/modules/review-workstation-model.mjs` — `nextClauseSelectionState`
  (already returns `reviewInspectorView: "clause"`, so click-to-jump is correct).

---

## 3. Task breakdown (one agent per task)

Each task = an independently-buildable piece with explicit file ownership and
dependencies. Shared-file touch-points are flagged with ⚠️ and a recommended owner.

### T1 — Tab registration, rename + reorder, header cleanup *(integration spine — assign first)*

- **Owns:**
  - ⚠️ `static/index.html`: reorder/rename the `.studio-inspector-tabs` buttons to
    `Overview / Clause / Structure` (`data-review-inspector="overview"` first);
    move `#studioCounterpartyField`, `.studio-matter-meta`, and the
    `#studioApproveReviewButton` + `#studioSendForSignatureButton` nodes out of the
    header (keep ids stable) into Overview-host containers; collapse the header to
    `#studioDocTitle` + `.back-dot`; add the new `<script src=".../review-overview.js?v=…">`
    and `<link rel="stylesheet" href="/static/css/overview.css?v=…">`; bump the
    `?v=` cache-busts on every JS/CSS file this feature changes
    (`app.js`, `review-workstation-rendering.js`, `review-fill.js`,
    `review-workstation-viewer.js`, plus the new `review-overview.js` + `overview.css`).
  - `static/app.js`: rename inspector view `fill`→`overview`
    (`REVIEW_INSPECTOR_VIEWS`, `REVIEW_INSPECTOR_TITLES` — title `"Overview"`),
    default selected view `overview`, instantiate + wire `reviewOverviewController`.
  - `static/js/review-workstation-rendering.js`: swap the `=== "fill"` dispatch →
    `=== "overview"` → `reviewOverviewController.render()` in `renderStudioDetail`
    and `renderStudioEmpty`.
  - `static/js/review-workstation-viewer.js`: default `reviewInspectorView` to
    `"overview"` on matter load.
- **Depends on:** the `reviewOverviewController` *interface* existing — so T1 and
  T2 agree the controller exposes `render()` and is constructed with `{ state }`.
  T1 can land against a thin T2 stub. **T1 is the integration spine**: it owns the
  two shared files (`index.html` + the app.js wiring) so no other task edits them.
- **Shared-file flags:** ⚠️ `index.html` (sole owner T1). ⚠️ `styles.css` minimal
  header tweak after nodes leave the header — keep it inside T1's `index.html`
  work or hand to T6; do not let T2/T3/T4 touch `styles.css`.

### T2 — Counterparty + matter-facts + entity-fill (Overview top block)

- **Owns:** `static/js/review-overview.js` **skeleton + `render()` seam**
  (the function stubs T3/T4 fill) + the counterparty block + matter-facts strip +
  the absorbed entity-fill widget; creates `static/js/modules/review-overview-model.mjs`.
- **Reuses (read-only):** `renderCounterpartyConfirmation` /
  `submitCounterpartyOverride` (`review-workstation-source.js`) by keeping the
  `#studioCounterpartyField` node (moved by T1) and re-invoking the existing
  render on Overview render; the entity picker/insert/replace from
  `review-fill.js`. Matter-facts data per §4.
- **Edits:** `static/js/review-fill.js` self-guard rename `"fill"`→`"overview"`
  (owns the fill widget's relocation).
- **Depends on:** T1 having moved `#studioCounterpartyField` into the Overview
  host container (or T2 renders it itself if T1 leaves it). Coordinate the host
  container id in kickoff (e.g. `#studioOverviewBody`).

### T3 — Clause roster component

- **Owns:** the `renderRoster()` stub inside `static/js/review-overview.js`; the
  `sortRosterClauses()` pure helper in `review-overview-model.mjs`.
- **Builds:** problems-first roster rows = clause name + AI verdict pill
  (`clauseStatus`) + human "reviewed" check (`clauseReviewAcknowledged` /
  `markMatterReviewed({ clauseId })`) + click-to-jump
  (`selectReviewClause(id, { jump: true })`) + selected-row highlight. Sort
  Fail → Needs Review → Pass → Pending. `stopPropagation()` on the pill + check so
  they don't trigger the row jump.
- **Reuses (read-only):** `clause-status.mjs`, the existing
  `selectReviewClause` / `markMatterReviewed`.
- **Depends on:** T2's skeleton seam (`renderRoster` stub) + T6's roster CSS class
  names (agree class names in kickoff: `.studio-overview-row`,
  `.studio-overview-verdict`, `.studio-overview-reviewed`, `.is-selected`).

### T4 — Progress line + footer (Approve gate + Send for signature)

- **Owns:** the `renderProgress()` + `renderFooter()` + `renderEmptyState()` stubs
  inside `review-overview.js`; the `reviewProgress()` + `approveGateReasons()` pure
  helpers in `review-overview-model.mjs`.
- ⚠️ **Edits `static/js/review-workstation-actions.js`**: extend
  `approveBlockReasons()` to push a `human_review_incomplete` (or agreed code)
  reason when `reviewProgress()` is not complete, and add its label to
  `approveBlockReasonLabel()`. **Flag:** this is the one shared-file edit outside
  T1. Owner = T4; no other task touches that file — low collision risk,
  single-function edit.
- **Builds:** progress line "N of M clauses reviewed" (counts human sign-offs,
  §1.2(4)) + optional fill bar; footer Approve button (greyed + gate-reason text)
  reusing `#studioApproveReviewButton` / `updateApproveReviewControl` /
  `approveSelectedReview`; Send-for-signature reusing
  `#studioSendForSignatureButton` / `syncDocuSignTriggerButton`; empty state
  ("No review yet" + "Refresh with AI" → `refreshSelectedReview()` reusing the
  `/review-refresh` handler).
- **Depends on:** T2 skeleton; the §1.2(4) progress-count decision (A vs B);
  backend gate-code confirmation (§4).

### T5 — Click-to-jump wiring + selected-clause sync

- **Scope:** mostly *verification + glue*, not new code — `selectReviewClause`
  already jumps + switches to Clause. T5 ensures: (a) the roster re-renders the
  selected highlight when selection changes elsewhere (e.g. via the existing
  clause lane), (b) the Overview roster re-paints when `markMatterReviewed`
  flips an ack, (c) returning to the Overview tab reflects the current selection.
- **Owns:** the re-render hooks. Prefer exposing a `reviewOverviewController.refresh()`
  the controller calls itself, so the selection / ack change paths trigger an
  Overview re-render *when the active inspector view is overview* without editing
  shared rendering/actions files. If a one-line call is unavoidable in
  `renderStudioResult` / `markMatterReviewed`, ⚠️ coordinate that single line with
  T1 (rendering) / T4 (actions).
- **Depends on:** T3 (roster), T2 (skeleton). Can merge late.

> T5 is small enough to fold into T3 if you want fewer agents. Kept separate so
> the "selected highlight stays in sync across both the lane and the roster" edge
> case has a clear owner.

### T6 — Overview CSS

- **Owns:** `static/css/overview.css` (new file). All `.studio-overview-*` styles:
  facts strip, roster rows, verdict pills (green/amber/red — reuse existing tone
  tokens: `.match`/`pass`, `.review`, `.verify`/`check` per `clause-status.mjs`
  `dotTone`/`tone`), reviewed check, progress bar, footer, empty state. Match the
  existing studio visual language (border-radius, spacing, the
  `.studio-inspector-tabs` / `.studio-clause-item` look).
- **Edits:** ⚠️ at most a minimal header-layout adjustment in `styles.css` after
  T1 empties the header — coordinate with T1 (recommend T1 makes that tweak inside
  its header pass so `styles.css` has a single owner here).
- **Depends on:** agreed class names with T2/T3/T4 (define them in the kickoff
  contract so CSS and markup land independently).

### T7 — Frontend tests

- **Owns:** `tests/frontend/review-overview.cjs` (Playwright e2e, mirror
  `tests/frontend/review-workstation.cjs` harness — random port, key-free AI stub,
  loads a fixture matter) + `tests/frontend/review-overview-model.mjs` (pure unit
  tests for sort / progress / gate). Add a `test:frontend:overview` script to
  `package.json` (⚠️ `package.json` shared — single owner T7, trivial append).
- **Covers:** tab order + rename; roster sort (fail/review first); click-to-jump
  selects clause + switches to Clause tab; selected-row highlight; progress count
  reflects human sign-offs not AI verdict; empty-state "Refresh with AI" calls the
  refresh path; Approve disabled + gate-reason text until complete, enabled after.
- **Depends on:** T2–T4 merged (writes assertions against final markup/ids).
  The `.mjs` model tests can start as soon as `review-overview-model.mjs` exists
  (after T2/T3/T4 land their pure helpers).

### Dependency / sequencing summary

```
T1 (spine: index.html + app.js wiring + rename)  ── lands first (with T2 stub)
  └─ T2 (skeleton + counterparty/facts/entity-fill + model.mjs)  ← unblocks T3/T4
       ├─ T3 (roster)            ┐
       ├─ T4 (footer/gate)       ├─ parallel once T2 skeleton exists
       └─ T6 (overview.css)      ┘  (CSS parallel from the class-name contract)
            └─ T5 (jump/selection sync)  ← after T3
                 └─ T7 (tests)           ← after T2–T4 merged
```

### Shared-file ownership map (the only contended files)

| Shared file | Sole owner | Why it's contended | Mitigation |
|---|---|---|---|
| `static/index.html` | **T1** | tablist + header + script/CSS includes + `?v=` | T1 is the only task that edits it |
| `static/app.js` | **T1** | inspector view rename + controller wiring | one task |
| `static/css/overview.css` | **T6** | all Overview CSS | **new file** → no `styles.css` contention |
| `static/styles.css` | **T1** (or T6) | only a small header tweak after nodes leave | single owner; keep it in the header pass |
| `static/js/review-overview.js` | **T2 / T3 / T4** | one controller, three sections | T2 lands skeleton + section stubs first; T3/T4 fill only their stub fn |
| `static/js/review-fill.js` | **T2** | `=== "fill"`→`"overview"` self-guard rename | single owner (the fill widget's new host) |
| `static/js/review-workstation-actions.js` | **T4** | extend `approveBlockReasons` for the human-review gate | single-function edit, no other task touches it |
| `static/js/review-workstation-rendering.js` | **T1** | `=== "fill"`→`"overview"` dispatch | T1 owns the swap; T3 adds roster-refresh via controller `refresh()` not by editing this file |
| `static/js/review-workstation-viewer.js` | **T1** | default view `"overview"` on load | one task |
| `package.json` | **T7** | add test script | trivial append |

---

## 4. Backend data each piece needs

The backend-data teammate is exposing the contract in parallel. The Overview tab
consumes the **already-loaded matter object** (`state.selectedMatter`) +
`state.reviewClauses` + `state.latestReviewResult`. Required fields per piece:

**Counterparty block (T2)** — already present on the matter:
- `matter.counterparty` (string), `matter.counterparty_needs_confirmation` (bool),
  `matter.counterparty_confidence` (0–1), `matter.counterparty_source`
  (`"human"` etc.). Write endpoint: `POST /api/matters/{id}/counterparty`
  (`{ name }`) — already exists.
- Entity-fill: `GET /api/signing-entities` (already used by `review-fill.js`).

**Matter-facts strip (T2/T3)** — `governing law · term · received date`:
- **Received date**: `matter.received_at || imported_at || created_at || updated_at`,
  formatted with `RepositoryModel.formatMatterDate`. *(present today)*
- **Governing law**: for generated matters, `matter.manifest.governing_law_value`
  (+ `governing_law_overridden` / `entity_default_governing_law_value` for the
  "(overridden from …)" provenance). For reviewed inbound NDAs, derive from the
  `governing_law` clause result in `state.reviewClauses`. **Ask the backend
  teammate to expose a single normalized `matter.governing_law` (label string) on
  the review payload** so the strip doesn't branch on matter type.
- **Term**: `matter.manifest.term_years` for generated; for inbound, the term
  clause result. **Ask for a normalized `matter.term_label`** (e.g. `"3 years"`)
  on the review payload likewise.

**Clause roster (T3)** — per clause in `state.reviewClauses`:
- AI verdict: `clause.review_state.state` (`pass`/`review`/`check`/`pending`)
  and/or `clause.decision` (`pass`/`review`/`fail`) — consumed via `clauseStatus`.
  *(already on the review result; do not change)*
- `clause.id`, `clause.name` (display name).
- Human reviewed: client-side ack map + `matter.human_reviewed` (today's model).
  **Decision needed (§1.2(4) A vs B)**: if per-clause human review should persist
  across reloads/sessions, ask the backend teammate whether a per-clause reviewed
  set should be stored on the matter (today only the matter-level `human_reviewed`
  bool + per-session `state.reviewedClauseIds` map exist). Persisting per-clause
  acks is the cleanest backing for "2 of 4 reviewed" surviving a reload.

**Progress line + Approve gate (T4)**:
- Progress numerator/denominator are computed client-side from the roster +
  ack map (no new backend field strictly required for display).
- Approve gate: `POST /api/matters/{id}/approve` returns `409` with
  `blocks_approval: [codes]` when blocked. **Confirm with the backend teammate the
  exact code(s)** for "not all clauses reviewed" so the client predictor
  (`approveBlockReasons`) matches the server and never understates the block. Today
  the only code is `stale_playbook`.

**Empty state (T4)**:
- `hasReviewResults()` (client-derived from `state.reviewClauses.length`).
- "Refresh with AI": `POST /api/matters/{id}/review-refresh` (already built).

**Send for signature (T4)**: existing DocuSign trigger contract
(`docusign-send.js` + `syncDocuSignTriggerButton`) — no new fields.

---

## 5. Build kickoff contract (decide these before fan-out)

1. **§1.2(4) progress semantics**: option **A** (count all clauses) vs **B**
   (needs-attention only). Recommended **A**.
2. **Internal view key**: rename `"fill"`→`"overview"` (recommended) — affects T1's
   rename sweep list.
3. **Host container id** for the Overview body (e.g. `#studioOverviewBody`) — T1
   and T2 must agree so moved nodes (`#studioCounterpartyField` etc.) land cleanly.
4. **CSS class-name contract** (`.studio-overview-row` / `-verdict` / `-reviewed` /
   `.is-selected` / `-facts` / `-progress` / `-footer` / `-empty`) — T6 ↔ T2/T3/T4.
5. **Approve gate code** for incomplete human review — T4 ↔ backend teammate.
6. **Per-clause reviewed persistence** — backend teammate (needed only if "2 of 4"
   must survive reload).
