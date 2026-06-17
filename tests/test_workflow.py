"""Tests for the canonical Matter workflow state machine (nda_automation/workflow.py).

Covers: phase/status derivation across the full lifecycle, the next_action / human_gate
contract, the orthogonal needs_attention failure axis, the board rollup, and the
timeline-event shape. The approval-gate dependency is stubbed where needed so the
Review<->Approval boundary is exercised without a live published playbook.
"""
from __future__ import annotations

import unittest
from unittest import mock

from nda_automation import workflow
from nda_automation.workflow import (
    BOARD_IN_REVIEW,
    BOARD_INBOX,
    BOARD_REVIEWED,
    BOARD_SENT,
    OWNER_HUMAN,
    OWNER_SYSTEM,
    PHASE_APPROVAL,
    PHASE_EXECUTED,
    PHASE_INTAKE,
    PHASE_NEGOTIATION,
    PHASE_REVIEW,
    PHASE_SENT,
    STATUS_APPROVAL_BLOCKED,
    STATUS_APPROVED,
    STATUS_AI_REVIEWING,
    STATUS_AUTO_CLEARED,
    STATUS_AWAITING_APPROVAL,
    STATUS_AWAITING_HUMAN,
    STATUS_COUNTER_RECEIVED,
    STATUS_EXTRACTED,
    STATUS_FULLY_SIGNED,
    STATUS_RECEIVED,
    STATUS_RE_REVIEWING,
    STATUS_REVIEW_FAILED,
    STATUS_SEND_FAILED,
    STATUS_SENDING,
    STATUS_SENT_AWAITING_COUNTERPARTY,
    workflow_state,
)


def _pass_review() -> dict:
    return {"clauses": [{"id": "mutuality", "decision": "pass"}]}


def _flagged_review() -> dict:
    return {"clauses": [{"id": "mutuality", "decision": "review"}]}


def _failed_review() -> dict:
    # A pure-fail review (state 'check', counts.review == 0): the AI rejected a
    # required clause (e.g. an unapproved governing law). This is the blocker
    # fixture -- it must NOT auto-clear.
    return {"clauses": [{"id": "governing_law", "decision": "fail"}]}


def _no_blocks(matter):  # stand-in for a non-stale, fully-resolved approval gate
    return []


