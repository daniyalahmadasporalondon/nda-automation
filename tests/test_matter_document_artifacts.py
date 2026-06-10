from __future__ import annotations

from unittest.mock import patch

from nda_automation import matter_document_artifacts
from nda_automation.matter_repository import InMemoryMatterRepository
from nda_automation.redline_export_service import RedlineExport


def test_build_reviewed_docx_materializes_and_registers_reviewed_artifact():
    repo = InMemoryMatterRepository()
    matter = repo.create_matter(
        source_filename="Mutual NDA.docx",
        document_bytes=b"source",
        extracted_text="Text",
        review_result={
            "clauses": [],
            "redline_edits": [],
            "playbook_version": {"hash": "sha256:playbook"},
        },
        triage={"triage_status": "ready_to_sign"},
    )
    matter = {
        **matter,
        "reviewer_decisions": {
            "governing_law": {"action": "accept", "actor": "legal@example.com"}
        },
    }

    redline_export = RedlineExport(data=b"reviewed-docx", filename="reviewed.docx")
    with patch.object(
        matter_document_artifacts.redline_export_service,
        "build_matter_redline",
        return_value=redline_export,
    ) as build_redline:
        with patch.object(
            matter_document_artifacts.artifact_service,
            "register_reviewed_docx",
            return_value=None,
        ) as register_reviewed:
            reviewed = matter_document_artifacts.build_reviewed_docx(
                matter["id"],
                matter,
                repository=repo,
                owner_user_id="owner-1",
            )

    assert reviewed.export is redline_export
    assert reviewed.artifact is None
    assert reviewed.payload == {
        "export_redline_edits": [],
        "manual_redline_edits": [],
        "review_comments": [],
    }
    build_redline.assert_called_once_with(
        matter["id"],
        reviewed.payload,
        persist=False,
        repository=repo,
        owner_user_id="owner-1",
    )
    register_reviewed.assert_called_once_with(
        matter["id"],
        b"reviewed-docx",
        review_version_hash="sha256:playbook",
        repository=repo,
        owner_user_id="owner-1",
    )

