"""Tests for the AI-first clause adapter (Playbook-bounded, guardrailed).

The adapter lets an AI rephrase the Playbook's authoritative clause wording to
fit a deal. The guardrail is the safety net: if the AI drifts off-position
(drops a load-bearing term) or fails, generation falls back to the deterministic
Playbook wording — so the generated NDA always still passes its own Playbook.
"""

from __future__ import annotations

import datetime

import pytest

from nda_automation import nda_generation as gen
from nda_automation import nda_generation_ai as gen_ai
from nda_automation.checker import load_playbook


@pytest.fixture
def playbook():
    return load_playbook()


def _bundle():
    return {
        "id": "real_transfer",
        "legal_name": "Real Transfer Limited",
        "addresses": [
            {"id": "london", "label": "London", "lines": ["10 Finsbury Square"], "country": "UK", "default": True}
        ],
        "governing_law": {"playbook_option_id": "england_and_wales", "label": "England and Wales"},
        "jurisdiction": "Courts of England and Wales",
        "signatory": {"name": "Jane Doe", "title": "Director"},
    }


def _intake():
    return gen.CounterpartyIntake(
        company_name="Acme Innovations Pvt Ltd",
        registered_office="42 MG Road, Bengaluru",
        jurisdiction_of_incorporation="India",
        business_description="digital payments",
        purpose="a cross-border payments partnership",
        term_years=3,
        agreement_date=datetime.date(2026, 6, 6),
    )


def _generate(playbook, adapter):
    entity = gen.entity_party_from_bundle(_bundle(), playbook)
    return gen.generate_nda(entity, _intake(), playbook=playbook, clause_adapter=adapter)


class TestBuildClauseAdapter:
    def test_returns_none_without_api_key(self, monkeypatch):
        monkeypatch.delenv(gen_ai.OPENROUTER_API_KEY_ENV, raising=False)
        assert gen_ai.build_clause_adapter() is None

    def test_returns_adapter_with_injected_provider(self):
        adapter = gen_ai.build_clause_adapter(provider=lambda request: "")
        assert adapter is not None
        assert hasattr(adapter, "adapt")


class TestGuardrail:
    def test_on_position_adaptation_is_used(self, playbook):
        # A provider that rephrases but keeps the load-bearing terms is accepted.
        def provider(request):
            base = request["playbook_text"]
            return base + " (adapted for the deal)"

        adapter = gen_ai.build_clause_adapter(provider=provider)
        out = adapter.adapt(
            "term_and_survival",
            "trade secrets and data-protection obligations survive as required.",
            {"counterparty": "Acme"},
        )
        assert "(adapted for the deal)" in out

    def test_drifted_adaptation_falls_back_to_playbook(self, playbook):
        # A provider that drops a required term must NOT be used.
        playbook_text = "trade secrets and data-protection obligations survive as required."

        def provider(request):
            return "the obligations end after a while."  # drops trade secret / data-protection

        adapter = gen_ai.build_clause_adapter(provider=provider)
        out = adapter.adapt("term_and_survival", playbook_text, {})
        assert out == playbook_text

    def test_provider_failure_falls_back_to_playbook(self, playbook):
        def provider(request):
            raise RuntimeError("model exploded")

        adapter = gen_ai.build_clause_adapter(provider=provider)
        out = adapter.adapt("mutuality", "each party acts as both a Disclosing Party and a Receiving Party.", {})
        assert "each party acts as both" in out

    def test_runaway_padding_is_rejected(self):
        playbook_text = "each party acts as both a Disclosing Party and a Receiving Party."

        def provider(request):
            return playbook_text + " " + ("padding " * 500)

        adapter = gen_ai.build_clause_adapter(provider=provider)
        assert adapter.adapt("mutuality", playbook_text, {}) == playbook_text


class TestAiFirstStillPassesSelfCheck:
    def test_on_position_ai_adapter_passes_self_check(self, playbook):
        # AI rephrases every clause but keeps positions -> self-check still green.
        def provider(request):
            return request["playbook_text"] + " The parties acknowledge the foregoing."

        adapter = gen_ai.build_clause_adapter(provider=provider)
        result = _generate(playbook, adapter)
        check = gen.self_check_generated_nda(result.docx_bytes, playbook=playbook)
        assert check.passed, (check.native_failures, check.dynamic_failures)

    def test_adversarial_ai_adapter_still_passes_via_fallback(self, playbook):
        # A hostile adapter that tries to gut every clause must be neutralised by
        # the guardrail; the deterministic fallback keeps the doc on-position.
        def provider(request):
            return "This clause is hereby deleted."

        adapter = gen_ai.build_clause_adapter(provider=provider)
        result = _generate(playbook, adapter)
        check = gen.self_check_generated_nda(result.docx_bytes, playbook=playbook)
        assert check.passed, (check.native_failures, check.dynamic_failures)

    def test_ai_adapter_cannot_introduce_non_circumvention(self, playbook):
        # If the AI tries to smuggle a prohibited restriction into an adapted
        # clause, the guardrail's prohibited-language screen rejects that output and
        # keeps the deterministic Playbook wording — so the prohibited text never
        # reaches the document and the dynamic self-check stays clean. Defence in
        # depth: even if it leaked, the dynamic gate would catch it.
        def provider(request):
            return request["playbook_text"] + " The parties shall not circumvent one another."

        adapter = gen_ai.build_clause_adapter(provider=provider)
        result = _generate(playbook, adapter)
        from nda_automation.docx_text import extract_docx_text

        assert "circumvent" not in extract_docx_text(result.docx_bytes).lower()
        check = gen.self_check_generated_nda(result.docx_bytes, playbook=playbook)
        assert check.dynamic_failures == [], check.dynamic_failures