class PhaseStatusDerivationTests(unittest.TestCase):
    def test_intake_received_when_no_extracted_text(self):
        state = workflow_state({"board_column": "gmail_demo"})
        self.assertEqual(state["phase"], PHASE_INTAKE)
        self.assertEqual(state["status"], STATUS_RECEIVED)

    def test_intake_extracted_when_text_present_but_no_review(self):
        state = workflow_state({"extracted_text": "NDA body"})
        self.assertEqual(state["phase"], PHASE_INTAKE)
        self.assertEqual(state["status"], STATUS_EXTRACTED)

    def test_review_ai_reviewing_marker(self):
        state = workflow_state({"extracted_text": "x", "ai_reviewing": True})
        self.assertEqual(state["phase"], PHASE_REVIEW)
        self.assertEqual(state["status"], STATUS_AI_REVIEWING)

    def test_review_auto_cleared_when_all_pass(self):
        state = workflow_state({"extracted_text": "x", "review_result": _pass_review()})
        self.assertEqual(state["phase"], PHASE_REVIEW)
        self.assertEqual(state["status"], STATUS_AUTO_CLEARED)

    def test_review_awaiting_human_when_flagged(self):
        state = workflow_state({"extracted_text": "x", "review_result": _flagged_review()})
        self.assertEqual(state["phase"], PHASE_REVIEW)
        self.assertEqual(state["status"], STATUS_AWAITING_HUMAN)

    def test_review_awaiting_human_when_failed(self):
        # The blocker fix at the workflow consumer: a pure-fail (check) review must
        # land in Review/awaiting_human, NOT auto_cleared -- a human has to resolve
        # the flagged clauses before it can move on.
        state = workflow_state({"extracted_text": "x", "review_result": _failed_review()})
        self.assertEqual(state["phase"], PHASE_REVIEW)
        self.assertEqual(state["status"], STATUS_AWAITING_HUMAN)

    def test_failed_review_advances_to_approval_once_human_engages(self):
        # Not permanently wedged: once the human engages (human_reviewed) a
        # fail-state matter advances past Review to the approval gate, exactly like
        # a needs-review matter. The earlier _approval_status branch owns this.
        matter = {"extracted_text": "x", "human_reviewed": True, "review_result": _failed_review()}
        with mock.patch.object(workflow, "_approval_blocks", _no_blocks):
            state = workflow_state(matter)
        self.assertEqual(state["phase"], PHASE_APPROVAL)
        self.assertEqual(state["status"], STATUS_AWAITING_APPROVAL)

    def test_approval_awaiting_when_resolved_and_reviewed(self):
        matter = {"extracted_text": "x", "human_reviewed": True, "review_result": _pass_review()}
        with mock.patch.object(workflow, "_approval_blocks", _no_blocks):
            state = workflow_state(matter)
        self.assertEqual(state["phase"], PHASE_APPROVAL)
        self.assertEqual(state["status"], STATUS_AWAITING_APPROVAL)

    def test_review_when_resolved_but_not_yet_human_reviewed(self):
        # An all-pass review with no human engagement stays in Review/auto_cleared,
        # not Approval -- the human hasn't picked it up yet.
        matter = {"extracted_text": "x", "review_result": _pass_review()}
        with mock.patch.object(workflow, "_approval_blocks", _no_blocks):
            state = workflow_state(matter)
        self.assertEqual(state["phase"], PHASE_REVIEW)
        self.assertEqual(state["status"], STATUS_AUTO_CLEARED)

    def test_approval_blocked_on_document_level_block_when_reviewed(self):
        matter = {"extracted_text": "x", "human_reviewed": True, "review_result": _pass_review()}
        with mock.patch.object(workflow, "_approval_blocks", lambda m: ["stale_playbook"]):
            state = workflow_state(matter)
        self.assertEqual(state["phase"], PHASE_APPROVAL)
        self.assertEqual(state["status"], STATUS_APPROVAL_BLOCKED)

    def test_unresolved_clause_blocks_keep_matter_in_review(self):
        # Per-clause unresolved blocks belong to Review (the reviewer resolves them
        # there); they must NOT surface as an Approval-phase block.
        matter = {"extracted_text": "x", "human_reviewed": True, "review_result": _flagged_review()}
        with mock.patch.object(workflow, "_approval_blocks", lambda m: ["unresolved_clause:mutuality"]):
            state = workflow_state(matter)
        self.assertEqual(state["phase"], PHASE_REVIEW)
        self.assertEqual(state["status"], STATUS_AWAITING_HUMAN)

    def test_approved_status(self):
        state = workflow_state({
            "extracted_text": "x",
            "status": "approved",
            "approved_at": "2026-01-01T00:00:00+00:00",
            "review_result": _pass_review(),
        })
        self.assertEqual(state["phase"], PHASE_APPROVAL)
        self.assertEqual(state["status"], STATUS_APPROVED)

    def test_sent_from_outbound_stamp(self):
        state = workflow_state({
            "extracted_text": "x",
            "board_column": "sent",
            "last_outbound_at": "2026-01-02T00:00:00+00:00",
            "approved_at": "2026-01-01T00:00:00+00:00",
        })
        self.assertEqual(state["phase"], PHASE_SENT)
        self.assertEqual(state["status"], STATUS_SENT_AWAITING_COUNTERPARTY)

    def test_sending_in_flight_marker(self):
        state = workflow_state({"extracted_text": "x", "approved_at": "2026-01-01", "sending": True})
        self.assertEqual(state["phase"], PHASE_SENT)
        self.assertEqual(state["status"], STATUS_SENDING)

    def test_negotiation_counter_received(self):
        state = workflow_state({
            "extracted_text": "x",
            "last_outbound_at": "2026-01-02",
            "counter_received": True,
        })
        self.assertEqual(state["phase"], PHASE_NEGOTIATION)
        self.assertEqual(state["status"], STATUS_COUNTER_RECEIVED)

    def test_negotiation_re_reviewing(self):
        state = workflow_state({
            "extracted_text": "x",
            "last_outbound_at": "2026-01-02",
            "counter_received": True,
            "re_reviewing": True,
        })
        self.assertEqual(state["phase"], PHASE_NEGOTIATION)
        self.assertEqual(state["status"], STATUS_RE_REVIEWING)

    def test_executed_terminal(self):
        state = workflow_state({"extracted_text": "x", "executed_at": "2026-01-03"})
        self.assertEqual(state["phase"], PHASE_EXECUTED)
        self.assertEqual(state["status"], STATUS_FULLY_SIGNED)

    def test_executed_takes_precedence_over_earlier_signals(self):
        # A fully-signed matter that still carries its old outbound/approval stamps
        # reads as Executed, never as an earlier phase.
        state = workflow_state({
            "extracted_text": "x",
            "approved_at": "2026-01-01",
            "last_outbound_at": "2026-01-02",
            "board_column": "sent",
            "executed_at": "2026-01-03",
        })
        self.assertEqual(state["phase"], PHASE_EXECUTED)


