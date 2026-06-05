# How the AI Reviews an NDA

Status: active

Owner: evidence

Last updated: 2026-06-05

## Purpose

This is the plain-English explanation of how the AI reviewer assesses an NDA. It
is written for reviewers and stakeholders who want to understand *how a verdict
was reached*, not the code. It mirrors the instructions the model is actually
given in `nda_automation/ai_assessment_prompt.py`; if that prompt changes, this
doc should change with it.

The Playbook is the source of truth. The AI does not bring its own opinion of
what an NDA "should" say — it checks the document against the Playbook's clause
rules and reports what it finds, with the exact supporting text.

## What the AI is asked to do

For every clause in the Playbook, the AI returns one assessment: a decision
(**pass**, **fail**, or **review**), a short plain-English rationale, and — when
the clause is present in the document — the exact quote the decision is based on.

## The five reasoning steps

The AI works one clause at a time and follows the same five steps in order. The
steps are surfaced to the model explicitly (the packet's `reasoning_steps`) so
the method is legible and repeatable.

1. **Locate.** Find the paragraph(s) in the document that address this clause. If
   none do, treat the clause as absent.
2. **Read carefully.** Parse the located text *literally*, accounting for the
   things that quietly change meaning: negations ("not", "no", "nor"), carve-outs
   and exceptions ("except", "other than", "provided that", "save for"),
   conditions ("if", "unless", "to the extent"), and inversions. A genuine
   prohibition can sit right next to freedom-preserving language in the same
   paragraph, so each obligation is judged on its own.
3. **Apply.** Check the read meaning against this clause's Playbook criteria and
   approved options — not against assumptions about what is "normal".
4. **Cite.** Select the exact quote span from the located paragraph that drives
   the decision. The quote is copied, never paraphrased.
5. **Decide.** Pass only if the criteria are satisfied, fail only if they are
   clearly violated, review when the text is ambiguous, conflicting, conditional,
   or incomplete.

## Reading polarity literally

The most common way an automated reviewer goes wrong is reading a keyword without
reading the words around it. "Restricted from dealing" sounds like a restriction
— but "shall **not** be restricted from dealing with introduced contacts"
*preserves* freedom and is **not** a restriction at all. The same goes for
"**Nothing** in this Agreement restricts either party from contacting introduced
parties". The AI is instructed to read these negations and inversions literally
and to honour carve-outs before deciding, so a freedom-preserving carve-out is
not mistaken for a prohibition, and a real prohibition hiding next to freedom
language is still caught.

## When in doubt, escalate

If the language is ambiguous, borderline, internally conflicting, or the AI
cannot tell with confidence whether a clause passes or fails, the answer is
**review** — a human decides. The AI is explicitly told never to guess a pass or
fail just to avoid a review. A review verdict also blocks an automatic send, so
an unclear clause cannot slip through unseen.

## Every verdict is grounded in the document

A finding about clause text must quote the text it relies on. The system enforces
this: a pass or a "present but wrong" finding that cannot cite a supporting quote
is not trusted and is downgraded to **review**. There are only two verdicts that
legitimately have no quote, because there is nothing to quote:

- a **required clause that is missing** (the absence is the finding), and
- a **prohibited clause that is absent** (you cannot quote text that is not there).

Everywhere else, the Review tab and redline can show *based on: "&lt;quoted
text&gt;"*, pointing at the precise span in the document. See
`docs/` and the grounding module (`nda_automation/evidence_grounding.py`) for the
enforcement details.

## Consistency

Identical clause language yields the same decision, issue type, and quote choice
every time. The issue type stays aligned with the decision (pass → none; fail →
missing or present-but-wrong; review → unclear), and the cited quote — not
outside knowledge — drives the verdict.

## A second, adversarial check

Some findings are additionally challenged by an independent verifier pass that
tries to *refute* the engine's conclusion (for example, confirming that a
freedom-to-deal carve-out is not a violation). When the verifier overturns a
finding, the grounding surface is re-derived so the displayed evidence always
matches the final verdict. This is the AI verifier (a separate workstream); the
review reflects its outcome when it runs.
