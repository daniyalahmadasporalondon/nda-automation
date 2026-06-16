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


class TestGenerationModel:
    """The generate path must use the FAST generation model, not the Opus reviewer."""

    def test_default_generation_model_is_fast_not_review_model(self, monkeypatch):
        monkeypatch.delenv(gen_ai.GENERATION_MODEL_ENV, raising=False)
        resolved = gen_ai.configured_generation_model()
        assert resolved == gen_ai.DEFAULT_GENERATION_MODEL
        # Must NOT silently fall back to the heavyweight review model (the bug).
        assert resolved != gen_ai.DEFAULT_OPENROUTER_MODEL
        assert "opus" not in resolved.lower()

    def test_env_override_is_honoured(self, monkeypatch):
        # Use a value distinct from the default so this proves the env knob, not the
        # default. (The default is now deepseek-v4-flash; deepseek-v4-pro is a real,
        # distinct DeepSeek model we also run, so it's a faithful override value.)
        monkeypatch.setenv(gen_ai.GENERATION_MODEL_ENV, "deepseek/deepseek-v4-pro")
        assert gen_ai.configured_generation_model() == "deepseek/deepseek-v4-pro"

    def test_blank_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv(gen_ai.GENERATION_MODEL_ENV, "   ")
        assert gen_ai.configured_generation_model() == gen_ai.DEFAULT_GENERATION_MODEL

    def test_build_clause_adapter_uses_generation_model_by_default(self, monkeypatch):
        # With a key but no explicit model, the live OpenRouter adapter must carry the
        # fast generation model — this is the exact call the generate path makes.
        monkeypatch.delenv(gen_ai.GENERATION_MODEL_ENV, raising=False)
        monkeypatch.setenv(gen_ai.OPENROUTER_API_KEY_ENV, "sk-or-test-key")
        adapter = gen_ai.build_clause_adapter()
        assert adapter is not None
        inner = adapter._inner  # GuardedClauseAdapter wraps the OpenRouter adapter
        assert isinstance(inner, gen_ai.OpenRouterClauseAdapter)
        assert inner.model == gen_ai._sanitize_model_name(gen_ai.DEFAULT_GENERATION_MODEL)
        assert "opus" not in inner.model.lower()

    def test_explicit_model_overrides_generation_default(self, monkeypatch):
        monkeypatch.setenv(gen_ai.OPENROUTER_API_KEY_ENV, "sk-or-test-key")
        adapter = gen_ai.build_clause_adapter(model="anthropic/claude-opus-4.8")
        assert adapter._inner.model == "anthropic/claude-opus-4.8"


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


class TestRequiredTermsReconciliation:
    """The CLAUSE_REQUIRED_TERMS table duplicates substance the generator now
    reads live from the Playbook clause templates. ``reconcile_required_terms``
    asserts the two copies cannot silently diverge."""

    def test_canonical_playbook_reconciles(self, playbook):
        # The shipped table must already agree with the shipped Playbook.
        gen_ai.reconcile_required_terms(playbook)

    def test_every_required_term_is_in_its_source_template(self, playbook):
        clauses = {c["id"]: c for c in playbook["clauses"]}
        for clause_id, terms in gen_ai.CLAUSE_REQUIRED_TERMS.items():
            field = gen_ai._REQUIRED_TERM_SOURCE_FIELD[clause_id]
            template = str(clauses[clause_id].get(field) or "").lower()
            for term in terms:
                assert term.lower() in template, (clause_id, term)

    def test_divergence_raises(self, playbook):
        from copy import deepcopy

        edited = deepcopy(playbook)
        ci = next(c for c in edited["clauses"] if c["id"] == "confidential_information")
        # Reword the template away from the required "independently developed" term.
        ci["standard_exclusions_template"] = "Confidential Information does not include public information."
        with pytest.raises(AssertionError):
            gen_ai.reconcile_required_terms(edited)


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


class _SlowAdapter:
    """Adapter that blocks for ``hang_seconds`` on the named clause (simulates a hung
    or pathologically slow AI call); all other clauses adapt instantly."""

    def __init__(self, *, slow_clause: str, hang_seconds: float, fast_text: str = "FAST"):
        self.slow_clause = slow_clause
        self.hang_seconds = hang_seconds
        self.fast_text = fast_text

    def adapt(self, clause_id, base_text, context):
        if clause_id == self.slow_clause:
            import time

            time.sleep(self.hang_seconds)
            return "ADAPTED-SHOULD-NEVER-BE-USED"
        return self.fast_text