class HumanGateTests(unittest.TestCase):
    def test_machine_working_statuses_are_not_human_gates(self):
        for matter in (
            {"extracted_text": "x", "ai_reviewing": True},
            {"extracted_text": "x", "approved_at": "2026-01-01", "sending": True},
        ):
            with self.subTest(matter=matter):
                self.assertFalse(workflow_state(matter)["human_gate"])

    def test_awaiting_human_is_a_gate(self):
        state = workflow_state({"extracted_text": "x", "review_result": _flagged_review()})
        self.assertTrue(state["human_gate"])

    def test_sent_awaiting_counterparty_is_a_gate(self):
        state = workflow_state({"extracted_text": "x", "last_outbound_at": "2026-01-02"})
        self.assertTrue(state["human_gate"])

    def test_failure_is_not_a_human_gate_but_needs_attention(self):
        state = workflow_state({
            "extracted_text": "x",
            "board_column": "in_review",
            "workflow_error": {"phase": "review", "code": "ai_error"},
        })
        self.assertFalse(state["human_gate"])
        self.assertTrue(state["needs_attention"])


class NextActionTests(unittest.TestCase):
    def test_next_action_is_structured_with_owner_and_blocked(self):
        state = workflow_state({"extracted_text": "x", "review_result": _flagged_review()})
        action = state["next_action"]
        self.assertEqual(action["owner"], OWNER_HUMAN)
        self.assertTrue(action["blocked"])
        self.assertIn("Resolve", action["label"])

    def test_system_owned_next_action_for_machine_work(self):
        state = workflow_state({"extracted_text": "x", "ai_reviewing": True})
        action = state["next_action"]
        self.assertEqual(action["owner"], OWNER_SYSTEM)
        self.assertFalse(action["blocked"])

    def test_approved_next_action_is_send(self):
        state = workflow_state({"extracted_text": "x", "status": "approved", "approved_at": "2026-01-01"})
        self.assertEqual(state["next_action"]["owner"], OWNER_HUMAN)
        self.assertIn("Send", state["next_action"]["label"])


class NeedsAttentionTests(unittest.TestCase):
    def test_review_failure_flips_attention_and_keeps_phase(self):
        state = workflow_state({
            "extracted_text": "x",
            "board_column": "in_review",
            "workflow_error": {"phase": "review", "code": "ai_error", "message": "AI timed out"},
        })
        self.assertEqual(state["status"], STATUS_REVIEW_FAILED)
        self.assertTrue(state["needs_attention"])
        self.assertEqual(state["attention_reason"], "AI timed out")

    def test_send_failure(self):
        state = workflow_state({
            "extracted_text": "x",
            "board_column": "sent",
            "workflow_error": {"phase": "sent", "code": "smtp_error"},
        })
        self.assertEqual(state["status"], STATUS_SEND_FAILED)
        self.assertTrue(state["needs_attention"])
        # No message -> fall back to the code.
        self.assertEqual(state["attention_reason"], "smtp_error")

    def test_failure_does_not_move_board_column(self):
        state = workflow_state({
            "extracted_text": "x",
            "board_column": "sent",
            "workflow_error": {"phase": "sent", "code": "smtp_error"},
        })
        self.assertEqual(state["board_column"], BOARD_SENT)

    def test_malformed_workflow_error_without_phase_is_ignored(self):
        state = workflow_state({"extracted_text": "x", "workflow_error": {"code": "x"}})
        self.assertFalse(state["needs_attention"])


