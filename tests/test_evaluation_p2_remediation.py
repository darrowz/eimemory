"""Evaluation audit P2 remediation checks (Protocol, authority API, SEC-3, text)."""
from __future__ import annotations

import ast
from pathlib import Path
from threading import RLock

import pytest

from eimemory.evaluation.contracts import (
    RECALL_EVALUATOR_ENTRYPOINTS,
    RecallEvaluator,
    load_recall_evaluator,
)
from eimemory.evaluation.exceptions import (
    EvaluationAuthorityError,
    EvaluationCatalogError,
    EvaluationDatasetError,
    EvaluationError,
)
from eimemory.evaluation.capability_catalog import (
    CapabilityEvaluationCatalog,
    CatalogResolutionError,
)
from eimemory.evaluation._text import extract_text_from_messages, extract_text_from_turn
from eimemory.evaluation.label_authority import (
    sign_operator_label,
    verify_case_authority,
    verify_delegated_label,
    verify_label_authority,
    verify_operator_label,
)


ROOT = Path(__file__).resolve().parents[1]
EVAL_DIR = ROOT / "eimemory" / "evaluation"


def test_recall_evaluator_protocol_exists_and_variants_compatible() -> None:
    contracts_src = (EVAL_DIR / "contracts.py").read_text(encoding="utf-8")
    tree = ast.parse(contracts_src)
    protocol_names = {
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and any(
            (isinstance(base, ast.Name) and base.id == "Protocol")
            or (isinstance(base, ast.Attribute) and base.attr == "Protocol")
            for base in node.bases
        )
    }
    assert "RecallEvaluator" in protocol_names
    assert set(RECALL_EVALUATOR_ENTRYPOINTS) == {
        "production_recall",
        "explicit_recall",
        "semantic_recall",
        "original_query_recall",
    }
    for name in RECALL_EVALUATOR_ENTRYPOINTS:
        evaluator = load_recall_evaluator(name)
        assert callable(evaluator)
        # runtime_checkable Protocol: plain functions satisfy via structural __call__
        assert isinstance(evaluator, RecallEvaluator)


def test_evaluation_exception_hierarchy() -> None:
    assert issubclass(EvaluationDatasetError, EvaluationError)
    assert issubclass(EvaluationAuthorityError, EvaluationError)
    assert issubclass(EvaluationCatalogError, EvaluationError)
    assert issubclass(CatalogResolutionError, EvaluationCatalogError)
    assert issubclass(CatalogResolutionError, EvaluationError)


def test_label_authority_public_api_surface() -> None:
    assert callable(verify_label_authority)
    assert callable(verify_case_authority)
    assert callable(verify_delegated_label)
    assert callable(verify_operator_label)
    assert callable(sign_operator_label)


def test_text_messages_helper_is_single_source() -> None:
    turn = {
        "messages": [
            {"role": "A", "content": "hello"},
            {"role": "B", "content": "world"},
        ]
    }
    assert extract_text_from_turn(turn) == "A: hello\nB: world"
    assert extract_text_from_messages([turn["messages"][0], turn["messages"][1]]) == "A: hello\nB: world"
    # longmemeval must not reimplement extraction
    lm = (EVAL_DIR / "longmemeval.py").read_text(encoding="utf-8")
    assert "extract_text_from_messages" in lm
    assert 'texts.append(f"{role}: {text}"' not in lm


def test_catalog_seal_rlock_blocks_register(tmp_path=None) -> None:
    catalog = CapabilityEvaluationCatalog()
    assert isinstance(catalog._mutation_lock, type(RLock()))
    catalog.seal()
    with pytest.raises(CatalogResolutionError, match="capability_catalog_sealed"):
        catalog.register_case  # noqa: B018 — attribute exists
        from eimemory.evaluation.capability_catalog import CatalogCase
        # register_executor requires callable; use register_case with bad type
        catalog.register_case(object())  # type: ignore[arg-type]


def test_operator_hmac_fail_closed_without_key(monkeypatch) -> None:
    monkeypatch.delenv("EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY", raising=False)
    monkeypatch.delenv("EIMEMORY_EVIDENCE_RECEIPT_KEYRING_FILE", raising=False)
    monkeypatch.delenv("EIMEMORY_EVIDENCE_RECEIPT_ENV_FILE", raising=False)
    with pytest.raises(ValueError, match="operator_label_attestation_key_unavailable"):
        sign_operator_label({"scope": {}, "source_id": "x", "label": {}})


def test_operator_hmac_roundtrip(monkeypatch) -> None:
    monkeypatch.setenv(
        "EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY",
        "fixture-only-0123456789-abcdefghijklmnopqrstuvwxyz",
    )
    body = sign_operator_label(
        {
            "scope": {"tenant_id": "default", "agent_id": "hongtu", "workspace_id": "embodied", "user_id": "u"},
            "source_id": "codex",
            "label": {
                "pending_record_id": "p1",
                "record_ref": "r1",
                "grade": 3,
                "labeler": "operator",
            },
            "operator_packet_evidence": {"schema": "secure_dataset_fingerprint.v1", "digest": "a" * 64},
        }
    )
    content = {
        "pending_record_id": "p1",
        "record_ref": "r1",
        "grade": 3,
        "labeler": "operator",
        "operator_packet_evidence": {"schema": "secure_dataset_fingerprint.v1", "digest": "a" * 64},
        "operator_authority": body,
    }
    from eimemory.models.records import ScopeRef

    assert (
        verify_operator_label(
            content,
            scope=ScopeRef(tenant_id="default", agent_id="hongtu", workspace_id="embodied", user_id="u"),
            source_id="codex",
        )
        == ""
    )
