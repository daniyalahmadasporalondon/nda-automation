import json
import unittest

from nda_automation.ai_assessment_contract import AI_CLAUSE_ASSESSMENT_SCHEMA
from nda_automation.ai_assessment_prompt import (
    AI_ASSESSMENT_PROMPT_VERSION,
    AI_ASSESSMENT_RESPONSE_SCHEMA,
    AI_ASSESSMENT_TASK,
    build_ai_assessment_packet,
    build_ai_assessment_prompt,
)
from nda_automation.checker import load_playbook


SOURCE_TEXT = "\n\n".join([
    "Each party may disclose Confidential Information to the other party under this Agreement.",
    "This Agreement shall be governed by the laws of California.",
    "The confidentiality obligations survive for five years.",
])


class AIAssessmentPromptTests(unittest.TestCase):
    def test_packet_contains_playbook_rules_paragraphs_and_output_contract(self):
        packet = build_ai_assessment_packet(
            SOURCE_TEXT,
            playbook=load_playbook(),
            provider="test-provider",
            model="test-model",
        )

        self.assertEqual(packet["version"], AI_ASSESSMENT_PROMPT_VERSION)
        self.assertEqual(packet["task"], AI_ASSESSMENT_TASK)
        self.assertEqual(packet["provider"], "test-provider")
        self.assertEqual(packet["model"], "test-model")
        self.assertEqual(packet["document"]["paragraph_count"], 3)
        self.assertEqual(packet["document"]["included_paragraph_count"], 3)
        self.assertEqual([paragraph["id"] for paragraph in packet["paragraphs"]], ["p1", "p2", "p3"])
        self.assertEqual(packet["output_contract"]["response_schema"], AI_ASSESSMENT_RESPONSE_SCHEMA)
        self.assertEqual(
            packet["output_contract"]["response_schema"]["properties"]["assessments"]["items"],
            AI_CLAUSE_ASSESSMENT_SCHEMA,
        )
        self.assertEqual(packet["output_contract"]["required_assessment_count"], len(load_playbook()["clauses"]))
        self.assertEqual(
            [clause["clause_id"] for clause in packet["playbook"]["clauses"]],
            [clause["id"] for clause in load_playbook()["clauses"]],
        )

    def test_packet_includes_governing_law_approved_options(self):
        packet = build_ai_assessment_packet(SOURCE_TEXT, playbook=load_playbook())
        governing_law = next(clause for clause in packet["playbook"]["clauses"] if clause["clause_id"] == "governing_law")

        self.assertEqual(
            [option["value"] for option in governing_law["rules"]["approved_options"]],
            ["India", "Delaware", "England and Wales", "DIFC"],
        )
        self.assertEqual(
            [option["value"] for option in governing_law["rules"]["approved_options"] if option.get("default")],
            ["England and Wales"],
        )

    def test_packet_instructions_cover_missing_absent_and_verdict_choices(self):
        packet = build_ai_assessment_packet(SOURCE_TEXT, playbook=load_playbook())
        instructions = " ".join(packet["instructions"])

        self.assertIn("exactly one assessment for every playbook clause", instructions)
        self.assertIn("missing required clauses", instructions)
        self.assertIn("absent prohibited clauses", instructions)
        self.assertIn("reviewer-facing assessment commentary", instructions)
        self.assertIn("Ground every present-clause verdict in a quote", instructions)
        self.assertIn("ungrounded pass will be downgraded to review", instructions)
        self.assertIn("2 to 4 concise sentences", instructions)
        self.assertIn("specific to the cited document text", instructions)
        self.assertIn("pass", packet["decision_policy"])
        self.assertIn("fail", packet["decision_policy"])
        self.assertIn("review", packet["decision_policy"])

    def test_packet_carries_ordered_reasoning_steps(self):
        packet = build_ai_assessment_packet(SOURCE_TEXT, playbook=load_playbook())
        steps = packet["reasoning_steps"]

        self.assertIsInstance(steps, list)
        # The five-step method must be present and in order: locate -> read -> apply -> cite -> decide.
        leading_verbs = [step.split(":", 1)[0].strip().lower() for step in steps]
        self.assertEqual(leading_verbs, ["locate", "read carefully", "apply", "cite", "decide"])

    def test_hardened_instructions_cover_negation_and_escalation(self):
        packet = build_ai_assessment_packet(SOURCE_TEXT, playbook=load_playbook())
        instructions = " ".join(packet["instructions"])

        # Polarity lesson: an inverted phrase is not a prohibition.
        self.assertIn("shall not be restricted from", instructions)
        self.assertIn("freedom-preserving", instructions)
        # Carve-outs/exceptions/conditions are honoured.
        self.assertIn("carve-outs, exceptions, and conditions", instructions)
        # Hard escalate-on-ambiguity rule, not a guess.
        self.assertIn("escalation is the correct", instructions)
        self.assertIn("Never guess a pass or fail", instructions)
        # Output-consistency tightening.
        self.assertIn("identical clause language must yield the same decision", instructions)
        # The reasoning method is referenced from the instruction list too.
        self.assertIn("locate, read carefully, apply, cite, decide", instructions)

    def test_system_prompt_teaches_polarity_and_escalation(self):
        packet = build_ai_assessment_packet(SOURCE_TEXT, playbook=load_playbook())
        prompt = build_ai_assessment_prompt(packet)
        system = prompt["system"]

        self.assertIn("shall not be restricted from dealing", system)
        self.assertIn("escalate to", system)
        self.assertIn("read it carefully including every negation", system)

    def test_packet_budget_records_omitted_paragraphs(self):
        packet = build_ai_assessment_packet(
            SOURCE_TEXT,
            playbook=load_playbook(),
            max_paragraphs=2,
            max_chars=10000,
        )

        self.assertEqual(packet["document"]["paragraph_count"], 3)
        self.assertEqual(packet["document"]["included_paragraph_count"], 2)
        self.assertEqual(packet["document"]["omitted_paragraph_count"], 1)
        self.assertEqual([paragraph["id"] for paragraph in packet["paragraphs"]], ["p1", "p2"])

    def test_prompt_contract_wraps_packet_with_system_and_response_schema(self):
        packet = build_ai_assessment_packet(SOURCE_TEXT, playbook=load_playbook())
        prompt = build_ai_assessment_prompt(packet)

        self.assertEqual(prompt["version"], AI_ASSESSMENT_PROMPT_VERSION)
        self.assertIn("Use only the supplied playbook rules and document paragraphs", prompt["system"])
        self.assertEqual(prompt["response_schema"], AI_ASSESSMENT_RESPONSE_SCHEMA)
        self.assertIn("Return only JSON matching the response schema", prompt["user"])
        self.assertIn(AI_ASSESSMENT_TASK, prompt["user"])
        parsed_packet = json.loads(prompt["user"].split("\n\n", 1)[1])
        self.assertEqual(parsed_packet["task"], AI_ASSESSMENT_TASK)


if __name__ == "__main__":
    unittest.main()
