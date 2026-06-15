# Description = WHEN, never WHAT

## The failure mode

A skill's front-matter `description` is the only text the model sees when deciding whether to load the skill. It is a **router**, not a summary.

When the description *summarizes the workflow* (WHAT the skill does, its steps), two things break:

1. **The model follows the description and skips the body.** If the description already says "Reproduce → minimise → hypothesise → instrument → fix," a hurried model treats that as the instructions and never opens the SKILL.md where the actual discipline lives. The richer the WHAT-summary, the more the body gets skipped.
2. **Triggering gets noisier.** A workflow summary doesn't tell the router *when* this skill beats a neighbor. "Improves articles by restructuring and tightening prose" doesn't say when to pick it over a dozen other writing skills.

## The rule

Describe the **triggering conditions** (WHEN / for-what-situation), never the **procedure** (WHAT-steps / HOW).

- **WHEN-style** answers: *In what situation should I reach for this? What words, file types, or moments signal it?*
- **WHAT-style** answers: *What are the steps?* — which belongs in the body, not the router.

A useful litmus: if you can map clauses of the description onto the headers of the SKILL body, it's leaking WHAT. The description should make you *open* the body to learn the steps, not let you skip it.

## Before → after

**WHAT-style (leaks the workflow):**
> Edit and improve articles by restructuring sections, improving clarity, and tightening prose. Use when user wants to edit, revise, or improve an article draft.

**WHEN-style (pure trigger):**
> Use when the user has an article draft they want made stronger — clearer, tighter, better-structured. Triggers: "edit my article," "revise this draft," "this section drags," handing over a piece for a polish pass.

---

**WHAT-style:**
> Test-driven development with red-green-refactor loop. Use when user wants to build features or fix bugs using TDD…

The "red-green-refactor loop" phrase is the entire discipline compressed into the router — a hurried agent reads it and improvises the loop instead of opening the body. Move the loop into the body; keep only the trigger.

**WHEN-style:**
> Use when building a feature or fixing a bug test-first, or when the user asks for TDD / red-green-refactor / test-first / integration tests.

## Allowed exceptions

A short capability clause ("Anti-slop frontend skill for landing pages…") is fine as long as it orients rather than enumerates steps. The disqualifier is **step enumeration** or a **procedure recap** that an agent could mistake for the instructions.

## How this binds skill-hardening's own description

The `skill-hardening` description must be pure WHEN: it lists the *situations* (authoring a rule-defending skill; a skill agents keep violating) and the *signals* (STOP/refuse/wait/escalate rules, "agents follow the description and skip the body"). It never says "run RED then GREEN then REFACTOR" — that procedure lives in SKILL.md by design, precisely so the model can't shortcut it from the router.