class BoardRollupTests(unittest.TestCase):
    def test_unreviewed_gmail_arrival_stays_in_inbox(self):
        state = workflow_state({"board_column": "gmail_demo"})
        self.assertEqual(state["board_column"], BOARD_INBOX)

    def test_review_rolls_up_to_in_review(self):
        state = workflow_state({"extracted_text": "x", "review_result": _flagged_review()})
        self.assertEqual(state["board_column"], BOARD_IN_REVIEW)

    def test_approval_rolls_up_to_reviewed(self):
        state = workflow_state({"extracted_text": "x", "status": "approved", "approved_at": "2026-01-01"})
        self.assertEqual(state["board_column"], BOARD_REVIEWED)

    def test_sent_and_negotiation_roll_up_to_sent(self):
        # A half-signed / outbound matter (and a counter-received negotiation) is
        # still ACTIVE work, so it rolls up to the Sent column.
        for matter in (
            {"extracted_text": "x", "last_outbound_at": "2026-01-02"},
            {"extracted_text": "x", "last_outbound_at": "2026-01-02", "counter_received": True},
        ):
            with self.subTest(matter=matter):
                self.assertEqual(workflow_state(matter)["board_column"], BOARD_SENT)

    def test_executed_rolls_off_the_board(self):
        # An EXECUTED (fully-signed, 2/2) matter is done work and drops OFF the
        # WIP board entirely: it resolves to the terminal off-board sentinel
        # (board_column == ""), not to any active column. This holds even when it
        # still carries its old outbound/sent stamps.
        for matter in (
            {"extracted_text": "x", "executed_at": "2026-01-03"},
            {"extracted_text": "x", "executed": True},
            {
                "extracted_text": "x",
                "last_outbound_at": "2026-01-02",
                "board_column": "sent",
                "executed_at": "2026-01-03",
            },
        ):
            with self.subTest(matter=matter):
                state = workflow_state(matter)
                self.assertEqual(state["phase"], PHASE_EXECUTED)
                self.assertEqual(state["board_column"], workflow.BOARD_NONE)
                self.assertEqual(state["board_column"], "")

    def test_half_signed_matter_stays_in_sent(self):
        # A half-signed (1/2) matter never sets executed, so it stays on the
        # board in Sent as active outbound work.
        matter = {"extracted_text": "x", "last_outbound_at": "2026-01-02"}
        self.assertFalse(workflow.is_matter_executed(matter))
        self.assertEqual(workflow_state(matter)["board_column"], BOARD_SENT)

    def test_is_matter_executed_predicate(self):
        self.assertTrue(workflow.is_matter_executed({"executed": True}))
        self.assertTrue(workflow.is_matter_executed({"executed_at": "2026-01-03"}))
        self.assertFalse(workflow.is_matter_executed({"last_outbound_at": "2026-01-02"}))
        self.assertFalse(workflow.is_matter_executed({}))
        self.assertFalse(workflow.is_matter_executed(None))

    def test_legacy_board_columns_canonicalize(self):
        self.assertEqual(workflow._canonical_board("redline_ready"), BOARD_REVIEWED)
        self.assertEqual(workflow._canonical_board("signed_closed"), BOARD_SENT)


class TimelineEventTests(unittest.TestCase):
    def test_build_event_shape_and_defaults(self):
        event = workflow.build_timeline_event(
            workflow.EVENT_SENT, phase=PHASE_SENT, status=STATUS_SENT_AWAITING_COUNTERPARTY, actor="ops@x", detail="to@y"
        )
        self.assertEqual(event["type"], "sent")
        self.assertEqual(event["phase"], PHASE_SENT)
        self.assertEqual(event["status"], STATUS_SENT_AWAITING_COUNTERPARTY)
        self.assertEqual(event["actor"], "ops@x")
        self.assertEqual(event["detail"], "to@y")
        self.assertTrue(event["at"])  # defaulted to now

    def test_build_event_omits_empty_optionals(self):
        event = workflow.build_timeline_event(workflow.EVENT_CREATED)
        self.assertEqual(set(event.keys()), {"type", "at"})

    def test_timeline_summary_counts_and_last_event(self):
        matter = {
            "matter_timeline": [
                {"type": "created", "at": "2026-01-01"},
                {"type": "sent", "at": "2026-01-02"},
            ]
        }
        summary = workflow.timeline_summary(matter)
        self.assertEqual(summary["event_count"], 2)
        self.assertEqual(summary["last_event"], {"type": "sent", "at": "2026-01-02"})

    def test_timeline_summary_empty(self):
        self.assertEqual(workflow.timeline_summary({})["event_count"], 0)


class RobustnessTests(unittest.TestCase):
    def test_non_dict_matter_is_safe(self):
        state = workflow_state(None)  # type: ignore[arg-type]
        self.assertEqual(state["phase"], PHASE_INTAKE)
        self.assertEqual(state["status"], STATUS_RECEIVED)

    def test_state_does_not_mutate_input(self):
        matter = {"extracted_text": "x", "review_result": _pass_review()}
        before = dict(matter)
        workflow_state(matter)
        self.assertEqual(matter, before)


if __name__ == "__main__":
    unittest.main()
