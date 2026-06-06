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
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from docx import Document
from docx.document import Document as DocxDocument

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
    signatory_name: str = "[Authorised Signatory]"
    signatory_title: str = "[Designation]"
    entity_id: str = ""


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
    slot_fills: dict[str, str] = field(default_factory=dict)
    clause_alignments: list[str] = field(default_factory=list)

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
            "slot_fills": dict(self.slot_fills),
            "clause_alignments": list(self.clause_alignments),
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


def entity_party_from_bundle(bundle: Mapping[str, Any], playbook: Mapping[str, Any]) -> EntityParty:
    """Build an :class:`EntityParty` from an ``entity_registry`` bundle.

    Resolves the governing-law *value* through the Playbook so the slot text
    always matches an approved option: the bundle carries a
    ``governing_law.playbook_option_id`` which joins onto the Playbook's
    ``governing_law.rules.approved_options[].id``. A bundle whose option is not
    approved is rejected — generation never emits an off-position governing law.
    """

    legal_name = str(bundle.get("legal_name") or "").strip()
    if not legal_name:
        raise NdaGenerationError("Entity bundle is missing legal_name.")

    option_id = str((bundle.get("governing_law") or {}).get("playbook_option_id") or "").strip()
    approved = _approved_governing_law_options(playbook)
    if option_id not in approved:
        raise NdaGenerationError(
            f"Entity governing-law option {option_id!r} is not an approved Playbook option "
            f"(approved: {sorted(approved)})."
        )
    governing_law_value = approved[option_id]

    address = _bundle_default_address(bundle)
    signatory = bundle.get("signatory") or {}

    return EntityParty(
        legal_name=legal_name,
        registered_office=address,
        jurisdiction_of_incorporation=governing_law_value,
        governing_law_value=governing_law_value,
        forum=str(bundle.get("jurisdiction") or "").strip() or governing_law_value,
        signatory_name=str(signatory.get("name") or "[Authorised Signatory]").strip()
        or "[Authorised Signatory]",
        signatory_title=str(signatory.get("title") or "[Designation]").strip() or "[Designation]",
        entity_id=str(bundle.get("id") or "").strip(),
    )


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
    )

    _fill_variable_slots(document, entity, intake, agreement_date, manifest)
    _align_mutuality(document, playbook, clause_adapter, intake, manifest)
    _align_confidential_information(document, playbook, clause_adapter, intake, manifest)
    _align_term_and_survival(document, term_years, clause_adapter, intake, manifest)

    # An entity may legitimately supply a bracketed signatory value when no
    # signatory is assigned yet; those filled-from-input values are allowed.
    allowed_brackets = {
        value
        for value in (entity.signatory_name, entity.signatory_title)
        if value.startswith("[") and value.endswith("]")
    }
    _assert_no_unfilled_placeholders(document, allowed_brackets)

    return GenerationResult(docx_bytes=_document_bytes(document), manifest=manifest)


def save_generated_nda(
    result: GenerationResult,
    matter_id: str,
    *,
    add_artifact: Callable[..., Any],
    repository: Any | None = None,
    owner_user_id: str = "",
) -> Any:
    """Persist a generated NDA as artifact v1 (actor=entity, role=generated).

    ``add_artifact`` is injected (``artifact_service.add_artifact``) so this
    module does not depend on the artifact registry at import time. The actor is
    the chosen entity's legal name; the role is ``generated``; the manifest rides
    along as artifact metadata for provenance.
    """

    return add_artifact(
        matter_id,
        source="generated",
        actor=result.manifest.entity_legal_name or "aspora",
        role="generated",
        document_bytes=result.docx_bytes,
        repository=repository,
        owner_user_id=owner_user_id,
        metadata={"generation": result.manifest.to_dict()},
    )


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
        "[FORUM / JURISDICTION]": entity.forum,
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
            "[FORUM / JURISDICTION]": entity.forum,
            "[AUTHORISED SIGNATORY]": entity.signatory_name,
            "[DESIGNATION]": entity.signatory_title,
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

    lines = [
        "Signed for and on behalf of",
        party_name,
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

    independent = _independent_development_sentence(clause_adapter, intake)

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

    survival = _survival_sentence(clause_adapter, intake)

    body = (
        "This Agreement shall become effective on the date of signing of this Agreement and shall "
        f"remain in force, and the confidentiality obligations shall survive, for a fixed period of "
        f"{_years_label(term_years)} from the date of this Agreement or until the completion of the "
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


def _independent_development_sentence(
    clause_adapter: ClauseAdapter | None,
    intake: CounterpartyIntake,
) -> str:
    base = "is independently developed by the receiving Party without use of or reference to the Confidential Information."
    if clause_adapter is None:
        return base
    adapted = clause_adapter.adapt(CLAUSE_CONFIDENTIAL, base, _adapter_context(intake))
    return adapted.strip() or base


def _survival_sentence(
    clause_adapter: ClauseAdapter | None,
    intake: CounterpartyIntake,
) -> str:
    # The survival carve-out is appended after the fixed-term sentence, so it
    # reads as its own statement rather than a sub-clause of the term sentence.
    base = (
        "Notwithstanding the foregoing, trade secrets, information whose confidentiality is "
        "required by law or regulation, and personal data protected by data-protection law shall "
        "remain confidential for as long as the protected status or applicable law requires."
    )
    if clause_adapter is None:
        return base
    adapted = clause_adapter.adapt(CLAUSE_TERM, base, _adapter_context(intake))
    return adapted.strip() or base


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
        "[FORUM / JURISDICTION]",
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

_NUMBER_WORDS = {
    1: "one (1)",
    2: "two (2)",
    3: "three (3)",
    4: "four (4)",
    5: "five (5)",
}


def _years_label(years: int) -> str:
    words = _NUMBER_WORDS.get(years, f"{years}")
    return f"{words} years" if years != 1 else "one (1) year"


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
    addresses = bundle.get("addresses") or []
    chosen = None
    for address in addresses:
        if address.get("default"):
            chosen = address
            break
    if chosen is None and addresses:
        chosen = addresses[0]
    if chosen is None:
        raise NdaGenerationError("Entity bundle has no addresses.")
    lines = chosen.get("lines") or []
    joined = ", ".join(line for line in lines if line)
    return joined or str(chosen.get("label") or "").strip()
