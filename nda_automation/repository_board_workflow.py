"""Repository board workflow commands.

This module owns the public board payload grammar for matter cards. HTTP routes
parse request details and delegate here; repository adapters keep owning storage.
"""
from __future__ import annotations

from typing import Any

from . import document_rendering, matter_view
from .matter_repository import MatterRepository, MatterRepositoryError

BOARD_COLUMNS = {"gmail_demo", "in_review", "reviewed", "sent"}


class RepositoryBoardWorkflowError(RuntimeError):
    """A board workflow error already translated into the public HTTP payload."""

    def __init__(self, message: str, *, status: int) -> None:
        super().__init__(message)
        self.payload = {"error": message}
        self.status = status


class RepositoryBoardWorkflow:
    """Commands and public payloads for the Repository board."""

    def __init__(self, repository: MatterRepository) -> None:
        self._repository = repository

    def list_board(self, *, owner_user_id: str = "") -> dict[str, Any]:
        try:
            matters = self._repository.list_matters(owner_user_id)
        except MatterRepositoryError as error:
            raise _repository_error(error) from error
        return {"matters": matter_view.public_matters(matters)}

    def detail_card(self, matter_id: str | None, *, owner_user_id: str = "") -> dict[str, Any]:
        matter = self._matter_or_not_found(matter_id, owner_user_id=owner_user_id)
        return {"matter": matter_view.public_matter(matter)}

    def move_card(
        self,
        matter_id: str | None,
        board_column: object,
        *,
        owner_user_id: str = "",
    ) -> dict[str, Any]:
        if matter_id is None:
            raise _not_found()
        if not isinstance(board_column, str) or board_column not in BOARD_COLUMNS:
            raise RepositoryBoardWorkflowError("Unsupported matter stage.", status=400)
        try:
            matter = self._repository.update_matter_stage(
                matter_id,
                board_column,
                owner_user_id=owner_user_id,
            )
        except MatterRepositoryError as error:
            raise _repository_error(error) from error
        if matter is None:
            raise _not_found()
        return {"matter": matter_view.public_matter(matter)}

    def set_reviewed(
        self,
        matter_id: str | None,
        reviewed: object,
        *,
        owner_user_id: str = "",
    ) -> dict[str, Any]:
        if matter_id is None:
            raise _not_found()
        if not isinstance(reviewed, bool):
            raise RepositoryBoardWorkflowError("reviewed must be true or false.", status=400)
        try:
            matter = self._repository.update_matter_fields(
                matter_id,
                {"human_reviewed": reviewed},
                owner_user_id=owner_user_id,
            )
        except MatterRepositoryError as error:
            raise _repository_error(error) from error
        if matter is None:
            raise _not_found()
        return {"matter": matter_view.public_matter(matter)}

    def delete_card(self, matter_id: str | None, *, owner_user_id: str = "") -> dict[str, Any]:
        if matter_id is None:
            raise _not_found()
        try:
            pre_delete = self._repository.get_matter(matter_id, owner_user_id=owner_user_id)
            source_bytes = self._repository.get_source_document_bytes(pre_delete) if pre_delete else None
            source_filename = str(pre_delete.get("source_filename") or "") if pre_delete else ""
            matter = self._repository.delete_matter(matter_id, owner_user_id=owner_user_id)
        except MatterRepositoryError as error:
            raise _repository_error(error) from error
        if matter is None:
            raise _not_found()

        document_rendering.matter_render_coordinator().forget(matter_id)
        if source_bytes is not None:
            document_rendering.purge_render_cache_for_source(
                source_bytes,
                owner_user_id=str(matter.get("owner_user_id") or owner_user_id),
                source_filename=source_filename,
            )
        return {"deleted": matter_view.public_matter(matter)}

    def reset_board(self, *, owner_user_id: str = "") -> dict[str, Any]:
        try:
            removed_count = self._repository.reset_demo_repository(owner_user_id=owner_user_id)
        except MatterRepositoryError as error:
            raise _repository_error(error) from error
        return {"removed": removed_count, "matters": []}

    def _matter_or_not_found(self, matter_id: str | None, *, owner_user_id: str) -> dict[str, Any]:
        if matter_id is None:
            raise _not_found()
        try:
            matter = self._repository.get_matter(matter_id, owner_user_id=owner_user_id)
        except MatterRepositoryError as error:
            raise _repository_error(error) from error
        if matter is None:
            raise _not_found()
        return matter


def _not_found() -> RepositoryBoardWorkflowError:
    return RepositoryBoardWorkflowError("Matter not found.", status=404)


def _repository_error(error: MatterRepositoryError) -> RepositoryBoardWorkflowError:
    return RepositoryBoardWorkflowError(str(error), status=500)
