# Phase 6 Backend Design: Playbook Draft, Publish, and Runtime Safety

Status: proposed

Owner: backend

Last updated: 2026-06-04

## Purpose

Phase 6 turns Playbook editing from "save changes to the live rules" into a controlled backend workflow:

1. edit a draft Playbook,
2. validate the draft,
3. preview operational impact,
4. publish as the active Playbook,
5. record exactly which active Playbook version each matter review used.

The goal is to prevent silent rule changes from affecting review creation, export, or Gmail send without an audit trail.

## Current State

The current backend already has:

- `playbook.json` as the live Playbook used by `checker.load_playbook()`.
- `GET /api/playbook` returning the current Playbook and public history.
- `POST /api/playbook` validating and writing `playbook.json`.
- `POST /api/playbook/restore` restoring a historical snapshot directly into `playbook.json`.
- `playbook.history.json` storing restorable snapshots.
- `REVIEW_ENGINE_VERSION` based staleness for stored matter reviews.
- stale review refresh that re-runs review and clears `human_reviewed` and `redline_draft`.

The gap is that a Playbook save currently changes active review behavior immediately.

## Goals

- Separate draft Playbook changes from the active runtime Playbook.
- Publish active Playbook changes explicitly.
- Record active Playbook metadata on every new review result.
- Mark matter reviews stale when the active Playbook changes.
- Prevent export/send from using stale review results.
- Keep the current JSON-file architecture and lock discipline.
- Keep the current checker contract mostly intact.
- Preserve backward compatibility for the existing frontend during rollout.

## Non-Goals

- No Playbook visual redesign.
- No per-user Playbook drafts in this phase.
- No database migration.
- No counsel evaluation model.
- No AI prompt rewrite beyond supplying active Playbook metadata.
- No automatic bulk re-analysis of all existing matters after publish.

## Core Decisions

### 1. `playbook.json` remains the active runtime Playbook

Do not rename or move the active Playbook file in Phase 6.

Rationale:

- `checker.load_playbook()` and the AI prompt pipeline already read `playbook.json`.
- Keeping `playbook.json` active avoids a broad review-engine refactor while the other UX pass is active.
- The sidecar files can add draft/publish safety without changing every checker entry point.

### 2. Draft state lives in sidecar files

Add sidecars next to `playbook.json`:

- `playbook.draft.json`
- `playbook.runtime.json`
- existing `playbook.history.json`

`playbook.runtime.json` is the small metadata index. It should be safe to rebuild from `playbook.json`, `playbook.draft.json`, and history if necessary.

### 3. Active staleness is hash-based

Each active Playbook snapshot has:

- `active_version_id`: human/audit identifier.
- `active_hash`: canonical SHA-256 hash of the full Playbook JSON.
- `published_at`.
- `published_by`.
- `playbook_name`.
- `playbook_version`.

Matter staleness should compare `active_hash`, not only `active_version_id`.

Rationale:

- Restoring and publishing an identical Playbook should not make every matter stale.
- The hash is the behavioral identity. The version id is the audit identity.

### 4. Restore creates a draft, not a live change

Restoring a historical Playbook version should create or replace the draft. It should not publish automatically.

Rationale:

- Restore is still a policy change.
- The reviewer/admin should be able to inspect, validate, and publish restored policy deliberately.

### 5. Publishing invalidates old reviews but does not mutate them

Publishing a new active Playbook should not immediately rewrite stored matter reviews.

Instead:

- existing reviews become stale when their recorded Playbook hash differs;
- opening the review can show stale metadata;
- explicit refresh re-runs the review under the new active Playbook;
- refresh clears `human_reviewed` and `redline_draft`.

This preserves auditability and controls AI/API cost.

## Storage Model

### `playbook.json`

Canonical active Playbook.

Existing file. Still validated before every write.

### `playbook.draft.json`

Optional draft payload:

```json
{
  "version": 1,
  "draft_id": "pbd_20260604T210000Z_ab12cd34ef56",
  "base_active_version_id": "pbv_20260604T200000Z_112233445566",
  "base_active_hash": "sha256:...",
  "updated_at": "2026-06-04T21:00:00+00:00",
  "updated_by": "admin",
  "summary": "Draft changes to Governing Law.",
  "changed_clause_ids": ["governing_law"],
  "snapshot_hash": "sha256:...",
  "snapshot": {}
}
```