class TestAdaptWallClockDeadline:
    """The whole AI clause-adaptation step is bounded by a HARD wall-clock deadline:
    a hung/slow call is abandoned and the deterministic base text is used, so a stuck
    AI call can never hold the synchronous generate request past the budget."""

    _JOBS = [
        ("mutuality", "BASE-mutuality", {}),
        ("confidential_information", "BASE-ci", {}),
        ("term_and_survival", "BASE-term", {}),
    ]

    def test_hung_call_is_abandoned_at_the_deadline(self):
        import time

        adapter = _SlowAdapter(slow_clause="mutuality", hang_seconds=30.0)
        start = time.monotonic()
        result = gen_ai.adapt_clauses_in_parallel(adapter, self._JOBS, total_budget_seconds=0.5)
        elapsed = time.monotonic() - start

        # Returned at ~the budget, NOT the 30s hang.
        assert elapsed < 5.0, f"hung call held the request {elapsed:.1f}s"
        # The hung clause fell back to its deterministic base text...
        assert result["mutuality"] == "BASE-mutuality"
        # ...while the instant clauses still got their adapted text.
        assert result["confidential_information"] == "FAST"
        assert result["term_and_survival"] == "FAST"

    def test_single_job_hung_call_also_respects_the_deadline(self):
        import time

        adapter = _SlowAdapter(slow_clause="mutuality", hang_seconds=30.0)
        start = time.monotonic()
        result = gen_ai.adapt_clauses_in_parallel(
            adapter, [("mutuality", "BASE-mutuality", {})], total_budget_seconds=0.5
        )
        elapsed = time.monotonic() - start
        assert elapsed < 5.0, f"single hung call held the request {elapsed:.1f}s"
        assert result["mutuality"] == "BASE-mutuality"

    def test_fast_calls_complete_within_budget_and_keep_ai_text(self):
        adapter = _SlowAdapter(slow_clause="<none>", hang_seconds=0.0)
        result = gen_ai.adapt_clauses_in_parallel(adapter, self._JOBS, total_budget_seconds=5.0)
        assert result == {
            "mutuality": "FAST",
            "confidential_information": "FAST",
            "term_and_survival": "FAST",
        }

    def test_budget_is_env_tunable(self, monkeypatch):
        monkeypatch.setenv(gen_ai.ADAPT_TOTAL_BUDGET_ENV, "3.5")
        assert gen_ai.configured_adapt_total_budget_seconds() == 3.5
        # Non-positive / unparseable never disables the deadline.
        monkeypatch.setenv(gen_ai.ADAPT_TOTAL_BUDGET_ENV, "0")
        assert gen_ai.configured_adapt_total_budget_seconds() == gen_ai.DEFAULT_ADAPT_TOTAL_BUDGET_SECONDS
        monkeypatch.setenv(gen_ai.ADAPT_TOTAL_BUDGET_ENV, "not-a-number")
        assert gen_ai.configured_adapt_total_budget_seconds() == gen_ai.DEFAULT_ADAPT_TOTAL_BUDGET_SECONDS


class TestOutputBoundedRequestBody:
    """The clause-adapter request bounds the model's output so it can't hold the
    synchronous generate by over-generating: reasoning is disabled (the real speed
    lever for the DeepSeek-Flash reasoning model) and a runaway max_tokens cap is set."""

    def test_request_disables_reasoning_and_caps_max_tokens(self):
        request = gen_ai.build_adaptation_request("mutuality", "Each party ...", {})
        body = gen_ai._openrouter_request_body(request, model="deepseek/deepseek-v4-flash")
        assert body["reasoning"] == {"enabled": False}
        assert body["max_tokens"] == gen_ai.DEFAULT_ADAPT_MAX_TOKENS
        # The cap is a runaway guard, not a working bound — comfortably above a real
        # ~25-60 token clause rephrase so it never truncates a legitimate answer.
        assert body["max_tokens"] >= 256
