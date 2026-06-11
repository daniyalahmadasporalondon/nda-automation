from nda_automation import nda_generation, nda_generation_workflow


def test_intake_from_payload_accepts_committed_frontend_shape():
    entity_id, intake, governing_law_override = nda_generation_workflow.intake_from_payload(
        {
            "counterparty": {"name": "Globex International Ltd"},
            "project_purpose": "evaluating a data-sharing integration",
            "term": "3 years",
            "nda_type": "mutual",
            "notes": "introduced via the partnerships team",
            "signing_entity": {
                "id": "aspora_technology",
                "governing_law": {"playbook_option_id": "england_and_wales"},
            },
        }
    )

    assert entity_id == "aspora_technology"
    assert intake.company_name == "Globex International Ltd"
    assert intake.purpose == "evaluating a data-sharing integration"
    assert intake.term_years == 3
    assert intake.business_description == "introduced via the partnerships team"
    assert governing_law_override == "england_and_wales"


def test_intake_from_payload_rejects_missing_required_fields():
    try:
        nda_generation_workflow.intake_from_payload({"intake": {"counterparty_name": "Acme"}})
    except nda_generation_workflow.GenerationPayloadError as error:
        assert "signing entity" in str(error)
    else:
        raise AssertionError("Expected missing signing entity to be rejected.")

    try:
        nda_generation_workflow.intake_from_payload({"signing_entity_id": "aspora_technology"})
    except nda_generation_workflow.GenerationPayloadError as error:
        assert "counterparty name" in str(error)
    else:
        raise AssertionError("Expected missing counterparty to be rejected.")


def test_workflow_response_payload_matches_generation_route_contract(monkeypatch):
    download_contract = {
        "source": {"formats": {"docx": {"available": True}, "pdf": {"available": False}}},
        "reviewed": {"formats": {"docx": {"available": False}, "pdf": {"available": False}}},
    }
    monkeypatch.setattr(
        nda_generation_workflow.pdf_export_service,
        "public_matter_document_downloads",
        lambda matter: download_contract,
    )
    manifest = nda_generation.GenerationManifest(
        entity_id="aspora_technology",
        entity_legal_name="Aspora Technology Services Private Limited",
        counterparty_name="Acme Ltd",
        nda_type="mutual",
        term_years=2,
        agreement_date="2026-06-10",
        governing_law_value="India",
        forum="Courts of India",
    )
    result = nda_generation.GenerationResult(docx_bytes=b"PK\x03\x04", manifest=manifest)
    self_check = nda_generation.SelfCheckResult(
        passed=True,
        overall_status="meets_requirements",
        native_failures=[],
        dynamic_failures=[],
    )
    artifact = type("Artifact", (), {"id": "artifact-1"})()
    active_playbook = type("ActivePlaybook", (), {"playbook": {"clauses": []}})()
    workflow_result = nda_generation_workflow.GeneratedNdaWorkflowResult(
        result=result,
        matter={"id": "matter 1", "source_filename": "NDA - Acme Ltd.docx", "source_type": "generated"},
        artifact=artifact,
        active_playbook=active_playbook,
        self_check=self_check,
    )

    assert workflow_result.response_payload() == {
        "matter_id": "matter 1",
        "artifact_id": "artifact-1",
        "status": "generated",
        "download_url": "/api/matters/matter%201/source",
        "pdf_download_url": "/api/matters/matter%201/source-pdf",
        "document_downloads": download_contract,
        "self_check": {
            "passed": True,
            "overall_status": "meets_requirements",
            "native_failures": [],
            "dynamic_failures": [],
        },
        "manifest": manifest.to_dict(),
    }
