from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from . import generation_timing, nda_generation, pdf_export_service, playbook_runtime
from .nda_generation import CounterpartyIntake


class GenerationPayloadError(ValueError):
    """A client payload problem (missing or invalid generation field)."""


@dataclass(frozen=True)
class GeneratedNdaWorkflowResult:
    result: nda_generation.GenerationResult
    matter: dict[str, Any]
    artifact: Any
    active_playbook: playbook_runtime.ActivePlaybookBundle
    self_check: nda_generation.SelfCheckResult

    def response_payload(self) -> dict[str, Any]:
        matter_id = str(self.matter.get("id") or "")
        return {
            "matter_id": matter_id,
            "artifact_id": str(getattr(self.artifact, "id", "") or ""),
            "status": "generated",
            "download_url": f"/api/matters/{quote(matter_id, safe='')}/source" if matter_id else "",
            "pdf_download_url": pdf_export_service.matter_pdf_download_url(matter_id),
            "document_downloads": pdf_export_service.public_matter_document_downloads(self.matter),
            "self_check": {
                "passed": self.self_check.passed,
                "overall_status": self.self_check.overall_status,
                "native_failures": self.self_check.native_failures,
                "dynamic_failures": self.self_check.dynamic_failures,
            },
            "manifest": self.result.manifest.to_dict(),
        }


def generate_nda_from_payload(payload: dict[str, Any], *, owner_user_id: str) -> GeneratedNdaWorkflowResult:
    entity_id, intake, governing_law_override, address_id = intake_from_payload(payload)
    return generate_nda_for_matter(
        entity_id,
        intake,
        owner_user_id=owner_user_id,
        governing_law_override=governing_law_override,
        address_id=address_id,
    )


def generate_nda_for_matter(
    entity_id: str,
    intake: CounterpartyIntake,
    *,
    owner_user_id: str,
    governing_law_override: str = "",
    address_id: str = "",
) -> GeneratedNdaWorkflowResult:
    from .artifact_registry import ROLE_ORIGINAL, latest_artifact_for_role  # noqa: PLC0415
    from .ingestion_service import create_matter_from_document  # noqa: PLC0415
    from .matter_repository import DiskMatterRepository  # noqa: PLC0415

    repository = DiskMatterRepository()
    active_playbook = playbook_runtime.ensure_active_playbook_bundle()
    generation_timing.mark_phase("playbook loaded")
    result = nda_generation.generate_nda_for_entity(
        entity_id,
        intake,
        playbook_bundle=active_playbook,
        governing_law_override=governing_law_override,
        address_id=address_id,
    )
    generation_timing.mark_phase("docx built")

    # Hard safety gate on the actual matter-creation path. Nothing is persisted
    # before this passes.
    nda_generation.assert_generated_nda_is_on_position(result, playbook=active_playbook.playbook)

    filename = generated_filename(intake)
    matter = create_matter_from_document(
        filename=filename,
        document_bytes=result.docx_bytes,
        source_type="generated",
        board_column="generated",
        intake_metadata={"generation": result.manifest.to_dict()},
        owner_user_id=owner_user_id,
        repository=repository,
        defer_ai_review=True,
        playbook_runtime_func=lambda: active_playbook,
    )
    generation_timing.mark_phase("matter persisted")

    original = latest_artifact_for_role(matter, ROLE_ORIGINAL)
    artifact = nda_generation.save_generated_nda(
        result,
        str(matter.get("id") or ""),
        repository=repository,
        based_on_artifact_id=(original.id if original else ""),
        owner_user_id=owner_user_id,
    )
    self_check = nda_generation.self_check_generated_nda(result.docx_bytes, playbook=active_playbook.playbook)
    return GeneratedNdaWorkflowResult(
        result=result,
        matter=matter,
        artifact=artifact,
        active_playbook=active_playbook,
        self_check=self_check,
    )


def generated_filename(intake: CounterpartyIntake) -> str:
    counterparty = (intake.company_name or "Counterparty").strip()
    safe = "".join(ch if ch.isalnum() or ch in " -_" else "" for ch in counterparty).strip() or "Counterparty"
    return f"NDA - {safe}.docx"


