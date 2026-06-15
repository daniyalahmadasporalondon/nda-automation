"""LIVE end-to-end proof of the two-tier AI counterparty scrape.

This is a PROOF test (CP-Verify lane), not a unit test of ``counterparty_extraction``.
It drives the REAL shipping review entry point -- ``build_ai_first_review_result``
in ``nda_automation.ai_first_review`` -- with a stubbed tier-1 reviewer and an
instrumented tier-2 verifier, and proves four things from running code:

  1. The shipping path INVOKES BOTH TIERS, in order: tier 1 (the reviewer/extractor)
     fires first, then tier 2 (the adversarial verifier) fires -- and tier 2 receives
     TIER 1's extracted name as the claim under test (it checks tier 1's output, not
     independent garbage).
  2. Verifier AFFIRMS  -> result["counterparty"]["verified"] is True, name preserved.
  3. Verifier REFUTES  -> verified is False (it FLIPS) and the refuted name is NOT
     trusted per the fallback contract.
  4. It is LIVE, not dead code: reached through ``build_ai_first_review_result``,
     gated on ``verify`` (verify=False -> empty/unverified block, no tier fires).

No real network/API calls -- both models are stubbed deterministically. The seams
patched are the prod resolvers inside ``counterparty_extraction`` (the same names the
shipping path resolves through when ``extract_counterparty`` is called with no
injected reviewer/verifier): ``_resolve_reviewer`` (tier 1), and ``verifier_enabled``
+ ``resolve_verifier`` (tier 2). The tier-2 stub is a real ``OpenRouterVerifier``
subclass so the shipping path's ``isinstance(..., OpenRouterVerifier)`` AI-verifier
gate passes WITHOUT a network call.
"""

from __future__ import annotations

import unittest
from typing import Any
from unittest import mock

from nda_automation import counterparty_extraction
from nda_automation.ai_first_review import build_ai_first_review_result
from nda_automation.ai_verifier import OpenRouterVerifier

# The known counterparty the stubbed tier-1 reviewer "extracts" from the preamble.
KNOWN_COUNTERPARTY = "Coverstack"

# A preamble paragraph that names BOTH parties (us = Aspora, them = Coverstack),
# followed by two numbered clauses so build_contract_structure isolates p1 as the
# kind="preamble" section -- exactly the section the shipping path feeds the scrape.
PREAMBLE_PARAGRAPHS = [
    {
        "id": "p1",
        "index": 0,
        "text": (
            "This Non-Disclosure Agreement is entered into by and between Aspora "
            'Technology Services Private Limited ("Aspora") and Coverstack '
            'Technologies Inc. ("Coverstack").'
        ),
    },
    {
        "id": "p2",
        "index": 1,
        "text": "1. Definitions. Confidential Information means any information disclosed.",
    },
    {
        "id": "p3",
        "index": 2,
        "text": (
            "2. Governing Law. This Agreement shall be governed by the laws of "
            "England and Wales."
        ),
    },
]
SOURCE_TEXT = "\n".join(paragraph["text"] for paragraph in PREAMBLE_PARAGRAPHS)


class _InstrumentedReviewer:
    """Tier-1 stub: the reviewer model that names the parties from the preamble.

    Stands in for the prod OpenRouter reviewer. Records that it was called and the
    order in which it fired, and returns a fixed strict-JSON extraction naming
    KNOWN_COUNTERPARTY as the counterparty.
    """

    def __init__(self, call_log: list[str], counterparty: str = KNOWN_COUNTERPARTY) -> None:
        self._call_log = call_log
        self._counterparty = counterparty
        self.received_request: dict[str, Any] | None = None

    def __call__(self, request_body: Any) -> dict[str, Any]:
        self._call_log.append("TIER1_REVIEWER")
        self.received_request = dict(request_body) if isinstance(request_body, dict) else {}
        return {
            "first_party": "Aspora",
            "second_party": f"{self._counterparty} Technologies Inc.",
            "counterparty": self._counterparty,
            "confidence": 0.95,
        }


