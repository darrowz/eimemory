"""Semantic and AST security gates for model-proposed code replacements."""

from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence
from typing import Any


_RELEASE_CLOSURE_INCIDENT = "release.closure_internal_failure"
_GATE_EVIDENCE_PATH = "eimemory/governance/release_closure_gate_evidence.py"
_RECEIPT_DOMAINS = frozenset({"code.evolution", "deployment.runtime"})

# Modules whose import or use is banned in any candidate file_update content.
_BANNED_IMPORT_ROOTS = frozenset({
    "importlib",
    "ctypes",
    "socket",
    "urllib",
    "http",
    "requests",
})
_BANNED_OS_CALLS = frozenset({
    "execl",
    "execle",
    "execlp",
    "execlpe",
    "execv",
    "execve",
    "execvp",
    "execvpe",
    "spawnl",
    "spawnle",
    "spawnlp",
    "spawnlpe",
    "spawnv",
    "spawnve",
    "spawnvp",
    "spawnvpe",
    "fork",
    "forkpty",
    "system",
    "popen",
})


def code_evolution_proposal_semantic_error(
    incident: Mapping[str, Any],
    file_updates: Sequence[Mapping[str, Any]],
) -> str:
    """Reject a structurally valid proposal that violates safety or evidence roles.

    Generic AST execution-authority invariants apply to every incident class.
    Release-closure repair additionally enforces receipt-evidence role rules.
    """

    for update in file_updates:
        path = str(update.get("path") or "").replace("\\", "/")
        from eimemory.governance.code_evolution_path_policy import path_denied_by_self

        if path_denied_by_self(path):
            return "code_evolution_deny_self_path"
        content = update.get("content")
        if not isinstance(content, str):
            return "proposal_file_content_invalid"
        if path.endswith(".py"):
            authority_error = python_execution_authority_error(content, filename=path)
            if authority_error:
                return authority_error

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


def python_execution_authority_error(content: str, *, filename: str = "<proposal>") -> str:
    """AST reject of importlib/os.exec*/network/ctypes/dynamic getattr onto them."""

    try:
        module = ast.parse(content, filename=filename)
    except SyntaxError:
        return "proposal_python_syntax_invalid"

    aliases: dict[str, str] = {}
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                local = alias.asname or alias.name
                aliases[local] = alias.name
                if root in _BANNED_IMPORT_ROOTS or alias.name.startswith("http.client"):
                    return f"execution_authority_banned_import:{root}"
        elif isinstance(node, ast.ImportFrom):
            module_name = str(node.module or "")
            root = module_name.split(".", 1)[0] if module_name else ""
            for alias in node.names:
                local = alias.asname or alias.name
                if module_name:
                    aliases[local] = f"{module_name}.{alias.name}"
                if module_name == "importlib" and alias.name in {"import_module", "__import__"}:
                    return "execution_authority_banned_call:importlib.import_module"
            if root in _BANNED_IMPORT_ROOTS or module_name.startswith("http.client"):
                return f"execution_authority_banned_import:{root or module_name}"
        elif isinstance(node, ast.Call):
            banned = _banned_call_reason(node, aliases)
            if banned:
                return banned
    return ""


def _banned_call_reason(node: ast.Call, aliases: Mapping[str, str]) -> str:
    func = node.func
    # importlib.import_module(...)
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        owner = func.value.id
        resolved = aliases.get(owner, owner)
        root = resolved.split(".", 1)[0]
        if root == "importlib" and func.attr in {"import_module", "__import__"}:
            return "execution_authority_banned_call:importlib.import_module"
        if root == "os" and func.attr in _BANNED_OS_CALLS:
            return f"execution_authority_banned_call:os.{func.attr}"
        if root in _BANNED_IMPORT_ROOTS:
            return f"execution_authority_banned_call:{root}.{func.attr}"
    if isinstance(func, ast.Name):
        resolved = aliases.get(func.id, func.id)
        if resolved.endswith("import_module") or resolved == "__import__":
            return "execution_authority_banned_call:importlib.import_module"
        leaf = resolved.rsplit(".", 1)[-1]
        if leaf in _BANNED_OS_CALLS and (
            resolved.startswith("os.") or aliases.get(func.id, "").startswith("os.")
        ):
            return f"execution_authority_banned_call:os.{leaf}"
    # getattr(os, "system") / getattr(importlib, "import_module")
    if isinstance(func, ast.Name) and func.id == "getattr" and len(node.args) >= 2:
        target = node.args[0]
        attr = node.args[1]
        target_name = ""
        if isinstance(target, ast.Name):
            target_name = aliases.get(target.id, target.id)
        if isinstance(attr, ast.Constant) and isinstance(attr.value, str):
            root = target_name.split(".", 1)[0]
            if root == "os" and attr.value in _BANNED_OS_CALLS:
                return f"execution_authority_banned_getattr:os.{attr.value}"
            if root in _BANNED_IMPORT_ROOTS:
                return f"execution_authority_banned_getattr:{root}.{attr.value}"
            if root == "importlib" and attr.value in {"import_module", "__import__"}:
                return "execution_authority_banned_getattr:importlib.import_module"
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


__all__ = [
    "code_evolution_proposal_semantic_error",
    "python_execution_authority_error",
]