Notes:

- `snapshot` is a full Playbook object.
- `base_active_hash` is used for conflict detection.
- Draft saves must validate the full Playbook before writing.

### `playbook.runtime.json`

Runtime metadata:

```json
{
  "version": 1,
  "active_version_id": "pbv_20260604T200000Z_112233445566",
  "active_hash": "sha256:...",
  "published_at": "2026-06-04T20:00:00+00:00",
  "published_by": "system",
  "playbook_name": "Aspora NDA Playbook",
  "playbook_version": "1",
  "draft_id": "pbd_20260604T210000Z_ab12cd34ef56",
  "draft_hash": "sha256:...",
  "draft_updated_at": "2026-06-04T21:00:00+00:00",
  "draft_base_active_version_id": "pbv_20260604T200000Z_112233445566",
  "draft_base_active_hash": "sha256:..."
}
```

If this file is absent, the backend should compute active metadata from `playbook.json` and write the sidecar lazily under the Playbook lock.

### `playbook.history.json`

Continue using the existing history file. Extend entries with:

```json
{
  "id": "pbv_20260604T213000Z_aabbccddeeff",
  "recorded_at": "2026-06-04T21:30:00+00:00",
  "actor": "admin",
  "action": "publish",
  "summary": "Published changes to Governing Law.",
  "playbook_name": "Aspora NDA Playbook",
  "playbook_version": "1",
  "changed_clause_ids": ["governing_law"],
  "snapshot_hash": "sha256:...",
  "base_active_version_id": "pbv_20260604T200000Z_112233445566",
  "base_active_hash": "sha256:...",
  "snapshot": {}
}
```

Supported `action` values after Phase 6:

- `baseline`
- `draft_save`
- `draft_discard`
- `publish`
- `restore_to_draft`
- `restore_publish` only if a future endpoint deliberately publishes during restore

Public history should continue omitting `snapshot`.

## Review Result Metadata

Every new review result should include:

```json
{
  "playbook_runtime": {
    "active_version_id": "pbv_20260604T213000Z_aabbccddeeff",
    "active_hash": "sha256:...",
    "playbook_name": "Aspora NDA Playbook",
    "playbook_version": "1",
    "published_at": "2026-06-04T21:30:00+00:00",
    "published_by": "admin",
    "source": "active"
  }
}
```

This should be added by the review engine after `review_nda` or `assess_nda_with_ai` produces the result, or by a shared helper inside the active review engine wrapper.

Required invariant:

- The Playbook used to create the AI prompt and deterministic checks must be the same Playbook represented by `playbook_runtime.active_hash`.

Implementation note:

- A helper such as `playbook_routes.active_playbook_metadata()` can compute metadata under the Playbook lock.
- A later refactor can pass a Playbook object directly into the checker and AI assessor. Phase 6 does not require that wider refactor if `playbook.json` remains active.

## Staleness Rules

Current `review_result_is_stale(review_result)` should gain Playbook checks:

1. stale if `review_engine_version` differs from `REVIEW_ENGINE_VERSION`;
2. stale if required review structure fields are missing;
3. stale if `playbook_runtime.active_hash` is missing;
4. stale if `playbook_runtime.active_hash` differs from current active Playbook hash.

Recommended response metadata:

```json
{
  "review_refresh": {
    "stale": true,
    "stale_reasons": ["playbook_changed"],
    "current_playbook": {
      "active_version_id": "pbv_...",
      "active_hash": "sha256:..."
    },
    "review_playbook": {
      "active_version_id": "pbv_...",
      "active_hash": "sha256:..."
    },
    "refresh_method": "POST",
    "refresh_url": "/api/matters/<id>/review-refresh"
  }
}
```

Legacy matter reviews without `playbook_runtime` should be stale after Phase 6. That is acceptable because the first refresh records the active Playbook version and clears stale redline decisions.

## Export and Send Guards

Export and Gmail send should reject stale review results.

Recommended behavior:

- If the review is stale, return HTTP `409`.
- Error copy: `Review is stale because the active Playbook changed. Refresh the review before exporting or sending a redline.`
- Do not silently refresh during export/send.
- Do not apply saved redline decisions against a stale review.

Rationale:

- Export/send is where stale redline decisions become user-facing contract output.
- Refresh can clear redline drafts, so it should remain an explicit action.

## API Design

### `GET /api/playbook`

Backward compatible response:

```json
{
  "playbook": {},
  "active": {
    "playbook": {},
    "metadata": {}
  },
  "draft": null,
  "history": []
}
```

Compatibility:

- Existing frontend can keep reading top-level `playbook`.
- Phase 6 frontend can read `active`, `draft`, and `history`.

### `POST /api/playbook/draft`

Save a validated draft only.

Request:

```json
{
  "playbook": {},
  "actor": "admin",
  "expected_base_active_version_id": "pbv_...",
  "expected_base_active_hash": "sha256:...",
  "summary": "Draft changes to term cap."
}
```

Responses:

- `200` with `{ "draft": {}, "active": {}, "history": [] }`
- `400` for invalid schema/template
- `409` when expected base does not match current active metadata

### `POST /api/playbook/publish`

Publish the current draft or supplied Playbook as active.

Preferred request:

```json
{
  "draft_id": "pbd_...",
  "actor": "admin",
  "expected_active_version_id": "pbv_...",
  "expected_active_hash": "sha256:..."
}
```

Allowed fallback request for API clients:

```json
{
  "playbook": {},
  "actor": "admin",
  "expected_active_hash": "sha256:..."
}
```

Behavior:

- validate full Playbook;
- conflict if current active hash differs from expected hash;
- write `playbook.json` atomically;
- write runtime metadata atomically;
- append `publish` history entry;
- clear matching draft;
- return new active metadata.

### `POST /api/playbook/discard-draft`

Discard the current draft.

Request:

```json
{
  "draft_id": "pbd_...",
  "actor": "admin"
}
```

Behavior:

- conflict if supplied `draft_id` does not match current draft;
- remove `playbook.draft.json`;
- clear draft metadata in runtime sidecar;
- append `draft_discard` history entry without full active snapshot duplication unless useful.

### `POST /api/playbook/restore`

Change semantics from "restore directly to active" to "restore to draft".

Request:

```json
{
  "history_id": "pbv_...",
  "actor": "admin"
}
```

Response:

```json
{
  "draft": {},
  "active": {},
  "history": [],
  "restored_to_draft_at": "2026-06-04T21:40:00+00:00"
}
```

Compatibility option:

- Keep the existing route path.
- Add response field `restore_mode: "draft"`.
- The frontend can update copy from "restored" to "restored to draft".

### `GET /api/playbook/impact`

Optional but recommended for Phase 6.

Returns matter impact for publishing a draft:

```json
{
  "draft_id": "pbd_...",
  "changed_clause_ids": ["governing_law"],
  "active_hash": "sha256:...",
  "draft_hash": "sha256:...",
  "matters": {
    "total": 24,
    "would_be_stale": 18,
    "already_stale": 3,
    "closed": 1,
    "with_redline_draft": 4,
    "human_reviewed": 7
  }
}
```

This endpoint should be owner-aware under per-user matter storage. For admin/global deployment impact later, add a separate admin-only mode.

## Concurrency and Locking

All draft/publish/restore/discard operations should use the existing Playbook lock.

Required checks:

- Validate the candidate Playbook under the lock.
- Re-read current runtime metadata under the lock.
- Check expected active hash/version before write.
- Write JSON files atomically.
- Prefer a single critical section for active write plus runtime/history write.

Expected conflict response:

```json
{
  "error": "The active Playbook changed while this draft was open.",
  "code": "playbook_conflict",
  "active": {
    "active_version_id": "pbv_...",
    "active_hash": "sha256:..."
  }
}
```

## Migration

On first Phase 6 startup:

1. Validate existing `playbook.json`.
2. Compute canonical hash.
3. Create `playbook.runtime.json` if absent.
4. Create baseline history if history is absent.
5. Leave `playbook.draft.json` absent.
6. Treat existing matter reviews without `playbook_runtime` as stale.

No file rename is required.

## Implementation Slices

### 6A: Playbook runtime metadata helpers

Files likely touched:

- `nda_automation/routes/playbook.py`
- `tests/test_server.py`
- `tests/test_playbook_rules.py` or new `tests/test_playbook_runtime.py`

Deliver:

- canonical hash helper;
- active metadata helper;
- runtime sidecar read/write;
- migration from current `playbook.json`;
- tests.

### 6B: Draft, publish, discard, restore-to-draft endpoints

Files likely touched:

- `nda_automation/routes/playbook.py`
- `nda_automation/server.py`
- server route tests

Deliver:

- `POST /api/playbook/draft`;
- `POST /api/playbook/publish`;
- `POST /api/playbook/discard-draft`;
- updated `POST /api/playbook/restore`;
- `GET /api/playbook` response extension;
- optimistic concurrency tests.

### 6C: Review result Playbook metadata

Files likely touched:

- `nda_automation/review_engine.py`
- `nda_automation/checker.py` or a narrow metadata helper
- `nda_automation/ai_first_review.py` if AI-first bypasses wrapper metadata
- review tests

Deliver:

- `playbook_runtime` on new review results;
- test that AI-first and deterministic active engine paths both record metadata.

### 6D: Matter staleness and export/send guards

Files likely touched:

- `nda_automation/routes/matters.py`
- `nda_automation/redline_export_service.py`
- `nda_automation/routes/gmail.py`
- `tests/test_server.py`

Deliver:

- stale reason includes Playbook hash mismatch;
- refresh records new Playbook metadata;
- refresh clears `redline_draft`;
- stale export/send returns `409`;
- tests for stale redline draft safety.

### 6E: Frontend wiring after UX pass lands

Files likely touched later:

- `static/js/playbook-view.js`
- `static/app.js`
- `static/index.html`
- `static/styles.css`
- `tests/frontend/review-workstation.cjs`

Deliver:

- save draft;
- publish;
- discard draft;
- restore to draft;
- impact preview;
- stale review copy.

This slice should wait until the current auth/Gmail/deployment UX pass has landed.

## Test Plan

Backend:

- active metadata is created from existing `playbook.json`;
- canonical hash is stable for key order changes;
- draft save validates templates and does not alter `playbook.json`;
- publish writes `playbook.json`, updates runtime metadata, appends history, clears draft;
- publish conflicts when active hash changed;
- restore creates draft and does not alter active Playbook;
- review result records `playbook_runtime`;
- matter is stale when active hash changes;
- stale refresh clears `human_reviewed` and `redline_draft`;
- export/send rejects stale review.

Frontend later:

- Playbook tab shows draft vs active state;
- save draft does not mark current matters stale;
- publish shows impact and then marks old matter reviews stale;
- restore creates editable draft;
- stale review blocks export/send until refresh.

## Risks

- Global draft means two admins can contend. Mitigation: optimistic concurrency and clear conflict response.
- JSON sidecars are fine for private 2-3 user hosting, but not multi-instance writes. Mitigation: keep file lock and document single-instance assumption.
- Existing matters become stale after metadata rollout. Mitigation: explicit refresh and clear stale copy.
- Export/send guard may interrupt existing workflows. Mitigation: clear `409` message and a refresh CTA in frontend slice.
- AI-first fail-closed can make stale refresh fail. Mitigation: stale review remains visible but export/send stays blocked until successful refresh or runtime mode changes.

## Open Questions

1. Should publishing require a required `summary` field?
   - Recommendation: optional in Phase 6, required later if audit quality is poor.

2. Should restore have an emergency "publish immediately" mode?
   - Recommendation: no for Phase 6. Restore to draft only.

3. Should draft be per-user?
   - Recommendation: no for Phase 6. Add per-user drafts only if admin contention becomes real.

4. Should closed matters be marked stale?
   - Recommendation: yes in metadata, but do not prompt refresh in closed workflows unless someone reopens or exports again.

5. Should a Playbook publish automatically increment the human-readable `playbook.version` field?
   - Recommendation: no. Generate runtime version ids automatically; leave human-readable version as an editable policy label.

## Acceptance Criteria

Phase 6 backend is complete when:

- a Playbook draft can be saved without changing active review behavior;
- a Playbook draft can be published with conflict protection;
- restored history snapshots become drafts, not live policy;
- every new review result records active Playbook metadata;
- old reviews become stale when the active Playbook hash changes;
- stale refresh records new Playbook metadata and clears redline drafts;
- export and Gmail send reject stale review results;
- all behavior is covered by backend tests.
