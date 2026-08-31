from __future__ import annotations

from eimemory.governance.code_evolution_semantic_validation import (
    code_evolution_proposal_semantic_error,
)


INCIDENT = {"incident_class": "release.closure_internal_failure"}
PATH = "eimemory/governance/release_closure_gate_evidence.py"


def _proposal(builder: str) -> list[dict[str, str]]:
    return [{"path": PATH, "content": builder}]


def test_exact_receipt_and_storage_roles_are_accepted() -> None:
    content = '''
def build_release_lineage_gate_evidence(
    *, receipt_record_id: str, live_record_ids: list[str], **kwargs
):
    evidence = {
        "storage.integrity": list(live_record_ids),
        "deployment.runtime": [receipt_record_id],
        "code.evolution": [receipt_record_id],
    }
    return evidence
'''
    assert code_evolution_proposal_semantic_error(INCIDENT, _proposal(content)) == ""


def test_live_record_fallback_is_rejected_before_materialization() -> None:
    content = '''
def build_release_lineage_gate_evidence(
    *, receipt_record_id: str, live_record_ids: list[str], **kwargs
):
    normalized_live = list(live_record_ids)
    current_receipt = receipt_record_id if receipt_record_id in normalized_live else normalized_live[-1]
    evidence = {
        "storage.integrity": list(live_record_ids),
        "deployment.runtime": [current_receipt],
        "code.evolution": [current_receipt],
    }
    return evidence
'''
    assert code_evolution_proposal_semantic_error(INCIDENT, _proposal(content)) == (
        "release_closure_receipt_must_be_authoritative_input"
    )


def test_receipt_argument_cannot_be_rebound_from_live_records() -> None:
    content = '''
def build_release_lineage_gate_evidence(
    *, receipt_record_id: str, live_record_ids: list[str], **kwargs
):
    receipt_record_id = live_record_ids[-1]
    evidence = {
        "storage.integrity": list(live_record_ids),
        "deployment.runtime": [receipt_record_id],
        "code.evolution": [receipt_record_id],
    }
    return evidence
'''
    assert code_evolution_proposal_semantic_error(INCIDENT, _proposal(content)) == (
        "release_closure_receipt_rebinding_forbidden"
    )


def test_receipt_domains_cannot_be_mutated_after_exact_mapping() -> None:
    content = '''
def build_release_lineage_gate_evidence(
    *, receipt_record_id: str, live_record_ids: list[str], **kwargs
):
    evidence = {
        "storage.integrity": list(live_record_ids),
        "deployment.runtime": [receipt_record_id],
        "code.evolution": [receipt_record_id],
    }
    evidence["code.evolution"] = [live_record_ids[-1]]
    return evidence
'''
    assert code_evolution_proposal_semantic_error(INCIDENT, _proposal(content)) == (
        "release_closure_receipt_post_assignment_forbidden"
    )


def test_other_incident_classes_are_not_constrained_by_release_contract() -> None:
    assert code_evolution_proposal_semantic_error(
        {"incident_class": "deployment.runtime_commit_drift"},
        [{"path": "other.py", "content": "not python"}],
    ) == ""
