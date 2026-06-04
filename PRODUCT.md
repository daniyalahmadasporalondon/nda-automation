# Product

## Register

product

## Users

In-house legal counsel and contract reviewers at Aspora. They work inbound NDAs one matter at a time, under time pressure, deciding per clause whether the language is acceptable, needs review, or must be redlined before signing. Their trust in the verdict shown on screen is the whole job.

## Product Purpose

Triage and redline NDAs faster without lowering the legal bar. A deterministic engine classifies each clause (pass / review / fail); a blind AI second opinion is reconciled against it; the reviewer confirms, comments, and exports a redlined Word document to send to the counterparty. Success = a reviewer trusts the screen enough to send the redline without re-reading the whole contract.

## Brand Personality

Calm, exact, trustworthy. The interface of a senior colleague who has already done the first pass: confident, quiet, never flashy. Three words: precise, restrained, dependable.

## Anti-references

- Demoware: fake gauges, "match %", corpus/coverage stats, anything implying precision the engine doesn't have.
- Consumer-AI exuberance: gradients, glassmorphism, celebratory motion, emoji verdicts.
- Generic SaaS dashboard (hero-metric cards, rainbow status chips).
- Anything that visually softens or hides a FAIL/REVIEW so a clause reads safer than it is.

## Design Principles

1. **The verdict is sacred.** A clause's pass/review/fail must be unmistakable at a glance, never conveyed by color alone, never visually softened. Output integrity outranks aesthetics.
2. **Earned familiarity.** Behave like the tools counsel already trust (Linear / Stripe grade): standard affordances, consistent vocabulary, the tool disappears into the task.
3. **One restrained accent.** Aspora purple marks action and selection only; semantic green/amber/red carry verdict meaning; neutrals carry everything else.
4. **Show the reasoning, honestly.** When the AI and the deterministic engine disagree, or a citation can't be validated, say so plainly instead of papering over it.
5. **Density with calm.** Dense legal information, organised so it reads quiet.

## Accessibility & Inclusion

Target WCAG 2.1 AA. Verdict never by color alone (label + color). Full keyboard operability for tabs, clause navigation, and the document viewer. Respect `prefers-reduced-motion`. Known gaps at audit (2026-06-04): muted text below 4.5:1, color-only inline diffs, broken tab focus management, modal without focus trap.
