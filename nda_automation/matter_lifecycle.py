"""Small matter lifecycle operations used by route handlers."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from . import approval
from .matter_repository import DiskMatterRepository, MatterRepository


@dataclass(frozen=True)
class MatterApprovalResult:
    matter: dict[str, Any] | None
    blocks: list[str]
    approved_at: str = ""
    approver: str = ""
    timeline_event: dict[str, Any] | None = None


def repository_for_handler(handler: object) -> MatterRepository:
    repository = getattr(handler, "matter_repository", None)
    if isinstance(repository, MatterRepository):
        return repository
    return DiskMatterRepository()


def get_matter(
    repository: MatterRepository,
    matter_id: str,
    *,
    owner_user_id: str = "",
) -> dict[str, Any] | None:
    return repository.get_matter(matter_id, owner_user_id=owner_user_id)


def record_clause_decision(
    repository: MatterRepository,
    matter_id: str,
    clause_id: str,
    reviewer_decision: dict[str, Any],
    *,
    owner_user_id: str = "",
) -> dict[str, Any] | None:
    return repository.set_clause_reviewer_decision(
        matter_id,
        clause_id,
        reviewer_decision,
        owner_user_id=owner_user_id,
    )


def approve_matter(
    repository: MatterRepository,
    matter_id: str,
    *,
    actor: str,
    owner_user_id: str = "",
) -> MatterApprovalResult:
    matter = repository.get_matter(matter_id, owner_user_id=owner_user_id)
    if matter is None:
        return MatterApprovalResult(matter=None, blocks=[])

    blocks = approval.approval_blocks(matter)
    if blocks:
        return MatterApprovalResult(matter=matter, blocks=blocks)

    approved_at = datetime.now(timezone.utc).isoformat()
    timeline_event = approval.approval_timeline_event(actor=actor)
    updated_matter = repository.record_matter_approval(
        matter_id,
        approver=actor,
        approved_at=approved_at,
        timeline_event=timeline_event,
        owner_user_id=owner_user_id,
    )
    return MatterApprovalResult(
        matter=updated_matter,
        blocks=[],
        approved_at=approved_at,
        approver=actor,
        timeline_event=timeline_event,
    )
