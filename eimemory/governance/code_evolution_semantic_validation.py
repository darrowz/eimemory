"""Incident-specific semantic gates for model-proposed code replacements."""

from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence
from typing import Any


_RELEASE_CLOSURE_INCIDENT = "release.closure_internal_failure"
_GATE_EVIDENCE_PATH = "eimemory/governance/release_closure_gate_evidence.py"
_RECEIPT_DOMAINS = frozenset({"code.evolution", "deployment.runtime"})


def code_evolution_proposal_semantic_error(
    incident: Mapping[str, Any],
    file_updates: Sequence[Mapping[str, Any]],
) -> str:
    """Reject a structurally valid proposal that violates evidence roles.

    Release-closure repair is deliberately stricter than ordinary Python
    validation.  Storage acceptance records and the deployment receipt occupy
    different evidence namespaces.  The two receipt domains must therefore use
    the authoritative ``receipt_record_id`` argument directly; neither aliases,
    membership checks nor fallback selection from ``live_record_ids`` are
    accepted.
    """

    if str(incident.get("incident_class") or "") != _RELEASE_CLOSURE_INCIDENT:
        return ""
    update = next(
        (item for item in file_updates if str(item.get("path") or "") == _GATE_EVIDENCE_PATH),
        None,
    )
    if update is None:
        return "release_closure_gate_evidence_update_required"
    content = update.get("content")
    if not isinstance(content, str):
        return "release_closure_gate_evidence_content_invalid"
    try:
        module = ast.parse(content, filename=_GATE_EVIDENCE_PATH)
    except SyntaxError:
        return "release_closure_gate_evidence_syntax_invalid"
    functions = [
        node
        for node in module.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "build_release_lineage_gate_evidence"
    ]
    if len(functions) != 1:
        return "release_closure_gate_evidence_builder_invalid"
    function = functions[0]
    argument_names = {
        argument.arg
        for argument in (
            *function.args.posonlyargs,
            *function.args.args,
            *function.args.kwonlyargs,
        )
    }
    if not {"receipt_record_id", "live_record_ids"} <= argument_names:
        return "release_closure_gate_evidence_arguments_invalid"

    # Rebinding the receipt argument would make an apparently direct dictionary
    # value depend on storage records through an earlier assignment.
    for node in ast.walk(function):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
            targets = _assignment_targets(node)
            if any(isinstance(target, ast.Name) and target.id == "receipt_record_id" for target in targets):
                return "release_closure_receipt_rebinding_forbidden"

    evidence_assignments: list[ast.Dict] = []
    for node in ast.walk(function):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and target.id == "evidence" and isinstance(node.value, ast.Dict):
            evidence_assignments.append(node.value)
        for candidate in node.targets:
            if _protected_evidence_subscript(candidate):
                return "release_closure_receipt_post_assignment_forbidden"
    if len(evidence_assignments) != 1:
        return "release_closure_gate_evidence_mapping_invalid"
    mapping = _literal_dict(evidence_assignments[0])
    if mapping is None:
        return "release_closure_gate_evidence_mapping_invalid"
    for domain in _RECEIPT_DOMAINS:
        if not _direct_singleton_name(mapping.get(domain), "receipt_record_id"):
            return "release_closure_receipt_must_be_authoritative_input"
    if not _direct_list_call(mapping.get("storage.integrity"), "live_record_ids"):
        return "release_closure_storage_evidence_must_use_live_records"
    return ""


def _assignment_targets(node: ast.AST) -> tuple[ast.expr, ...]:
    if isinstance(node, ast.Assign):
        return tuple(node.targets)
    if isinstance(node, ast.AnnAssign):
        return (node.target,)
    if isinstance(node, ast.AugAssign):
        return (node.target,)
    if isinstance(node, ast.NamedExpr):
        return (node.target,)
    return ()


def _protected_evidence_subscript(node: ast.AST) -> bool:
    if not isinstance(node, ast.Subscript) or not isinstance(node.value, ast.Name) or node.value.id != "evidence":
        return False
    key = node.slice
    return isinstance(key, ast.Constant) and key.value in _RECEIPT_DOMAINS


def _literal_dict(node: ast.Dict) -> dict[str, ast.expr] | None:
    result: dict[str, ast.expr] = {}
    for key, value in zip(node.keys, node.values, strict=True):
        if not isinstance(key, ast.Constant) or not isinstance(key.value, str) or key.value in result:
            return None
        result[key.value] = value
    return result


def _direct_singleton_name(node: ast.expr | None, expected: str) -> bool:
    return bool(
        isinstance(node, ast.List)
        and len(node.elts) == 1
        and isinstance(node.elts[0], ast.Name)
        and node.elts[0].id == expected
    )


def _direct_list_call(node: ast.expr | None, expected: str) -> bool:
    return bool(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "list"
        and len(node.args) == 1
        and not node.keywords
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == expected
    )


__all__ = ["code_evolution_proposal_semantic_error"]