class _InstrumentedVerifier(OpenRouterVerifier):
    """Tier-2 stub: the independent adversarial verifier.

    Subclasses the real OpenRouterVerifier so the shipping path's
    ``isinstance(active_verifier, OpenRouterVerifier)`` AI-verifier gate passes,
    but overrides ``__call__`` to return a deterministic verdict WITHOUT any network
    call. Captures the packet it receives so the test can prove tier 2 was handed
    tier 1's extracted name.
    """

    def __init__(self, call_log: list[str], verdict: str) -> None:
        super().__init__(api_key="stub-key-not-used")
        self._call_log = call_log
        self._verdict = verdict
        self.received_packet: dict[str, Any] | None = None

    def __call__(self, packet: Any) -> dict[str, Any]:
        self._call_log.append("TIER2_VERIFIER")
        self.received_packet = dict(packet) if isinstance(packet, dict) else {}
        return {"verdict": self._verdict, "rationale": "stubbed deterministic verdict"}


def _run_shipping_review(
    call_log: list[str],
    reviewer: _InstrumentedReviewer,
    verifier: _InstrumentedVerifier | None,
    *,
    verify: bool = True,
) -> dict[str, Any]:
    """Drive the REAL shipping entry point with the two tiers patched at their seams.

    Patches the prod resolvers *inside counterparty_extraction* -- the exact names the
    shipping path flows through when it calls ``extract_counterparty`` with no injected
    reviewer/verifier. ``verifier_enabled`` is forced True and ``resolve_verifier``
    returns our AI-subclass stub so the live cross-check takes the AI branch offline.
    """
    patches = [
        mock.patch.object(counterparty_extraction, "_resolve_reviewer", return_value=reviewer),
        mock.patch.object(counterparty_extraction, "verifier_enabled", return_value=verify),
    ]
    if verifier is not None:
        patches.append(
            mock.patch.object(counterparty_extraction, "resolve_verifier", return_value=verifier)
        )
    with patches[0], patches[1], (patches[2] if verifier is not None else _nullcontext()):
        return build_ai_first_review_result(
            SOURCE_TEXT,
            [],  # no clause assessments needed; the scrape runs off contract_structure
            paragraphs=PREAMBLE_PARAGRAPHS,
            checked_at="2026-06-15T00:00:00+00:00",
            verify=verify,
        )


class _nullcontext:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> bool:
        return False


