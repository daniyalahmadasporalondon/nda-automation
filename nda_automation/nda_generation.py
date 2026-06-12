"""Generate a company NDA from the Generic NDA template + the Playbook.

The generation engine fills the company's Generic NDA template (the structural
frame and boilerplate) with deal variables, and aligns the *substantive*
clauses to the live Playbook — the Playbook is the authority on clause wording,
the template is the authority on structure. The output is a ``.docx`` plus a
machine-readable manifest describing every fill, so the result can be verified
deterministically and saved as a tracked artifact.

Division of authority (confirmed in the Phase-1 mapping):

* **Template** owns the frame: recitals, the boilerplate clauses (NO OBLIGATION,
  USE & NON-DISCLOSURE, COPIES, IP, REMEDIES, CONFIRMATIONS, NO WARRANTIES,
  RETURN, WAIVER/entire-agreement, SEVERABILITY), the party roles (Aspora is
  always the SECOND party, the counterparty the FIRST), and the signature block.
* **Playbook** owns the substantive positions. Two template clauses drift off
  the Playbook and are realigned at generation time:
    - **Term and survival**: the template caps at "two (2) years" with no
      survival carve-out; the Playbook caps the term at ``max_term_years`` and
      requires a trade-secret / legal / data-protection survival carve-out.
    - **Confidential Information exclusions**: the template lists three carve-outs
      and omits the Playbook's "independently developed without use of
      Confidential Information" exclusion.
* **non_circumvention** is a *prohibited* Playbook position. The template omits
  it by design; generation must never introduce one.
* **Governing law** fills from the chosen entity's approved position.

The deterministic core fills variables and realigns the two clauses so the
output passes the Playbook with zero failures. An optional :class:`ClauseAdapter`
seam lets an AI adapt phrasing to the deal *without* changing which position the
clause takes — adaptation is constrained to the slots, never the substance.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from docx import Document
from docx.document import Document as DocxDocument

from .checks.common import _year_count_label
from .playbook_runtime import ActivePlaybookBundle

# The tracked template asset (the company's Generic NDA). Resolved relative to
# this module so it works from any worktree / install location.
TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "generic_nda.docx"

# NDA posture. v1 builds the mutual path (the template is mutual). One-way is a
# possible add-on; it is declared here so callers and manifests can name it, but
# only ``mutual`` is implemented.
NDA_TYPE_MUTUAL = "mutual"
NDA_TYPE_ONE_WAY = "one_way"

# Playbook clause ids whose substantive wording this engine realigns.
CLAUSE_TERM = "term_and_survival"
CLAUSE_CONFIDENTIAL = "confidential_information"
CLAUSE_GOVERNING_LAW = "governing_law"

# --------------------------------------------------------------------------- #
# Untrusted free-text sanitisation
# --------------------------------------------------------------------------- #
# intake.purpose and intake.business_description are free text from the caller
# that gets filled verbatim into the recital / [BUSINESS DESCRIPTION] slot. That
# makes them an injection surface: a caller (or an upstream prompt-injection) can
# put a prohibited LEGAL POSITION (non-solicit, non-compete, non-circumvention,
# exclusivity, IP assignment, penalty, perpetual confidentiality), a one-way ask,
# or a "drafter instruction" into those fields and have it land in the generated
# NDA. The deterministic clause engine never invents these positions, but it also
# does nothing to stop tainted free text reaching the document. So we neutralise
# the offending content at the fill boundary, on every path (deterministic, AI,
# frozen). Each pattern pairs a label (for the manifest/audit) with a regex over
# the lowercased field; a hit means the field is replaced with a safe value.
_PROHIBITED_FREE_TEXT_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("non_compete", re.compile(r"non-?compete|not\b[^.]{0,40}\bcompete\b|engage in any (?:competing|business that competes)|competing business", re.IGNORECASE)),
    ("non_solicit", re.compile(r"non-?solicit|\bsolicit(?:s|ing)?\b[^.]{0,20}\b(?:employ|staff|hire|personnel)|(?:not|never|refrain from)[^.]{0,30}\bsolicit|solicit or hire", re.IGNORECASE)),
    ("non_circumvention", re.compile(r"non-?circumvent|circumvent or bypass|bypass (?:the disclosing party|us)|deal directly with", re.IGNORECASE)),
    ("exclusivity", re.compile(r"\bexclusiv(?:e|ity)\b|deal exclusively|sole and exclusive", re.IGNORECASE)),
    ("ip_assignment", re.compile(r"\bassigned? to\b|assignment of (?:all )?intellectual property|all (?:right,? )?title and interest", re.IGNORECASE)),
    ("penalty", re.compile(r"liquidated damages|\bpenalt(?:y|ies)\b|punitive damages", re.IGNORECASE)),
    ("perpetual_confidentiality", re.compile(r"in perpetuity|perpetual(?:ly)?\b|indefinitely\b|never expire|forever\b", re.IGNORECASE)),
    ("one_way", re.compile(r"one-?way nda|binding only (?:on|the) (?:the )?receiving party|only (?:the )?receiving party|we receive only", re.IGNORECASE)),
    ("drafter_instruction", re.compile(r"ignore (?:all )?(?:prior|previous) instructions|note to drafter|you are drafting|add a clause|do not mention", re.IGNORECASE)),
)

# Safe neutral replacements when an injection dominates the field. These keep the
# recital/description on-position and readable rather than blanking the document.
_SAFE_PURPOSE = "the proposed business relationship between the parties"
_SAFE_BUSINESS_DESCRIPTION = "its business activities"


def sanitize_free_text(value: str, *, field_name: str, fallback: str) -> tuple[str, str]:
    """Neutralise untrusted free text before it is filled into the document.

    Returns ``(safe_value, note)``: when the input carries a prohibited position,
    a one-way ask, or a drafter instruction, the whole field is replaced with the
    safe ``fallback`` (surgical excision of a span risks leaving a half-sentence
    that still reads as a position, so we substitute the field wholesale) and a
    note like ``"purpose: replaced injected content (non_solicit)"`` is returned
    for the manifest. Clean input passes through unchanged with an empty note.
    """

    text = str(value or "").strip()
    if not text:
        return fallback, ""
    hits = [label for label, pattern in _PROHIBITED_FREE_TEXT_PATTERNS if pattern.search(text)]
    if not hits:
        return text, ""
    note = f"{field_name}: replaced injected content ({', '.join(sorted(set(hits)))})"
    return fallback, note


def _sanitize_intake(intake: "CounterpartyIntake", manifest: "GenerationManifest") -> "CounterpartyIntake":
    """Return an intake whose free-text fields are safe to fill into the document.

    Only the two free-text slots that flow into prose (purpose, business
    description) are sanitised; identity fields (names/addresses) are filled into
    structured slots and checked by the entity gate, not the position scan. Any
    neutralisation is recorded on the manifest so it is auditable downstream.
    """

    safe_purpose, purpose_note = sanitize_free_text(
        intake.purpose, field_name="purpose", fallback=_SAFE_PURPOSE
    )
    safe_business, business_note = sanitize_free_text(
        intake.business_description, field_name="business_description", fallback=_SAFE_BUSINESS_DESCRIPTION
    )
    for note in (purpose_note, business_note):
        if note:
            manifest.sanitized_fields.append(note)
    if not purpose_note and not business_note:
        return intake
    from dataclasses import replace  # noqa: PLC0415

    return replace(intake, purpose=safe_purpose, business_description=safe_business)


class NdaGenerationError(ValueError):
    """Raised when generation inputs are invalid or the template is malformed."""


@dataclass(frozen=True)
class EntityParty:
    """The Aspora signing entity (the SECOND party) for this NDA.

    Mirrors the fields the engine consumes from an ``entity_registry`` bundle —
    constructed via :func:`entity_party_from_bundle` so the registry stays the
    single source of entity truth.
    """

    legal_name: str
    registered_office: str
    jurisdiction_of_incorporation: str
    governing_law_value: str
    forum: str
    # Empty signatory name/title means "no signer assigned" — the signature block
    # renders blank fill-lines (underscores) for signing, never bracketed text.
    signatory_name: str = ""
    signatory_title: str = ""
    entity_id: str = ""
    # The registry address id actually written into ``registered_office`` (the
    # user's pick, or the default). Recorded on the manifest for provenance parity
    # with the governing-law override.
    registered_office_address_id: str = ""


@dataclass(frozen=True)
class CounterpartyIntake:
    """The counterparty (FIRST party) + deal variables, from draft-ui intake."""

    company_name: str
    registered_office: str
    jurisdiction_of_incorporation: str
    business_description: str
    purpose: str
    term_years: int = 2
    nda_type: str = NDA_TYPE_MUTUAL
    agreement_date: _dt.date | None = None


@dataclass
class GenerationManifest:
    """Ground-truth record of what was filled — for verification + provenance.

    The verifier (gen-verify) diffs its structural / entity assertions against
    this instead of re-parsing the prose, so a fill is checked against intent
    rather than against a regex over the output.
    """

    entity_id: str
    entity_legal_name: str
    counterparty_name: str
    nda_type: str
    term_years: int
    agreement_date: str
    governing_law_value: str
    forum: str
    # The registry address id whose lines were written as the entity's registered
    # office. Records the user's address pick (or the default) for provenance parity
    # with the governing-law override, so gen-verify / audit can confirm WHICH office
    # was used rather than re-parsing the prose.
    entity_address_id: str = ""
    slot_fills: dict[str, str] = field(default_factory=dict)
    clause_alignments: list[str] = field(default_factory=list)
    # Free-text intake fields whose untrusted content was neutralised before it
    # could reach the document, e.g. "purpose: removed prohibited position
    # (non_solicit)". Empty in the normal case; auditable when an injection was
    # caught (the UI can surface that the purpose/description was sanitised).
    sanitized_fields: list[str] = field(default_factory=list)
    # Governing-law provenance. ``governing_law_value`` above is always the
    # EFFECTIVE law written into the clause; ``governing_law_option_id`` is its
    # Playbook approved-option id (the join key). When the user picked a non-default
    # (but still Playbook-approved) law, ``governing_law_overridden`` is True and
    # ``entity_default_governing_law_value`` records the entity's registry default,
    # so gen-verify validates against the INTENDED law rather than flagging it as a
    # mismatch.
    governing_law_option_id: str = ""
    governing_law_overridden: bool = False
    entity_default_governing_law_value: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "entity_legal_name": self.entity_legal_name,
            "counterparty_name": self.counterparty_name,
            "nda_type": self.nda_type,
            "term_years": self.term_years,
            "agreement_date": self.agreement_date,
            "governing_law_value": self.governing_law_value,
            "forum": self.forum,
            "entity_address_id": self.entity_address_id,
            "slot_fills": dict(self.slot_fills),
            "clause_alignments": list(self.clause_alignments),
            "sanitized_fields": list(self.sanitized_fields),
            "governing_law_option_id": self.governing_law_option_id,
            "governing_law_overridden": self.governing_law_overridden,
            "entity_default_governing_law_value": self.entity_default_governing_law_value,
        }


@dataclass
class GenerationResult:
    """The generated NDA bytes + its manifest."""

    docx_bytes: bytes
    manifest: GenerationManifest


class ClauseAdapter(Protocol):
    """Optional AI seam: adapt clause phrasing to the deal, on-position only.

    ``adapt(clause_id, playbook_text, context)`` returns adapted text for the
    given Playbook clause. The deterministic core supplies ``playbook_text`` as
    the authoritative position; an adapter may polish phrasing for the deal but
    must keep the position. When no adapter is supplied the Playbook text is used
    verbatim, which is why the engine is fully testable offline.
    """

    def adapt(self, clause_id: str, playbook_text: str, context: Mapping[str, Any]) -> str: ...


def entity_party_from_bundle(
    bundle: Mapping[str, Any],
    playbook: Mapping[str, Any],
    *,
    governing_law_option_id: str = "",
    address_id: str = "",
) -> EntityParty:
    """Build an :class:`EntityParty` from an ``entity_registry`` bundle.

    Resolves the governing-law *value* through the Playbook so the slot text
    always matches an approved option: the bundle carries a
    ``governing_law.playbook_option_id`` which joins onto the Playbook's
    ``governing_law.rules.approved_options[].id``. A bundle whose option is not
    approved is rejected — generation never emits an off-position governing law.

    ``governing_law_option_id`` overrides the entity's default law with another
    Playbook-approved option (the product lets the user pick a different — still
    approved — governing law). An unapproved override is rejected. When the law is
    overridden, the forum tracks the overridden law (rather than the entity's
    default courts) so the draft never pairs one jurisdiction's law with another's
    forum.

    ``address_id`` selects which registry address fills the entity's registered
    office (the product lets the user pick a non-default office). A blank id falls
    back to the default-flagged address; an unknown id is rejected (same guard shape
    as the governing-law override). The chosen id is carried on the returned
    :class:`EntityParty` so the manifest can record it for provenance.
    """

    legal_name = str(bundle.get("legal_name") or "").strip()
    if not legal_name:
        raise NdaGenerationError("Entity bundle is missing legal_name.")

    approved = _approved_governing_law_options(playbook)
    default_option_id = str((bundle.get("governing_law") or {}).get("playbook_option_id") or "").strip()
    if default_option_id not in approved:
        raise NdaGenerationError(
            f"Entity governing-law option {default_option_id!r} is not an approved Playbook option "
            f"(approved: {sorted(approved)})."
        )

    override_id = str(governing_law_option_id or "").strip()
    overridden = bool(override_id) and override_id != default_option_id
    if override_id and override_id not in approved:
        raise NdaGenerationError(
            f"Governing-law override {override_id!r} is not an approved Playbook option "
            f"(approved: {sorted(approved)})."
        )
    option_id = override_id or default_option_id
    governing_law_value = approved[option_id]

    address, chosen_address_id = select_bundle_address(bundle, address_id)
    signatory = bundle.get("signatory") or {}
    # The entity's registered forum is correct for its default law. When the law is
    # overridden to a different option, derive the forum that goes WITH the chosen
    # option (each approved option's proper forum is registered against the entity
    # that defaults to it, e.g. england_and_wales -> "Courts of England and Wales"),
    # so we never pair one jurisdiction's law with another's courts. If that lookup
    # is unavailable, fall back to the law value rather than the wrong forum.
    if not overridden:
        forum = str(bundle.get("jurisdiction") or "").strip() or governing_law_value
    else:
        forum = _forum_for_option_id(option_id) or governing_law_value

    return EntityParty(
        legal_name=legal_name,
        registered_office=address,
        jurisdiction_of_incorporation=_incorporation_jurisdiction(bundle, governing_law_value),
        governing_law_value=governing_law_value,
        forum=forum,
        # The registry ships placeholder signatory strings ("[Authorised Signatory]"
        # / "[Title]") when no real signer is assigned; treat those as unassigned so
        # the signature block renders clean blank fill-lines, not bracketed text.
        signatory_name=_real_or_blank(signatory.get("name")),
        signatory_title=_real_or_blank(signatory.get("title")),
        entity_id=str(bundle.get("id") or "").strip(),
        registered_office_address_id=chosen_address_id,
    )


def _forum_for_option_id(option_id: str) -> str:
    """The proper forum/jurisdiction for a governing-law option.

    The Playbook approved_options carry only id/label/value (no forum), but the
    registry does: each entity registers a ``jurisdiction`` that goes WITH its
    default governing-law option (e.g. england_and_wales -> "Courts of England and
    Wales", difc -> "DIFC Courts, Dubai"). So we resolve an overridden option's
    forum from whichever registry entity defaults to that option. Returns "" if the
    registry is unavailable or no entity defaults to the option, letting the caller
    fall back to the law value (flagged in the manifest as forum == law value)."""

    try:
        from . import entity_registry  # noqa: PLC0415

        for bundle in entity_registry.list_entities():
            bundle_option = str((bundle.get("governing_law") or {}).get("playbook_option_id") or "").strip()
            if bundle_option == option_id:
                forum = str(bundle.get("jurisdiction") or "").strip()
                if forum:
                    return forum
    except Exception:  # noqa: BLE001 - registry absent/!importable -> caller falls back
        return ""
    return ""


def _real_or_blank(value: object) -> str:
    """Return a real signatory value, or "" if it is empty or a bracketed placeholder.

    The registry uses ``"[Authorised Signatory]"`` / ``"[Title]"`` as placeholders
    for an unassigned signer. We map those (and any ``[...]`` form) to an empty
    string so the signature block renders blank fill-lines for signing.
    """

    text = str(value or "").strip()
    if not text or (text.startswith("[") and text.endswith("]")):
        return ""
    return text


def _incorporation_jurisdiction(bundle: Mapping[str, Any], governing_law_value: str) -> str:
    """The entity's jurisdiction of incorporation.

    Consumes the registry's explicit ``incorporation_jurisdiction`` field when
    present (the authoritative value, e.g. "Delaware, United States" for a
    Delaware LLC). Falls back to the governing-law value, which the registry
    confirms matches incorporation for all current entities.
    """

    explicit = str(bundle.get("incorporation_jurisdiction") or "").strip()
    return explicit or governing_law_value


def generate_nda(
    entity: EntityParty,
    intake: CounterpartyIntake,
    *,
    playbook: Mapping[str, Any],
    template_path: Path | str = TEMPLATE_PATH,
    clause_adapter: ClauseAdapter | None = None,
) -> GenerationResult:
    """Generate the NDA ``.docx`` + manifest from the template and Playbook.

    This is the core seam: ``generate_nda(entity, intake) -> (docx, manifest)``.
    Saving the result as a tracked artifact is a separate, optional step
    (:func:`save_generated_nda`) so this function has no storage dependency and
    runs fully offline.
    """

    if intake.nda_type != NDA_TYPE_MUTUAL:
        # The template is mutual; one-way support is not implemented in v1.
        raise NdaGenerationError(
            f"nda_type {intake.nda_type!r} is not supported yet; only {NDA_TYPE_MUTUAL!r} is implemented."
        )

    term_years = _resolve_term_years(intake.term_years, playbook)
    agreement_date = intake.agreement_date or _dt.date.today()

    document = _load_template(template_path)

    manifest = GenerationManifest(
        entity_id=entity.entity_id,
        entity_legal_name=entity.legal_name,
        counterparty_name=intake.company_name,
        nda_type=intake.nda_type,
        term_years=term_years,
        agreement_date=agreement_date.isoformat(),
        governing_law_value=entity.governing_law_value,
        forum=entity.forum,
        entity_address_id=entity.registered_office_address_id,
    )

    # Neutralise untrusted free text BEFORE it reaches either the document fills
    # or the AI adapter context, so an injected prohibited position / one-way ask
    # / drafter instruction can never land in the recital or steer the adapter.
    intake = _sanitize_intake(intake, manifest)

    _fill_variable_slots(document, entity, intake, agreement_date, manifest)
    _align_mutuality(document, playbook, clause_adapter, intake, manifest)
    _align_confidential_information(document, playbook, clause_adapter, intake, manifest)
    _align_term_and_survival(document, playbook, term_years, clause_adapter, intake, manifest)

    # Unassigned signatories render as blank fill-lines (not bracketed text), so
    # no bracketed values are ever legitimately retained — the guard is strict.
    _assert_no_unfilled_placeholders(document, allowed=set())

    return GenerationResult(docx_bytes=_document_bytes(document), manifest=manifest)


def save_generated_nda(
    result: GenerationResult,
    matter_id: str,
    *,
    add_artifact: Callable[..., Any] | None = None,
    repository: Any | None = None,
    based_on_artifact_id: str = "",
    owner_user_id: str = "",
) -> Any:
    """Persist a generated NDA artifact (actor=entity, role=generated).

    The actor is the entity id slug (e.g. ``aspora_technology``) — stable and
    short, so the auto-generated filename stays clean; the full legal name is
    preserved on the manifest metadata. The role is ``generated``;
    ``based_on_artifact_id`` records lineage when the generation derives from an
    existing matter artifact (e.g. the original), and is optional because a
    generated NDA is often the matter's first document.

    ``add_artifact`` defaults to the live ``artifact_service.add_artifact`` but
    stays injectable so the engine can be tested without the artifact registry.
    The import is deferred so importing this module never requires artifact-spine.
    """

    if add_artifact is None:
        from .artifact_service import add_artifact as add_artifact  # noqa: PLC0415

    actor = result.manifest.entity_id or result.manifest.entity_legal_name or "aspora"
    return add_artifact(
        matter_id,
        source="generated",
        actor=actor,
        role="generated",
        document_bytes=result.docx_bytes,
        based_on_artifact_id=based_on_artifact_id,
        repository=repository,
        owner_user_id=owner_user_id,
        metadata={"generation": result.manifest.to_dict()},
    )


def generate_nda_for_entity(
    entity_id: str,
    intake: CounterpartyIntake,
    *,
    playbook: Mapping[str, Any] | None = None,
    playbook_bundle: ActivePlaybookBundle | None = None,
    clause_adapter: ClauseAdapter | None = None,
    use_ai: bool = True,
    use_frozen: bool = False,
    governing_law_override: str = "",
    address_id: str = "",
) -> GenerationResult:
    """Resolve the entity from the registry and generate the NDA (no save).

    The single convenience entry the HTTP route, gen-verify, and tests share:
    ``entity_id`` + intake -> ``GenerationResult``. Resolves the entity via the
    live ``entity_registry`` and builds the AI clause adapter when ``use_ai`` and
    no explicit adapter is given (Playbook-bounded; falls back to deterministic
    with no API key). The live ``entity_registry`` import is deferred so this
    module imports cleanly where the registry is absent.

    ``use_frozen`` selects the repeatable golden-fixture adapter instead of the
    live (non-deterministic) AI adapter: same guarded AI codepath, but replaying
    recorded on-position clause text, so gen-verify can gate the AI-shaped output
    deterministically. An explicit ``clause_adapter`` overrides both.

    ``governing_law_override`` is a Playbook governing-law option id; when given
    and different from the entity default, the draft uses that (still approved)
    law and the manifest records the override provenance (``governing_law_overridden``
    + ``entity_default_governing_law_value``) so gen-verify validates the chosen
    law rather than flagging an entity mismatch.

    ``address_id`` is the registry address id the user picked for the entity's
    registered office; blank uses the entity default, an unknown id is rejected (see
    :func:`entity_party_from_bundle`). The chosen id lands on the manifest
    (``entity_address_id``).
    """

    from . import entity_registry  # noqa: PLC0415

    if playbook is None and playbook_bundle is not None:
        playbook = playbook_bundle.playbook
    if playbook is None:
        from .checker import load_playbook  # noqa: PLC0415

        playbook = load_playbook()

    if clause_adapter is None and use_frozen:
        from .nda_generation_ai import build_frozen_clause_adapter  # noqa: PLC0415

        clause_adapter = build_frozen_clause_adapter()
    elif clause_adapter is None and use_ai:
        from .nda_generation_ai import build_clause_adapter  # noqa: PLC0415

        clause_adapter = build_clause_adapter()

    bundle = entity_registry.get_entity(entity_id)
    if bundle is None:
        raise NdaGenerationError(f"Unknown signing entity {entity_id!r}.")

    approved = _approved_governing_law_options(playbook)
    default_option_id = str((bundle.get("governing_law") or {}).get("playbook_option_id") or "").strip()
    entity_default_law = approved.get(default_option_id, "")
    override_id = str(governing_law_override or "").strip()
    overridden = bool(override_id) and override_id != default_option_id
    effective_option_id = override_id or default_option_id

    entity = entity_party_from_bundle(
        bundle, playbook, governing_law_option_id=override_id, address_id=address_id
    )
    result = generate_nda(entity, intake, playbook=playbook, clause_adapter=clause_adapter)
    # Record governing-law provenance on the manifest (the effective law value +
    # forum are already on the manifest; these let gen-verify validate against the
    # INTENDED option rather than the entity default).
    result.manifest.governing_law_option_id = effective_option_id
    result.manifest.governing_law_overridden = overridden
    result.manifest.entity_default_governing_law_value = entity_default_law
    return result


def generate_and_save_nda(
    entity_id: str,
    intake: CounterpartyIntake,
    matter_id: str,
    *,
    playbook: Mapping[str, Any] | None = None,
    playbook_bundle: ActivePlaybookBundle | None = None,
    repository: Any | None = None,
    based_on_artifact_id: str = "",
    owner_user_id: str = "",
    clause_adapter: ClauseAdapter | None = None,
    use_ai: bool = True,
    governing_law_override: str = "",
    address_id: str = "",
) -> tuple[GenerationResult, Any]:
    """End-to-end: resolve the entity, generate the NDA, save it as an artifact.

    Wires the live ``entity_registry`` (the single source of entity truth) and
    ``artifact_service`` so a caller only needs an ``entity_id`` + intake +
    ``matter_id``. Returns ``(result, artifact)``. ``governing_law_override`` is a
    Playbook governing-law option id (see :func:`generate_nda_for_entity`).
    """

    if playbook is None and playbook_bundle is not None:
        playbook = playbook_bundle.playbook
    if playbook is None:
        from .checker import load_playbook  # noqa: PLC0415

        playbook = load_playbook()

    result = generate_nda_for_entity(
        entity_id,
        intake,
        playbook=playbook,
        playbook_bundle=playbook_bundle,
        clause_adapter=clause_adapter,
        use_ai=use_ai,
        governing_law_override=governing_law_override,
        address_id=address_id,
    )
    # Hard pre-save gate: never persist an off-position draft. The clause adapter
    # is guarded and the intake is sanitised, but this is the last, independent
    # backstop on the SHIP path — a generated NDA that fails its own Playbook or
    # carries a prohibited position must not become a saved artifact.
    assert_generated_nda_is_on_position(result, playbook)
    artifact = save_generated_nda(
        result,
        matter_id,
        repository=repository,
        based_on_artifact_id=based_on_artifact_id,
        owner_user_id=owner_user_id,
    )
    return result, artifact


# --------------------------------------------------------------------------- #
# Pre-save ship gate — the last backstop before a draft becomes an artifact
# --------------------------------------------------------------------------- #
# The prohibited-position families are the SHARED canonical set
# (prohibited_positions.PROHIBITED_POSITION_PATTERNS) so this gate, the AI-adapter
# guard, and gen-verify's independent gate all enforce the same bar. A hit anywhere
# in the generated document (outside the permitted narrow survival carve-out) means
# the draft asserts a position the Playbook bans -> refuse to save.

# A clear, stable message the endpoint surfaces when the ship gate trips.
SAFETY_GATE_MESSAGE = "Generated NDA failed the safety gate and was not saved"


def assert_generated_nda_is_on_position(result: "GenerationResult", playbook: Mapping[str, Any] | None = None) -> None:
    """Refuse to ship a generated NDA that fails its own Playbook or carries a
    prohibited position. Raises :class:`NdaGenerationError` (which the endpoint
    surfaces as a 4xx with a "safety gate" message) so an off-position draft never
    reaches storage.

    PUBLIC so every persistence path can call it — both the convenience
    ``generate_and_save_nda`` AND the HTTP route (which builds the matter +
    artifact itself) MUST run this BEFORE any save, or the AI-first safety
    contract has a hole on the actual ship path.

    This is the load-bearing AI-first backstop on the SHIP path: the per-clause
    adapter guard can fall back to deterministic wording, but a drifted live-AI
    clause that slips through (or a prohibited position outside the six scored
    clauses) must still be caught here before save. Two independent screens:
    1. ``self_check_generated_nda`` — zero native fails AND non_circumvention does
       not fail (the deterministic + AI-first oracles). self_check ALONE is not
       enough: it only scores the six hard clauses, so a non-compete appended to
       mutuality leaves mutuality passing — hence screen 2.
    2. a meaning-based prohibited-position scan (the shared canonical set) over the
       whole document, so a position smuggled OUTSIDE the scored clauses is caught.
    The narrow trade-secret / legal / data-protection survival carve-out the
    Playbook permits ("for as long as ... required") is exempt from the perpetual
    screen so the legitimate survival sentence is not mistaken for a position.
    """

    from .docx_text import extract_docx_text  # noqa: PLC0415
    from .prohibited_positions import PROHIBITED_POSITION_PATTERNS  # noqa: PLC0415

    check = self_check_generated_nda(result.docx_bytes, playbook=playbook)
    if not check.passed:
        problems = check.native_failures + check.dynamic_failures
        raise NdaGenerationError(
            f"{SAFETY_GATE_MESSAGE}: failed its own Playbook ("
            + ", ".join(problems or ["unknown failure"])
            + ")."
        )

    text = extract_docx_text(result.docx_bytes)
    # Scan PER SENTENCE so the permitted-survival exemption applies only to the
    # specific sentence that carries the carve-out scope -- not the whole document
    # (the legitimate trade-secret/data-protection survival sentence must not
    # license a perpetual claim made elsewhere in the draft).
    for sentence in _scan_sentences(text):
        for label, pattern in PROHIBITED_POSITION_PATTERNS:
            if not pattern.search(sentence):
                continue
            if label == "perpetual_confidentiality" and _is_permitted_long_survival(sentence):
                continue
            raise NdaGenerationError(
                f"{SAFETY_GATE_MESSAGE}: carries a prohibited position ({label})."
            )


def _scan_sentences(text: str) -> list[str]:
    """Split the document into sentence/line units for the position scan."""
    return [s.strip() for s in re.split(r"(?<=[.;])\s+|\n+", text) if s.strip()]


def _is_permitted_long_survival(sentence: str) -> bool:
    """The Playbook permits a NARROW trade-secret / legal / data-protection
    survival obligation to last as long as the protected status or law requires.
    Perpetual-survival language scoped to that carve-out is on-position; the same
    language applied to ALL confidential information is drift. Mirrors gen-verify's
    permitted-long-survival logic so the ship gate and the verifier agree -- judged
    on the SAME sentence as the perpetual language, not the whole document."""

    lowered = re.sub(r"\s+", " ", sentence.lower())
    carve_outs = (
        "trade secret", "as long as", "so long as", "applicable law", "law requires",
        "legal obligation", "data protection", "data-protection",
    )
    if not any(token in lowered for token in carve_outs):
        return False
    # If the perpetual language in THIS sentence reaches ALL/ANY confidential
    # information it is no longer the narrow carve-out and is not permitted.
    blanket = ("all confidential information", "any and all information", "all information")
    return not any(token in lowered for token in blanket)


# --------------------------------------------------------------------------- #
# Self-check — the same oracle gen-verify uses
# --------------------------------------------------------------------------- #

# The Playbook's hard clauses, split by review engine. NATIVE clauses are scored
# deterministically (checker.review_nda); the DYNAMIC clause is only emitted on
# the AI-first path. These mirror gen-verify's split so both sides measure the
# generated NDA with the SAME oracle.
NATIVE_CLAUSE_IDS = (
    "mutuality",
    "confidential_information",
    "governing_law",
    "term_and_survival",
    "signatures",
)
DYNAMIC_CLAUSE_IDS = ("non_circumvention",)


@dataclass
class SelfCheckResult:
    """Outcome of the generation self-check."""

    passed: bool
    native_failures: list[str] = field(default_factory=list)
    native_reviews: list[str] = field(default_factory=list)
    dynamic_failures: list[str] = field(default_factory=list)
    overall_status: str = ""

    def __bool__(self) -> bool:
        return self.passed


def self_check_generated_nda(
    docx_bytes: bytes,
    *,
    playbook: Mapping[str, Any] | None = None,
) -> SelfCheckResult:
    """Run the generated NDA through the SAME oracle gen-verify uses.

    The native clauses are scored by the deterministic engine with ``verify=False``
    — the trustworthy, network-free oracle. The key-free *stub* AI reviewer is NOT
    used as the native oracle: it rubber-stamps native clauses and downgrades
    ungrounded passes to "review", which would make a stub-based self-check show a
    false green while a real native defect (e.g. missing survival carve-out or a
    broken execution block) slipped through.

    The dynamic ``non_circumvention`` clause is only emitted on the AI-first path,
    so it is checked separately via the key-free AI-first stub: the stub fails that
    clause iff a prohibited restriction is present, so a fail means the draft
    smuggled one in.

    A generated NDA passes iff it has zero native failures AND non_circumvention
    does not fail. (Native "review" verdicts are surfaced but are not failures —
    the generated NDA is expected to score 0 fails / 0 review, but the contract the
    bar enforces is 0 *fails*.)
    """

    from .checker import load_playbook, review_nda  # noqa: PLC0415
    from .docx_text import extract_docx_text  # noqa: PLC0415

    resolved_playbook = playbook if playbook is not None else load_playbook()
    text = extract_docx_text(docx_bytes)

    # Network-free oracle, as the docstring promises: verify=False drops the AI
    # verifier, ai_enabled=False drops the legacy per-clause AI overlay. Without
    # the latter this made ~5 live model calls (~20s) that never changed the
    # native verdict — and made a SAFETY GATE depend on a non-deterministic model.
    native = review_nda(text, playbook=resolved_playbook, verify=False, ai_enabled=False)
    native_by_id = {str(clause.get("id")): clause for clause in native.get("clauses", [])}
    native_failures: list[str] = []
    native_reviews: list[str] = []
    for clause_id in NATIVE_CLAUSE_IDS:
        clause = native_by_id.get(clause_id)
        if clause is None:
            native_failures.append(f"{clause_id} (not emitted by deterministic engine)")
            continue
        decision = str(clause.get("decision"))
        if decision == "fail":
            native_failures.append(clause_id)
        elif decision == "review":
            native_reviews.append(clause_id)

    dynamic_failures = _self_check_non_circumvention(text, resolved_playbook)

    passed = not native_failures and not dynamic_failures
    return SelfCheckResult(
        passed=passed,
        native_failures=native_failures,
        native_reviews=native_reviews,
        dynamic_failures=dynamic_failures,
        overall_status=str(native.get("overall_status") or ""),
    )


def _self_check_non_circumvention(text: str, playbook: Mapping[str, Any]) -> list[str]:
    """Key-free AI-first pass to surface the dynamic non_circumvention clause."""

    from .ai_assessor import (  # noqa: PLC0415
        _validate_ai_assessment_response,
        build_ai_assessment_packet,
        stub_ai_assessment_response,
    )
    from .ai_first_review import build_ai_first_review_result  # noqa: PLC0415

    packet = build_ai_assessment_packet(text, playbook=playbook)
    raw = stub_ai_assessment_response(packet)
    assessments = _validate_ai_assessment_response(raw, playbook=playbook, packet=packet)
    result = build_ai_first_review_result(text, assessments, playbook=playbook)
    by_id = {str(clause.get("id")): clause for clause in result.get("clauses", [])}

    failures: list[str] = []
    for clause_id in DYNAMIC_CLAUSE_IDS:
        clause = by_id.get(clause_id)
        if clause is None:
            failures.append(f"{clause_id} (not emitted by AI-first engine)")
        elif str(clause.get("decision")) == "fail":
            failures.append(clause_id)
    return failures


# --------------------------------------------------------------------------- #
# Variable-slot filling
# --------------------------------------------------------------------------- #


def _fill_variable_slots(
    document: DocxDocument,
    entity: EntityParty,
    intake: CounterpartyIntake,
    agreement_date: _dt.date,
    manifest: GenerationManifest,
) -> None:
    """Fill the 11 template placeholders, party-aware.

    ``[JURISDICTION OF INCORPORATION]`` and ``[REGISTERED OFFICE ADDRESS]`` each
    appear once per party, so they are filled positionally (first occurrence =
    Company / FIRST party, second = Aspora / SECOND party) rather than globally.
    The per-party fills are applied to the two specific party paragraphs; the
    remaining single-occurrence slots are filled globally.
    """

    day, month, year = _split_date(agreement_date)

    # Per-party paragraphs: the Company block names the FIRST party, the Aspora
    # block the SECOND. They are the two paragraphs that introduce each party.
    first_party_done = False
    second_party_done = False
    for paragraph in document.paragraphs:
        text = paragraph.text
        if not first_party_done and "“Company”" in text and "[COMPANY NAME]" in text:
            _set_paragraph_text(
                paragraph,
                _apply(
                    text,
                    {
                        "[COMPANY NAME]": intake.company_name,
                        "[JURISDICTION OF INCORPORATION]": intake.jurisdiction_of_incorporation,
                        "[REGISTERED OFFICE ADDRESS]": intake.registered_office,
                    },
                ),
            )
            first_party_done = True
        elif not second_party_done and "“Aspora”" in text and "[ASPORA ENTITY LEGAL NAME]" in text:
            _set_paragraph_text(
                paragraph,
                _apply(
                    text,
                    {
                        "[ASPORA ENTITY LEGAL NAME]": entity.legal_name,
                        "[JURISDICTION OF INCORPORATION]": entity.jurisdiction_of_incorporation,
                        "[REGISTERED OFFICE ADDRESS]": entity.registered_office,
                    },
                ),
            )
            second_party_done = True

    if not first_party_done:
        raise NdaGenerationError("Template is missing the Company (FIRST party) paragraph.")
    if not second_party_done:
        raise NdaGenerationError("Template is missing the Aspora (SECOND party) paragraph.")

    # Single-occurrence slots, filled across body paragraphs.
    global_fills = {
        "[•] day of [•], [YEAR]": f"{day} day of {month}, {year}",
        "[BUSINESS DESCRIPTION]": intake.business_description,
        "[GOVERNING LAW]": entity.governing_law_value,
    }
    for paragraph in document.paragraphs:
        text = paragraph.text
        if any(token in text for token in global_fills):
            _set_paragraph_text(paragraph, _apply(text, global_fills))

    # Purpose: the recital names the deal purpose. The template phrases the
    # purpose inline ("certain commercial propositions"); we make it concrete.
    for paragraph in document.paragraphs:
        if "certain commercial propositions" in paragraph.text and "“Purpose”" in paragraph.text:
            _set_paragraph_text(
                paragraph,
                paragraph.text.replace("certain commercial propositions", intake.purpose),
            )
            break

    # Signature block (the table): fill the party names + Aspora signatory.
    _fill_signature_table(document, entity, intake)

    manifest.slot_fills.update(
        {
            "[COMPANY NAME]": intake.company_name,
            "[ASPORA ENTITY LEGAL NAME]": entity.legal_name,
            "[JURISDICTION OF INCORPORATION] (first party)": intake.jurisdiction_of_incorporation,
            "[JURISDICTION OF INCORPORATION] (second party)": entity.jurisdiction_of_incorporation,
            "[REGISTERED OFFICE ADDRESS] (first party)": intake.registered_office,
            "[REGISTERED OFFICE ADDRESS] (second party)": entity.registered_office,
            "[BUSINESS DESCRIPTION]": intake.business_description,
            "[GOVERNING LAW]": entity.governing_law_value,
            # Empty -> the signature block renders a blank fill-line for signing.
            "[AUTHORISED SIGNATORY]": entity.signatory_name or "(blank fill-line)",
            "[DESIGNATION]": entity.signatory_title or "(blank fill-line)",
            "purpose": intake.purpose,
            "agreement_date": agreement_date.isoformat(),
        }
    )


def _fill_signature_table(document: DocxDocument, entity: EntityParty, intake: CounterpartyIntake) -> None:
    """Fill + normalise the signature block to the Playbook execution structure.

    The template's block uses "Designation:" and has no "By:"/"Date:" lines, so
    it fails the Playbook's signatures position (which requires both parties, a
    title, and a date — surfaced via ``By:``/``Title:``/``Date:`` markers). We
    rewrite each party cell to the Playbook ``redline_template`` shape while
    keeping the party-specific fills, so the executed block is Playbook-complete.
    """

    if not document.tables:
        raise NdaGenerationError("Template is missing the signature table.")

    table = document.tables[0]
    cells = table.rows[0].cells
    if len(cells) < 2:
        raise NdaGenerationError("Signature table does not have two party blocks.")

    _write_signature_cell(
        cells[0],
        party_name=intake.company_name,
        signatory_name="",
        signatory_title="",
    )
    _write_signature_cell(
        cells[1],
        party_name=entity.legal_name,
        signatory_name=entity.signatory_name,
        signatory_title=entity.signatory_title,
    )


def _write_signature_cell(cell: Any, *, party_name: str, signatory_name: str, signatory_title: str) -> None:
    """Rewrite a signature cell to: header, party name, By/Name/Title/Date lines.

    Mirrors the Playbook ``signatures.redline_template`` so the block carries the
    ``By:`` / ``Title:`` / ``Date:`` markers the signatures checker requires. The
    cell's existing paragraphs are reused in order and any surplus is cleared.
    """

    party_line = f"For {party_name}".strip() if party_name else "For _______________________"
    lines = [
        party_line,
        "By: _______________________________",
        f"Name: {signatory_name}".rstrip() if signatory_name else "Name: _______________________",
        f"Title: {signatory_title}".rstrip() if signatory_title else "Title: _______________________",
        "Date: _______________________",
    ]
    paragraphs = cell.paragraphs
    for index, line in enumerate(lines):
        if index < len(paragraphs):
            _set_paragraph_text(paragraphs[index], line)
        else:
            cell.add_paragraph(line)
    # Clear any leftover template paragraphs beyond the lines we wrote.
    for paragraph in paragraphs[len(lines):]:
        _set_paragraph_text(paragraph, "")


# --------------------------------------------------------------------------- #
# Clause realignment to the Playbook
# --------------------------------------------------------------------------- #


def _align_mutuality(
    document: DocxDocument,
    playbook: Mapping[str, Any],
    clause_adapter: ClauseAdapter | None,
    intake: CounterpartyIntake,
    manifest: GenerationManifest,
) -> None:
    """Make the reciprocal-obligation language explicit per the Playbook.

    The template defines Disclosing/Receiving roles abstractly (recital), but
    leaves the reciprocity implicit, which the Playbook's mutuality position
    flags for review. We insert the Playbook's mutuality statement ("each party
    acts as both a Disclosing Party and a Receiving Party ...") right after the
    role-definition recital so both parties are bound symmetrically.
    """

    clause = _playbook_clause(playbook, "mutuality")
    statement = str(clause.get("redline_template") or "").strip() or (
        "Each party acts as both a Disclosing Party and a Receiving Party with respect to "
        "Confidential Information it discloses or receives, and the confidentiality obligations "
        "under this Agreement bind each party reciprocally."
    )
    if clause_adapter is not None:
        adapted = clause_adapter.adapt("mutuality", statement, _adapter_context(intake)).strip()
        statement = adapted or statement

    # Anchor: the recital that defines the Disclosing/Receiving roles.
    for paragraph in document.paragraphs:
        if "Disclosing Party" in paragraph.text and "Receiving Party" in paragraph.text and (
            "shall be referred to as" in paragraph.text
        ):
            _insert_paragraph_after(paragraph, statement)
            manifest.clause_alignments.append(
                "mutuality: inserted reciprocal-obligation statement (Playbook position)"
            )
            return
    raise NdaGenerationError("Template is missing the Disclosing/Receiving role-definition recital.")


def _align_confidential_information(
    document: DocxDocument,
    playbook: Mapping[str, Any],
    clause_adapter: ClauseAdapter | None,
    intake: CounterpartyIntake,
    manifest: GenerationManifest,
) -> None:
    """Add the Playbook's missing exclusion so CI carve-outs match the standard.

    The template's "EXCEPTIONS TO CONFIDENTIAL INFORMATION" lists three carve-outs
    (public / lawful third-party / prior-possession) and omits the Playbook's
    "independently developed without use of Confidential Information" exclusion.
    We append it as a new sub-item to the exception list, in the template's own
    list style, so the definition stays template-shaped but Playbook-complete.
    """

    independent = _independent_development_sentence(playbook, clause_adapter, intake)

    # Find the last sub-item of the exceptions list (the prior-possession item).
    anchor_index = None
    for index, paragraph in enumerate(document.paragraphs):
        if paragraph.text.strip().startswith("was previously in the possession of the Receiving Party"):
            anchor_index = index
            break
    if anchor_index is None:
        raise NdaGenerationError("Template is missing the CI exceptions list (prior-possession item).")

    anchor = document.paragraphs[anchor_index]
    # The prior-possession item ends "...written records." We turn its trailing
    # full stop into "; or" and add the independent-development item after it, in
    # the same list paragraph style.
    anchor_text = anchor.text.rstrip()
    if anchor_text.endswith("."):
        anchor_text = anchor_text[:-1] + "; or"
    _set_paragraph_text(anchor, anchor_text)

    _insert_paragraph_after(anchor, independent)

    manifest.clause_alignments.append(
        "confidential_information: added independent-development exclusion (Playbook standard carve-out)"
    )


def _align_term_and_survival(
    document: DocxDocument,
    playbook: Mapping[str, Any],
    term_years: int,
    clause_adapter: ClauseAdapter | None,
    intake: CounterpartyIntake,
    manifest: GenerationManifest,
) -> None:
    """Rewrite the TERM clause to the Playbook term cap + survival carve-out.

    The template's TERM clause caps at "two (2) years" with no survival carve-out.
    We rewrite the clause body to a fixed term of ``term_years`` (already capped
    at the Playbook ``max_term_years``) and append the Playbook's trade-secret /
    legal / data-protection survival carve-out so it passes ``term_and_survival``.
    """

    survival = _survival_sentence(playbook, clause_adapter, intake)

    body = (
        "This Agreement shall become effective on the date of signing of this Agreement and shall "
        f"remain in force, and the confidentiality obligations shall survive, for a fixed period of "
        f"{_year_count_label(term_years, parenthetical=True)} from the date of this Agreement or until the completion of the "
        f"Purpose, whichever is later. {survival}"
    )

    for paragraph in document.paragraphs:
        if paragraph.text.startswith("TERM OF THE AGREEMENT:"):
            # Preserve the bold title run; replace only the body run(s).
            _set_clause_body(paragraph, "TERM OF THE AGREEMENT: ", body)
            manifest.clause_alignments.append(
                f"term_and_survival: term fixed at {term_years}y (<= Playbook max) + survival carve-out injected"
            )
            return
    raise NdaGenerationError("Template is missing the TERM OF THE AGREEMENT clause.")


# Blank-template fallbacks: used ONLY when the Playbook clause carries no template
# text. Normal operation sources the prose LIVE from the Playbook (so editing the
# Playbook changes the generated wording), exactly as ``_align_mutuality`` reads
# the mutuality ``redline_template`` and ``_resolve_term_years`` reads the term cap.
_INDEPENDENT_DEVELOPMENT_FALLBACK = (
    "is independently developed by the receiving Party without use of or reference "
    "to the Confidential Information."
)
_SURVIVAL_FALLBACK = (
    "Notwithstanding the foregoing, trade secrets, information whose confidentiality is "
    "required by law or regulation, and personal data protected by data-protection law shall "
    "remain confidential for as long as the protected status or applicable law requires."
)


def _independent_development_sentence(
    playbook: Mapping[str, Any],
    clause_adapter: ClauseAdapter | None,
    intake: CounterpartyIntake,
) -> str:
    """The CI independent-development exclusion list item, sourced from the Playbook.

    Reads ``confidential_information.standard_exclusions_template`` and lifts its
    independent-development carve-out into the template's list-item grammar
    ("is ..."), so a Playbook edit to the standard exclusions flows through to the
    generated clause. Falls back to the literal only when the template is blank.
    """

    clause = _playbook_clause(playbook, CLAUSE_CONFIDENTIAL)
    template = str(clause.get("standard_exclusions_template") or "").strip()
    base = _independent_development_item_from_template(template) or _INDEPENDENT_DEVELOPMENT_FALLBACK
    if clause_adapter is None:
        return base
    adapted = clause_adapter.adapt(CLAUSE_CONFIDENTIAL, base, _adapter_context(intake))
    return adapted.strip() or base


def _survival_sentence(
    playbook: Mapping[str, Any],
    clause_adapter: ClauseAdapter | None,
    intake: CounterpartyIntake,
) -> str:
    """The survival carve-out statement, sourced from the Playbook term template.

    Reads ``term_and_survival.redline_template`` and lifts its survival carve-out
    (the "except that ..." tail) into a standalone "Notwithstanding the foregoing,
    ..." statement appended after the fixed-term sentence, so it reads as its own
    statement rather than a sub-clause of the term sentence. A Playbook edit to the
    survival carve-out flows through; falls back to the literal only when blank.
    """

    clause = _playbook_clause(playbook, CLAUSE_TERM)
    template = str(clause.get("redline_template") or "").strip()
    base = _survival_statement_from_template(template) or _SURVIVAL_FALLBACK
    if clause_adapter is None:
        return base
    adapted = clause_adapter.adapt(CLAUSE_TERM, base, _adapter_context(intake))
    return adapted.strip() or base


def _independent_development_item_from_template(template: str) -> str:
    """Lift the independent-development carve-out from the CI exclusions template.

    The ``standard_exclusions_template`` lists carve-outs inline
    ("...or independently developed without use of or reference to Confidential
    Information."). We take the final "or"-joined item and recast it into the
    template's list-item grammar ("is ..."). Returns "" when the template carries
    no independent-development carve-out, so the caller uses the literal fallback.
    """

    if not template:
        return ""
    body = template.rstrip().rstrip(".")
    # The carve-outs are joined by commas with a final "or"; take the last item.
    _, _, last = body.rpartition(", or ")
    fragment = (last or body).strip()
    if "independently develop" not in fragment.lower():
        return ""
    # Recast "independently developed ..." -> "is independently developed ..." so it
    # completes the list stem ("Confidential Information does not include ... that ...").
    if not fragment.lower().startswith("is "):
        fragment = "is " + fragment
    return fragment.rstrip(".") + "."


def _survival_statement_from_template(template: str) -> str:
    """Lift the survival carve-out from the term template into a standalone sentence.

    The ``redline_template`` reads "...survive for a fixed period of up to
    {label}, except that <carve-out>." We take the "except that ..." tail and
    front it with "Notwithstanding the foregoing, " so it stands alone after the
    generator's own fixed-term sentence. Returns "" when there is no carve-out tail.
    """

    if not template:
        return ""
    marker = "except that "
    lowered = template.lower()
    position = lowered.find(marker)
    if position < 0:
        return ""
    carve_out = template[position + len(marker):].strip().rstrip(".").strip()
    if not carve_out:
        return ""
    return "Notwithstanding the foregoing, " + carve_out + "."


def _adapter_context(intake: CounterpartyIntake) -> dict[str, Any]:
    return {
        "counterparty": intake.company_name,
        "purpose": intake.purpose,
        "nda_type": intake.nda_type,
    }


# --------------------------------------------------------------------------- #
# Playbook helpers
# --------------------------------------------------------------------------- #


def _playbook_clause(playbook: Mapping[str, Any], clause_id: str) -> Mapping[str, Any]:
    for clause in playbook.get("clauses", []):
        if clause.get("id") == clause_id:
            return clause
    raise NdaGenerationError(f"Playbook is missing the {clause_id!r} clause.")


def _approved_governing_law_options(playbook: Mapping[str, Any]) -> dict[str, str]:
    """Map approved governing-law option id -> the law value to write."""

    clause = _playbook_clause(playbook, CLAUSE_GOVERNING_LAW)
    options = (clause.get("rules") or {}).get("approved_options") or []
    resolved: dict[str, str] = {}
    for option in options:
        option_id = str(option.get("id") or "").strip()
        value = str(option.get("value") or option.get("label") or "").strip()
        if option_id and value:
            resolved[option_id] = value
    if not resolved:
        raise NdaGenerationError("Playbook governing_law clause has no approved_options.")
    return resolved


def _resolve_term_years(requested: int, playbook: Mapping[str, Any]) -> int:
    """Clamp the requested term to [1, Playbook max_term_years]."""

    clause = _playbook_clause(playbook, CLAUSE_TERM)
    max_years = int(clause.get("max_term_years") or 5)
    try:
        years = int(requested)
    except (TypeError, ValueError):
        years = 2
    if years < 1:
        years = 1
    if years > max_years:
        years = max_years
    return years


# --------------------------------------------------------------------------- #
# docx low-level helpers
# --------------------------------------------------------------------------- #


def _load_template(template_path: Path | str) -> DocxDocument:
    path = Path(template_path)
    if not path.exists():
        raise NdaGenerationError(f"Template not found at {path}.")
    return Document(str(path))


def _document_bytes(document: DocxDocument) -> bytes:
    with BytesIO() as output:
        document.save(output)
        return output.getvalue()


def _apply(text: str, fills: Mapping[str, str]) -> str:
    for token, value in fills.items():
        text = text.replace(token, value)
    return text


def _set_paragraph_text(paragraph: Any, text: str) -> None:
    """Replace a paragraph's text, collapsing it to a single run.

    Placeholders can span runs, so we rewrite the whole paragraph text into the
    first run and clear the rest. Run-level formatting beyond the first run is
    not preserved — acceptable here because the affected paragraphs are plain
    body text or single-format party lines.
    """

    runs = paragraph.runs
    if not runs:
        paragraph.add_run(text)
        return
    runs[0].text = text
    for run in runs[1:]:
        run.text = ""


def _set_clause_body(paragraph: Any, title_prefix: str, body: str) -> None:
    """Replace a titled clause's body while keeping the bold title run intact.

    Body clauses are ``<bold title run><body run>``. We keep run0 (the title)
    and rewrite the remainder to ``title_prefix``-stripped ``body``.
    """

    runs = paragraph.runs
    if runs and runs[0].text.strip().rstrip(":").upper() == title_prefix.strip().rstrip(": ").upper():
        runs[0].text = title_prefix
        if len(runs) > 1:
            runs[1].text = body
            for run in runs[2:]:
                run.text = ""
        else:
            paragraph.add_run(body)
    else:
        _set_paragraph_text(paragraph, title_prefix + body)


def _insert_paragraph_after(paragraph: Any, text: str) -> Any:
    """Insert a new paragraph immediately after ``paragraph`` (same parent).

    The new ``<w:p>`` is a run-stripped copy of ``paragraph`` so it inherits the
    source's paragraph properties (list numbering + style + indentation) — the
    copied ``pPr`` already carries them, so nothing is re-applied.
    """

    from docx.text.paragraph import Paragraph as _P

    new_p = _copy_blank_p(paragraph)
    paragraph._p.addnext(new_p)
    new_paragraph = _P(new_p, paragraph._parent)
    new_paragraph.add_run(text)
    return new_paragraph


def _copy_blank_p(paragraph: Any):
    """A fresh empty ``<w:p>`` element to host an inserted paragraph."""

    from docx.oxml.ns import qn
    import copy

    new_p = copy.deepcopy(paragraph._p)
    # Strip all runs from the copy; keep paragraph properties (numbering/style).
    for run in new_p.findall(qn("w:r")):
        new_p.remove(run)
    return new_p


# The template's own variable slots — every one of these must be filled.
_TEMPLATE_SLOTS = frozenset(
    {
        "[COMPANY NAME]",
        "[ASPORA ENTITY LEGAL NAME]",
        "[JURISDICTION OF INCORPORATION]",
        "[REGISTERED OFFICE ADDRESS]",
        "[BUSINESS DESCRIPTION]",
        "[GOVERNING LAW]",
        "[AUTHORISED SIGNATORY]",
        "[DESIGNATION]",
        "[YEAR]",
        "[•]",
    }
)


def _assert_no_unfilled_placeholders(document: DocxDocument, allowed: set[str]) -> None:
    """Fail closed if any *template* slot survived generation.

    Only the template's own ``_TEMPLATE_SLOTS`` count as "unfilled". An entity
    may legitimately carry a bracketed signatory value (the registry ships
    ``"[Authorised Signatory]"`` / ``"[Title]"`` when no signatory is assigned);
    those were filled *from input* and are passed in ``allowed``, so they don't
    trip the guard.
    """

    import re

    leftover: set[str] = set()
    pattern = re.compile(r"\[[^\]]*\]")
    texts = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                texts.extend(p.text for p in cell.paragraphs)
    for text in texts:
        for token in pattern.findall(text):
            if token in _TEMPLATE_SLOTS and token not in allowed:
                leftover.add(token)
    if leftover:
        raise NdaGenerationError(
            "Generated NDA still contains unfilled placeholders: " + ", ".join(sorted(leftover))
        )


# --------------------------------------------------------------------------- #
# Small text utilities
# --------------------------------------------------------------------------- #

def _split_date(date: _dt.date) -> tuple[str, str, str]:
    day = _ordinal(date.day)
    month = date.strftime("%B")
    return day, month, str(date.year)


def _ordinal(day: int) -> str:
    if 11 <= (day % 100) <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


def _bundle_default_address(bundle: Mapping[str, Any]) -> str:
    chosen = _default_address_entry(bundle)
    if chosen is None:
        raise NdaGenerationError("Entity bundle has no addresses.")
    return _format_address(chosen)


def _default_address_entry(bundle: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The default-flagged address entry (first address if none is flagged)."""

    addresses = bundle.get("addresses") or []
    for address in addresses:
        if address.get("default"):
            return address
    if addresses:
        return addresses[0]
    return None


def _format_address(entry: Mapping[str, Any]) -> str:
    lines = entry.get("lines") or []
    joined = ", ".join(line for line in lines if line)
    return joined or str(entry.get("label") or "").strip()


def select_bundle_address(bundle: Mapping[str, Any], address_id: str = "") -> tuple[str, str]:
    """Pick the entity address to write, honouring the user's chosen ``address_id``.

    Returns ``(formatted_address, chosen_address_id)``. When ``address_id`` names a
    real address on the bundle, THAT address is used (the product lets the user pick
    a non-default office, e.g. Real Transfer's Belfast registered office instead of
    the default London corporate office). An UNKNOWN/invalid id is rejected — mirrors
    the governing-law override guard, so a stale or tampered client value fails loudly
    rather than silently substituting the default. A blank id falls back to the
    default-flagged address. The returned id is recorded on the manifest for the same
    provenance parity the governing-law override has.
    """

    addresses = bundle.get("addresses") or []
    wanted = str(address_id or "").strip()
    if wanted:
        for address in addresses:
            if str(address.get("id") or "").strip() == wanted:
                return _format_address(address), wanted
        known = sorted(str(a.get("id") or "").strip() for a in addresses if a.get("id"))
        raise NdaGenerationError(
            f"Address id {wanted!r} is not an address on entity bundle "
            f"{str(bundle.get('id') or '')!r} (known: {known})."
        )
    chosen = _default_address_entry(bundle)
    if chosen is None:
        raise NdaGenerationError("Entity bundle has no addresses.")
    return _format_address(chosen), str(chosen.get("id") or "").strip()
