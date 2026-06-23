"""Matter document artifact workflows.

This module owns document-version transitions that combine review decisions,
DOCX materialization, and artifact registration.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import approval, artifact_service, redline_export_service
from .artifact_registry import Artifact
from .matter_repository import DiskMatterRepository, MatterRepository


@dataclass(frozen=True)
class ReviewedDocx:
    export: redline_export_service.RedlineExport
    artifact: Artifact | None
    payload: dict[str, Any]


def build_reviewed_docx(
    matter_id: str,
    matter: dict[str, Any],
    *,
    repository: MatterRepository | None = None,
    owner_user_id: str = "",
    persist: bool = True,
) -> ReviewedDocx:
    """Materialize the reviewed DOCX from reviewer decisions.

    ``persist`` (default True) controls whether the materialized bytes are
    registered as a durable ``role="reviewed"`` artifact. Approval mints that
    artifact (``persist=True`` — the eager registration at the approval
    transition). A pre-approval *preview* serves the identical bytes for the
    faithful renderer without persisting anything (``persist=False`` →
    ``artifact=None``); approval stays the lone event that mints the durable
    reviewed version. The redline materialization itself never persists
    (``build_matter_redline(persist=False)``) in either case.
    """
    repository = repository or DiskMatterRepository()
    payload = approval.reviewed_docx_payload(matter)
    redline_export = redline_export_service.build_matter_redline(
        matter_id,
        payload,
        persist=False,
        repository=repository,
        owner_user_id=owner_user_id,
    )
    artifact: Artifact | None = None
    if persist:
        review_version_hash = ""
        review_result = matter.get("review_result")
        if isinstance(review_result, dict):
            playbook_version = review_result.get("playbook_version")
            if isinstance(playbook_version, dict):
                review_version_hash = str(playbook_version.get("hash") or "")
        artifact = artifact_service.register_reviewed_docx(
            matter_id,
            redline_export.data,
            review_version_hash=review_version_hash,
            repository=repository,
            owner_user_id=owner_user_id,
        )
    return ReviewedDocx(export=redline_export, artifact=artifact, payload=payload)

