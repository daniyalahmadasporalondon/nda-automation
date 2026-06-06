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


class TestProhibitedPatternCoverage:
    """D1: the guard's prohibited-language screen must cover ALL the families
    gen-verify's gate flags, so the in-process guard and the external gate agree
    on what is off-position (the earlier pattern leaked 5 families to the doc)."""

    @pytest.mark.parametrize(
        "snippet",
        [
            "the receiving party shall not compete in our market",
            "the parties will not solicit one another's employees",
            "you must not circumvent or bypass the disclosing party",
            "the parties shall deal exclusively with one another",
            "all right, title and interest in the IP is hereby assigned",
            "the obligations shall continue in perpetuity",
            "any breach carries liquidated damages of USD 100,000",
            "this agreement will automatically renew each year",
        ],
    )
    def test_widened_guard_catches_every_prohibited_family(self, snippet):
        assert gen_ai._PROHIBITED_PATTERN.search(snippet) is not None

    @pytest.mark.parametrize(
        "on_position",
        [
            "Each party acts as both a Disclosing Party and a Receiving Party and is bound reciprocally.",
            "is independently developed by the receiving Party without use of the Confidential Information.",
            "trade secrets and personal data protected by data-protection law remain confidential for as long as the law requires.",
            "exploring opportunities in a competitive payments market",
        ],
    )
    def test_widened_guard_does_not_false_positive_on_legitimate_text(self, on_position):
        assert gen_ai._PROHIBITED_PATTERN.search(on_position) is None

    def test_guard_rejects_smuggled_non_compete_and_keeps_playbook(self):
        playbook_text = "each party acts as both a Disclosing Party and a Receiving Party."

        def provider(request):
            return request["playbook_text"] + " The receiving party shall not compete with us."

        adapter = gen_ai.build_clause_adapter(provider=provider)
        # The non_compete now trips the guard -> Playbook wording kept.
        assert adapter.adapt("mutuality", playbook_text, {}) == playbook_text


class TestFrozenClauseAdapter:
    """The frozen adapter replays recorded on-position clause text so gen-verify
    can gate the AI-shaped output deterministically (no network, no drift)."""

    def test_golden_fixture_clauses_pass_the_guardrail(self):
        # Every recorded clause must keep its load-bearing terms (so the guard
        # accepts it, not the Playbook fallback) and carry no prohibited position.
        recordings = gen_ai._load_frozen_recordings()
        adapter = gen_ai.build_frozen_clause_adapter()
        assert recordings, "fixture should record at least one clause"
        for clause_id, recorded in recordings.items():
            out = adapter.adapt(clause_id, "PLAYBOOK_BASE", {})
            assert out == recorded, f"{clause_id} recording was rejected by the guard"
            for term in gen_ai.CLAUSE_REQUIRED_TERMS.get(clause_id, ()):
                assert term.lower() in out.lower()
            assert not gen_ai._PROHIBITED_PATTERN.search(out)

    def test_unknown_clause_replays_empty_so_guard_keeps_playbook(self):
        adapter = gen_ai.build_frozen_clause_adapter()
        playbook_text = "each party acts as both a Disclosing Party and a Receiving Party."
        # No recording for this id -> inner returns "" -> guard keeps the Playbook.
        assert adapter.adapt("not_a_recorded_clause", playbook_text, {}) == playbook_text

    def test_injected_recordings_override_the_fixture(self):
        adapter = gen_ai.build_frozen_clause_adapter(
            recordings={"mutuality": "each party (Disclosing Party / Receiving Party) is bound reciprocally."}
        )
        out = adapter.adapt("mutuality", "PLAYBOOK_BASE", {})
        assert out.startswith("each party")

    def test_drifted_injected_recording_falls_back_to_playbook(self):
        # A fixture that drifts off position cannot smuggle a bad clause past the
        # guard — it falls back to the authoritative Playbook wording.
        adapter = gen_ai.build_frozen_clause_adapter(
            recordings={"term_and_survival": "obligations end after a while."}
        )
        playbook_text = "trade secrets and data-protection obligations survive as required."
        assert adapter.adapt("term_and_survival", playbook_text, {}) == playbook_text

    def test_frozen_generation_passes_self_check(self, playbook):
        result = _generate(playbook, gen_ai.build_frozen_clause_adapter())
        check = gen.self_check_generated_nda(result.docx_bytes, playbook=playbook)
        assert check.passed, (check.native_failures, check.dynamic_failures)

    def test_frozen_generation_is_repeatable(self, playbook):
        from nda_automation.docx_text import extract_docx_text

        a = extract_docx_text(_generate(playbook, gen_ai.build_frozen_clause_adapter()).docx_bytes)
        b = extract_docx_text(_generate(playbook, gen_ai.build_frozen_clause_adapter()).docx_bytes)
        assert a == b

    def test_frozen_output_differs_from_deterministic(self, playbook):
        # Proves the frozen path actually exercises the AI-shaped wording rather
        # than collapsing to the deterministic path.
        from nda_automation.docx_text import extract_docx_text

        frozen = extract_docx_text(_generate(playbook, gen_ai.build_frozen_clause_adapter()).docx_bytes)
        deterministic = extract_docx_text(_generate(playbook, None).docx_bytes)
        assert frozen != deterministic

    def test_generate_for_entity_use_frozen_is_repeatable_and_passes(self, playbook):
        from nda_automation.docx_text import extract_docx_text

        a = gen.generate_nda_for_entity("aspora_technology", _intake(), playbook=playbook, use_frozen=True)
        b = gen.generate_nda_for_entity("aspora_technology", _intake(), playbook=playbook, use_frozen=True)
        assert extract_docx_text(a.docx_bytes) == extract_docx_text(b.docx_bytes)
        check = gen.self_check_generated_nda(a.docx_bytes, playbook=playbook)
        assert check.passed, (check.native_failures, check.dynamic_failures)