def intake_from_payload(payload: dict[str, Any]) -> tuple[str, CounterpartyIntake, str, str]:
    if not isinstance(payload, dict):
        raise GenerationPayloadError("Request body must be a JSON object.")

    governing_law_override = governing_law_override_from_payload(payload)
    address_id = address_id_from_payload(payload)
    entity_id = entity_id_from_payload(payload)
    if not entity_id:
        raise GenerationPayloadError("A signing entity must be selected.")

    intake_block = payload.get("intake") if isinstance(payload.get("intake"), dict) else {}
    counterparty = payload.get("counterparty") if isinstance(payload.get("counterparty"), dict) else {}

    def field(*keys: str) -> str:
        for source in (intake_block, counterparty, payload):
            for key in keys:
                value = source.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return ""

    company_name = field("counterparty_name", "name")
    if not company_name:
        raise GenerationPayloadError("The counterparty name is required.")

    intake = CounterpartyIntake(
        company_name=company_name,
        registered_office=field("counterparty_registered_office", "registered_office"),
        jurisdiction_of_incorporation=field(
            "counterparty_jurisdiction", "jurisdiction_of_incorporation"
        ),
        # business_description is the labelled, OUTBOUND business field (it fills the
        # recital [BUSINESS DESCRIPTION] slot the counterparty reads). Generation
        # reads ONLY this field — never the (now-removed) Special Notes `notes` field.
        # The old `field("business_description", "notes")` fallback leaked the private
        # counsel notes into the outbound recital; the `notes` read is gone entirely,
        # so generation no longer touches it on any path. When business_description is
        # absent the slot generates empty.
        business_description=field("business_description"),
        purpose=field("project", "project_purpose", "purpose")
        or "the proposed business relationship between the parties",
        term_years=term_years(_first(intake_block, payload, "term_years", "term")),
        nda_type=field("nda_type") or nda_generation.NDA_TYPE_MUTUAL,
        agreement_date=agreement_date(_first(intake_block, payload, "effective_date", "agreement_date")),
    )
    return entity_id, intake, governing_law_override, address_id


def entity_id_from_payload(payload: dict[str, Any]) -> str:
    flat = payload.get("signing_entity_id")
    if isinstance(flat, str) and flat.strip():
        return flat.strip()
    signing_entity = payload.get("signing_entity")
    if isinstance(signing_entity, dict):
        nested = signing_entity.get("id")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
    return ""


def address_id_from_payload(payload: dict[str, Any]) -> str:
    """The Aspora address id the user picked, from ``signing_entity.address.id``.

    The frontend emits the chosen address as a coupled block
    (``signing_entity.address = {id, label, lines}``); generation only needs the
    ``id`` to select that address from the registry bundle (the registry is the
    address source of truth, so we never trust the lines the client echoes back).
    Absent/blank means "use the entity's default address" — the selection is
    optional, exactly like the governing-law override.
    """

    signing_entity = payload.get("signing_entity")
    if isinstance(signing_entity, dict):
        address = signing_entity.get("address")
        if isinstance(address, dict):
            address_id = address.get("id")
            if isinstance(address_id, str) and address_id.strip():
                return address_id.strip()
    return ""


def governing_law_override_from_payload(payload: dict[str, Any]) -> str:
    signing_entity = payload.get("signing_entity")
    if isinstance(signing_entity, dict):
        option_id = option_id_from_law_block(signing_entity.get("governing_law"))
        if option_id:
            return option_id
    flat = payload.get("governing_law_override")
    if isinstance(flat, str) and flat.strip():
        return flat.strip()
    return option_id_from_law_block(payload.get("governing_law"))


def option_id_from_law_block(block: object) -> str:
    if isinstance(block, dict):
        option_id = block.get("playbook_option_id") or block.get("id")
        if isinstance(option_id, str) and option_id.strip():
            return option_id.strip()
    return ""


def term_years(value: object) -> int:
    if value is None or value == "":
        return 2
    try:
        return int(str(value).strip().split()[0])
    except (ValueError, IndexError):
        return 2


def agreement_date(value: object) -> datetime.date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        return None


def _first(*sources_then_keys: Any) -> object:
    sources = [source for source in sources_then_keys if isinstance(source, dict)]
    keys = [key for key in sources_then_keys if isinstance(key, str)]
    for source in sources:
        for key in keys:
            if key in source and source.get(key) not in (None, ""):
                return source.get(key)
    return None
