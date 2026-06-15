---
name: skill-hardening
description: Use when authoring or revising an agent skill that must hold a rule under pressure — a skill that tells an agent to STOP, refuse, wait, escalate, verify, or not take a tempting shortcut (safety gates, "never do X", approval checks, destructive-op guards, refuse-to-guess rules). Also use when a skill exists but agents keep violating it, rationalizing around it, or following its description and skipping its body. Not for purely generative or formatting skills with no rule to defend.
---

# Skill Hardening

A skill that asks an agent to do the easy thing (generate, format, summarize) rarely needs defending — the model wants to comply. A skill that asks an agent to do the *hard* thing — stop, wait, refuse, escalate, redo work, admit it can't — is under constant pressure to be ignored. Time pressure, sunk cost, a confident-sounding user, token exhaustion, and "it's basically fine" all push the agent toward the corner-cut. **A rule you wrote but never pressure-tested is a rule you only hope holds.**

This skill is the red-green-refactor loop, applied to skills instead of code. You do not get to claim a skill works because the prose is clear. You earn it by watching an agent *under pressure* pick the right option and cite the section that made them.

## The core inversion

Normal skill authoring: write instructions, hope they land.
Skill hardening: **find out how the agent rationalizes the wrong thing FIRST, then write the minimal text that defeats exactly those rationalizations.** You cannot pre-imagine the rationalizations as well as a real agent under pressure will produce them. Observe, don't guess.

## When NOT to use this

- The skill has no rule to defend (it only generates / formats / summarizes). Use `write-a-skill` instead.
- You are creating a brand-new skill and don't yet know what rule it defends. Draft it with `write-a-skill` first, then harden the rule here.

## The loop

### RED — watch it fail, unarmed

Before writing or editing a word of the skill, run a fresh subagent through a realistic **pressure scenario** WITHOUT the skill loaded. The point is to capture *how* it talks itself out of the right answer.

A pressure scenario is not a quiz. It must have:

- **Concrete options A / B / C** with real-looking paths, names, and numbers — not abstractions.
- **A "what do you do?"** that forces a single choice, with **no easy out** (no "ask the user", no "do both", no fourth safe option).
- **Real stakes** that make the wrong option genuinely tempting.

Combine **3 or more** pressure types — single-axis scenarios are too easy to resist. See [pressure-taxonomy.md](pressure-taxonomy.md) for the seven axes and how to stack them.

Record the agent's choice **and its rationalizations verbatim.** The exact sentences ("It's a tiny change, the test would just slow us down") are the raw material for GREEN. Do not paraphrase them.

If the unarmed agent picks the *right* option, the scenario is too weak — add pressure (more axes, higher stakes, remove the safe out) and re-run until it fails. A scenario the agent passes unarmed proves nothing about your skill.

### GREEN — write the minimal counter

Write the **smallest** skill text that defeats the *observed* rationalizations — not imagined ones. For each verbatim rationalization, the skill must contain an explicit sentence that names it and negates it. Generic exhortations ("be careful", "always do the right thing") do not count; the agent already ignored those.

Re-run the SAME scenario with the skill loaded. The agent should now pick the right option.

### REFACTOR — close every new crack

The armed agent will usually find a *new* rationalization the first version didn't cover. Each one becomes three things:

1. An **explicit negation** in the body — the rationalization, named and refuted.
2. A **row in the rationalization table** — `| What you'll think | Why it's wrong |`.
3. A **red-flag phrase** in a "stop if you catch yourself saying…" list.

Re-test. Repeat until the agent picks the right option **under maximum pressure AND cites the section** that decided it. Citation is the pass condition: an agent that complies but can't say why will drift the next time pressure changes shape.

**Meta-test on every failure:** ask the violating agent, *"How should this skill have been written so the right option was the obviously correct one?"* The violator knows exactly what text would have stopped it. Mine that answer — it is the highest-signal edit you will get.

See [loop-details.md](loop-details.md) for the subagent prompt scaffolds, what "verbatim" capture looks like, and worked RED/GREEN/REFACTOR examples.

## What a hardened skill contains

Beyond normal `write-a-skill` structure, a hardened rule-defending skill has:

- A blunt statement of the rule and **why the easy path is wrong**, up top.
- Explicit **negations** of each real rationalization (one sentence each).
- A **rationalization table** (`| What you'll think | Why it's wrong |`).
- A **red-flag list**: "Stop if you catch yourself saying…" with the verbatim phrases.
- A **citation hook**: the rule is in a named section the agent can point to.

## Definition of done

- [ ] Ran RED unarmed; the agent cut the corner; rationalizations captured **verbatim**.
- [ ] Scenario stacks **3+ pressure axes** with no easy out.
- [ ] Every observed rationalization has an explicit negation in the body.
- [ ] Rationalization table + red-flag list reflect the *actual* observed phrases.
- [ ] Re-tested armed; agent picks right **and cites the section** under max pressure.
- [ ] Ran the meta-test on the last failing run and folded in the violator's own advice.
- [ ] The skill's OWN `description` is WHEN-style (triggering conditions), never WHAT-style (a workflow summary). See [description-when-not-what.md](description-when-not-what.md).
