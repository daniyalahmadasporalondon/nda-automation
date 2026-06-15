# Skill description audit — WHAT-style → WHEN-style proposals

**Rule audited:** a skill's front-matter `description` is the router the model reads when deciding which skill to load. It must describe **WHEN** to trigger (situations, signals, keywords), not **WHAT** the skill does (its steps/workflow). A WHAT-style description tempts the model to follow the summary and skip the SKILL.md body, and gives the router weaker disambiguation signal.

**Scope:** our editable/user-owned skills only — the 30 in `~/.agents/skills/` (symlinked into `~/.claude/skills/`) plus the two real dirs `~/.claude/skills/autoreview` and `~/.claude/skills/impeccable`. Plugin skills (`legal:*`, `anthropic-skills:*`, `cowork-plugin-management:*`) were NOT audited — they are read-only.

**STATUS: PROPOSE ONLY — DO NOT APPLY.** Editing any of these descriptions changes auto-triggering for a global skill that this very session relies on. These are recommendations for the user to apply deliberately, one at a time, watching for trigger regressions. The only description set directly is the NEW `skill-hardening` skill.

---

## Verdict summary

Audited: 32 skills.
- **Clean (WHEN-style, leave as-is): 24** — caveman, design-an-interface, design-taste-frontend, git-guardrails-claude-code, grill-me, grill-with-docs, impeccable (×1, the real dir = the symlinked one), improve-codebase-architecture, migrate-to-shoehorn, obsidian-vault, prototype, qa, request-refactor-plan, review, scaffold-exercises, setup-matt-pocock-skills, setup-pre-commit, to-issues, to-prd, triage, ubiquitous-language, writing-beats, writing-fragments, writing-shape.
- **Offenders (WHAT-leaning, proposed rewrite below): 8** — diagnose, edit-article, tdd, write-a-skill, handoff, teach, zoom-out, autoreview.

The offenders fall in two grades:
- **Step-enumerating (worst — body-skip risk):** diagnose, tdd, edit-article, autoreview.
- **WHAT-summary with no/weak trigger clause:** write-a-skill, handoff, teach, zoom-out.

---

## Proposed rewrites (before → after)

### 1. diagnose  — STEP-ENUMERATING
**Before:**
> Disciplined diagnosis loop for hard bugs and performance regressions. Reproduce → minimise → hypothesise → instrument → fix → regression-test. Use when user says "diagnose this" / "debug this", reports a bug, says something is broken/throwing/failing, or describes a performance regression.

**After:**
> Use when a bug is hard — it's intermittent, the obvious fix didn't hold, or something is broken/throwing/failing and you don't yet know why — or when the user says "diagnose this" / "debug this" or reports a performance regression.

**Why:** "Reproduce → minimise → hypothesise → instrument → fix → regression-test" is the entire method in the router. A hurried agent reads the arrow-chain and improvises the loop instead of opening SKILL.md. The trigger clause was already good; drop the procedure.

---

### 2. tdd  — STEP-ENUMERATING
**Before:**
> Test-driven development with red-green-refactor loop. Use when user wants to build features or fix bugs using TDD, mentions "red-green-refactor", wants integration tests, or asks for test-first development.

**After:**
> Use when building a feature or fixing a bug test-first, or when the user asks for TDD, test-first development, integration tests, or mentions "red-green-refactor".

**Why:** "with red-green-refactor loop" names the discipline in the router; the body's whole value is the vertical-slice loop the agent must NOT shortcut. Keep the triggers, move the loop name out of the lead.

---

### 3. edit-article  — STEP-ENUMERATING
**Before:**
> Edit and improve articles by restructuring sections, improving clarity, and tightening prose. Use when user wants to edit, revise, or improve an article draft.

**After:**
> Use when the user has an article draft they want made stronger — clearer, tighter, better-structured — e.g. "edit my article", "revise this draft", "tighten this section", or hands over a piece for a polish pass.

**Why:** "by restructuring sections, improving clarity, and tightening prose" enumerates the moves the body should own. Recast as the situation that triggers it.

---

### 4. autoreview  — STEP-ENUMERATING
**Before:**
> Run a structured code review (Codex default, Claude optional) as a closeout check on a local or PR branch before commit or ship.

**After:**
> Use as a closeout check before committing or shipping a local or PR branch, or when the user asks for a structured code review of pending changes. (Defaults to Codex; Claude optional.)

**Why:** No "Use when" clause at all — it's pure WHAT. It also overlaps `review` and `code-review`, so a sharp trigger ("before commit or ship", "closeout check") is what lets the router pick it correctly.

---

### 5. write-a-skill  — WHAT-SUMMARY
**Before:**
> Create new agent skills with proper structure, progressive disclosure, and bundled resources. Use when user wants to create, write, or build a new skill.

**After:**
> Use when the user wants to create, write, or build a new agent skill, or scaffold a skill's structure, front-matter, and bundled resources.

**Why:** "with proper structure, progressive disclosure, and bundled resources" is a WHAT recap. The trigger clause is fine; fold the capability into the situation.

---

### 6. handoff  — WHAT-SUMMARY, NO TRIGGER
**Before:**
> Compact the current conversation into a handoff document for another agent to pick up.

**After:**
> Use when wrapping up a session so another agent (or a fresh context) can continue — e.g. "write a handoff", "hand this off", running low on context, or about to switch agents.

**Why:** Zero "Use when" clause; the router has nothing to match on. Pure WHAT.

---

### 7. teach  — WHAT-SUMMARY, NO TRIGGER
**Before:**
> Teach the user a new skill or concept, within this workspace.

**After:**
> Use when the user wants to learn or be taught a concept, technique, or tool in the context of this workspace — e.g. "teach me X", "help me understand Y", "walk me through how Z works".

**Why:** No trigger; vague WHAT. Easily confused with generic explanation — needs explicit signals.

---

### 8. zoom-out  — WHAT-SUMMARY, IMPERATIVE
**Before:**
> Tell the agent to zoom out and give broader context or a higher-level perspective. Use when you're unfamiliar with a section of code or need to understand how it fits into the bigger picture.

**After:**
> Use when you need higher-level context on unfamiliar code — how a section fits the bigger picture, the architecture around a file, or "zoom out / give me the big picture / how does this fit together".

**Why:** Leads with an imperative WHAT ("Tell the agent to zoom out…"). The second sentence is already WHEN-style and good; promote it and drop the imperative lead.

---

## Notes / caveats for the user

- **Apply one at a time and watch triggering.** Several of these (tdd, diagnose, review/autoreview) sit in crowded neighborhoods; a description change can pull triggers toward or away from a sibling skill. Change, then exercise a few real prompts before the next.
- **impeccable** appears twice in the inventory (a real dir at `~/.claude/skills/impeccable` and in the listing) but is the same skill; its description is long but WHEN-style (it enumerates *situations/targets*, not steps) — left clean.
- **caveman** names triggers verbatim ("caveman mode", "be brief", "/caveman") — textbook WHEN; left clean.
- The `setup-matt-pocock-skills` description uses "Run before first use of…" which is a WHEN-condition, not a step recap — left clean.