class TwoTierCounterpartyLiveProof(unittest.TestCase):
    def test_both_tiers_fire_in_order_and_tier2_checks_tier1_on_affirm(self) -> None:
        """POINT 1 + POINT 2: both tiers fire in order; tier 2 judges tier 1's name; AFFIRM -> verified True."""
        call_log: list[str] = []
        reviewer = _InstrumentedReviewer(call_log)
        verifier = _InstrumentedVerifier(call_log, verdict="affirm")

        result = _run_shipping_review(call_log, reviewer, verifier)
        counterparty = result["counterparty"]

        # --- POINT 1: BOTH tiers fired, in order tier1 -> tier2 ---
        self.assertIn("TIER1_REVIEWER", call_log, "tier 1 (reviewer/extractor) never fired")
        self.assertIn("TIER2_VERIFIER", call_log, "tier 2 (verifier) never fired")
        self.assertEqual(
            call_log,
            ["TIER1_REVIEWER", "TIER2_VERIFIER"],
            "tiers must fire in order: extractor first, then verifier",
        )

        # --- POINT 1 (the crux): tier 2 was handed TIER 1's extracted name ---
        self.assertIsNotNone(verifier.received_packet, "tier 2 received no packet")
        finding = str(verifier.received_packet.get("engine_finding", ""))
        self.assertIn(
            KNOWN_COUNTERPARTY,
            finding,
            "tier 2 must cross-check tier 1's extracted name, not independent input",
        )
        # The preamble tier 1 read is the same text tier 2 judges against.
        self.assertIn(KNOWN_COUNTERPARTY, str(verifier.received_packet.get("matched_text", "")))

        # --- POINT 2: AFFIRM -> verified True, name preserved ---
        self.assertEqual(counterparty["name"], KNOWN_COUNTERPARTY)
        self.assertIs(counterparty["verified"], True)

        print(
            f"\n[AFFIRM] TIER1 extracted: {KNOWN_COUNTERPARTY} "
            f"-> TIER2 received name in packet: {KNOWN_COUNTERPARTY} "
            f"-> TIER2 verdict: AFFIRM "
            f"-> verified={counterparty['verified']} name={counterparty['name']!r} "
            f"| call order: {' -> '.join(call_log)}"
        )

    def test_refute_flips_verified_false_and_name_not_trusted(self) -> None:
        """POINT 3: same tier-1 name, but verifier REFUTES -> verified flips to False (name not trusted)."""
        call_log: list[str] = []
        reviewer = _InstrumentedReviewer(call_log)
        verifier = _InstrumentedVerifier(call_log, verdict="refute")

        result = _run_shipping_review(call_log, reviewer, verifier)
        counterparty = result["counterparty"]

        # Both tiers still fired in order (tier 2 ran ON tier 1's name) ...
        self.assertEqual(call_log, ["TIER1_REVIEWER", "TIER2_VERIFIER"])
        self.assertIn(KNOWN_COUNTERPARTY, str(verifier.received_packet.get("engine_finding", "")))

        # ... but the refuted name is NOT confidently finalized: verified FLIPS to False.
        self.assertIs(
            counterparty["verified"],
            False,
            "a REFUTED counterparty must not be trusted (verified must flip to False)",
        )

        print(
            f"\n[REFUTE] TIER1 extracted: {KNOWN_COUNTERPARTY} "
            f"-> TIER2 received name in packet: {KNOWN_COUNTERPARTY} "
            f"-> TIER2 verdict: REFUTE "
            f"-> verified={counterparty['verified']} (FLIPPED, name not trusted) "
            f"| call order: {' -> '.join(call_log)}"
        )

    def test_affirm_vs_refute_same_name_only_verified_differs(self) -> None:
        """Side-by-side: identical tier-1 extraction, verdict is the ONLY thing that moves verified."""
        affirm_log: list[str] = []
        affirm = _run_shipping_review(
            affirm_log,
            _InstrumentedReviewer(affirm_log),
            _InstrumentedVerifier(affirm_log, verdict="affirm"),
        )["counterparty"]

        refute_log: list[str] = []
        refute = _run_shipping_review(
            refute_log,
            _InstrumentedReviewer(refute_log),
            _InstrumentedVerifier(refute_log, verdict="refute"),
        )["counterparty"]

        # Tier 1 produced the same name both times; the verifier verdict is the lever.
        self.assertEqual(affirm["name"], KNOWN_COUNTERPARTY)
        self.assertIs(affirm["verified"], True)
        self.assertIs(refute["verified"], False)

        print(
            f"\n[COMPARE] same TIER1 name {KNOWN_COUNTERPARTY!r}: "
            f"AFFIRM -> verified={affirm['verified']} | "
            f"REFUTE -> verified={refute['verified']} "
            "(verdict is the only lever)"
        )

    def test_gated_on_verify_no_tier_fires_when_disabled(self) -> None:
        """POINT 4: verify=False -> empty/unverified block, and NEITHER tier fires (proves the gate is live)."""
        call_log: list[str] = []
        reviewer = _InstrumentedReviewer(call_log)
        verifier = _InstrumentedVerifier(call_log, verdict="affirm")

        result = _run_shipping_review(call_log, reviewer, verifier, verify=False)
        counterparty = result["counterparty"]

        self.assertEqual(call_log, [], "no tier may fire when the scrape is gated off")
        self.assertEqual(counterparty["name"], "")
        self.assertIs(counterparty["verified"], False)

        print(
            f"\n[GATE] verify=False -> tiers fired: {call_log or 'NONE'} "
            f"-> counterparty={{name:{counterparty['name']!r}, verified:{counterparty['verified']}}} "
            "(scrape is gated on verify -> live, not always-on)"
        )


if __name__ == "__main__":
    unittest.main()
