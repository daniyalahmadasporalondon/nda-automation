from __future__ import annotations

import pytest

from nda_automation.checker import REVIEW_ENGINE_VERSION
from nda_automation.redline_export_service import StaleMatterReviewError, _review_result_for_export
from nda_automation.review_staleness import review_result_staleness


def _runtime(active_hash: str = "sha256:current") -> dict:
    return {
        "version": 1,
        "active_version_id": "pbv_current",
        "active_hash": active_hash,
        "playbook_name": "NDA Playbook",
        "playbook_version": "2026.06",
        "published_at": "2026-06-05T00:00:00+00:00",
        "published_by": "test",
        "source": "publish",
    }


def _review_result(active_hash: str = "sha256:current") -> dict:
    return {
        "review_engine_version": REVIEW_ENGINE_VERSION,
        "review_state": {},
        "clauses": [
            {
                "id": "governing_law",
                "structure_context": {},
                "review_state": {},
            }
        ],
        "playbook_runtime": {
            "active_version_id": "pbv_review",
            "active_hash": active_hash,
            "playbook_name": "NDA Playbook",
            "playbook_version": "2026.06",
            "published_at": "2026-06-04T00:00:00+00:00",
            "published_by": "test",
            "source": "active",
            "active_source": "publish",
        },
    }


def test_review_staleness_flags_changed_playbook_hash():
    summary = review_result_staleness(
        _review_result(active_hash="sha256:review"),
        current_runtime_func=lambda: _runtime(active_hash="sha256:current"),
    )

    assert summary["stale"] is True
    assert summary["stale_reasons"] == ["playbook_changed"]
    assert summary["current_playbook"]["active_hash"] == "sha256:current"
    assert summary["review_playbook"]["active_hash"] == "sha256:review"
    assert "Playbook changed" in summary["message"]


def test_review_staleness_accepts_matching_engine_and_playbook_runtime():
    summary = review_result_staleness(
        _review_result(active_hash="sha256:current"),
        current_runtime_func=lambda: _runtime(active_hash="sha256:current"),
    )

    assert summary["stale"] is False
    assert summary["stale_reasons"] == []


def test_review_staleness_flags_legacy_review_without_playbook_runtime():
    review_result = _review_result()
    review_result.pop("playbook_runtime")

    summary = review_result_staleness(
        review_result,
        current_runtime_func=lambda: _runtime(active_hash="sha256:current"),
    )

    assert summary["stale"] is True
    assert "missing_playbook_runtime" in summary["stale_reasons"]


def test_matter_export_blocks_stale_review_before_reading_source_document():
    class Repository:
        source_requested = False

        def get_matter(self, matter_id: str, owner_user_id: str = "") -> dict:
            return {
                "id": matter_id,
                "source_filename": "stale.docx",
                "review_result": _review_result(active_hash="sha256:review"),
                "extracted_text": "Stored text",
            }

        def get_source_document_bytes(self, matter: dict) -> bytes | None:
            self.source_requested = True
            return b"not reached"

    repository = Repository()

    with pytest.raises(StaleMatterReviewError) as error:
        _review_result_for_export({"matter_id": "matter-1"}, "", repository=repository)

    assert repository.source_requested is False
    assert error.value.reasons == ["playbook_changed"]
