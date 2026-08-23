"""Sealed, proposal-only evaluator for the Hermes code implementation binding."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from hashlib import sha256
import ast
import json
from pathlib import Path
import stat
import tempfile
from typing import Any

from eimemory.adapters.hermes.code_implementation import (
    BINDING_ID,
    CAPABILITY_ID,
    IMPLEMENTATION_DIGEST,
    CodeImplementationError,
    CodeImplementationSocketClient,
    OPERATION,
    PROVIDER_INSTANCE_ID,
    REVISION_ID,
    build_request,
    canonical_json,
    validate_attestation,
    validate_response,
)
from eimemory.governance.code_evolution_test_plans import (
    CODE_IMPLEMENTATION_CATALOG_TEST_PLAN_ID,
    protected_test_plan_digest,
)


CATALOG_EXECUTOR_ID = "hongtu.eval.code-implementation"
CATALOG_EXECUTOR_REVISION = "v2"
CATALOG_CASE_ID = "hongtu_code_implementation_v2"
CATALOG_TEST_PLAN_ID = CODE_IMPLEMENTATION_CATALOG_TEST_PLAN_ID
_BASE_COMMIT = "0" * 40
_STATIC_FIXTURE_CONTENT = "VALUE = 1\n"
_STATIC_FIXTURE_DIGEST = sha256(_STATIC_FIXTURE_CONTENT.encode("utf-8")).hexdigest()


def _tree_digest(root: Path, files: Sequence[str]) -> str:
    entries: list[dict[str, str]] = []
    for relative in sorted(files):
        path = root / relative
        if not path.is_file() or path.is_symlink():
            raise ValueError("catalog_fixture_file_invalid")
        data = path.read_bytes().replace(b"\r\n", b"\n")
        entries.append({"path": relative, "sha256": sha256(data).hexdigest()})
    return sha256(canonical_json(entries).encode("utf-8")).hexdigest()


def _complete_fixture_digest(root: Path) -> str:
    try:
        root.lstat()
    except OSError as exc:
        raise ValueError("catalog_fixture_root_invalid") from exc
    if root.is_symlink() or not root.is_dir():
        raise ValueError("catalog_fixture_root_invalid")
    entries: list[dict[str, str]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not (
            stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)
        ):
            raise ValueError("catalog_fixture_entry_invalid")
        if stat.S_ISDIR(metadata.st_mode):
            entries.append({"path": f"{relative}/", "sha256": "directory"})
            continue
        normalized = path.read_bytes().replace(b"\r\n", b"\n")
        entries.append({"path": relative, "sha256": sha256(normalized).hexdigest()})
    return sha256(canonical_json(entries).encode("utf-8")).hexdigest()


def evaluate_static_code_implementation_proposal(
    root: Path,
    updates: Sequence[Mapping[str, str]],
) -> dict[str, Any]:
    """Trusted syntax/compile/fixture behavior check for the sealed case."""

    syntax_valid = True
    compiled = True
    fixture_behavior_valid = False
    try:
        parsed_by_path: dict[str, ast.AST] = {}
        for update in updates:
            relative = str(update.get("path") or "")
            if not relative.endswith(".py"):
                raise SyntaxError("catalog update is not Python")
            source = (root / relative).read_text(encoding="utf-8")
            tree = ast.parse(source, filename=relative)
            compile(tree, relative, "exec")
            parsed_by_path[relative] = tree
        fixture_tree = parsed_by_path.get("fixture.py")
        if fixture_tree is not None:
            fixture_behavior_valid = any(
                isinstance(node, (ast.Assign, ast.AnnAssign))
                and any(
                    isinstance(target, ast.Name) and target.id == "VALUE"
                    for target in (
                        node.targets
                        if isinstance(node, ast.Assign)
                        else [node.target]
                    )
                )
                and isinstance(node.value, ast.Constant)
                and node.value.value == 2
                for node in ast.walk(fixture_tree)
            )
            # The static fixture has no reason to import or execute calls.
            fixture_behavior_valid = fixture_behavior_valid and not any(
                isinstance(node, (ast.Call, ast.Import, ast.ImportFrom))
                for node in ast.walk(fixture_tree)
            )
    except (OSError, UnicodeError, SyntaxError, ValueError):
        syntax_valid = False
        compiled = False
    return {
        "ok": syntax_valid and compiled and fixture_behavior_valid,
        "syntax_valid": syntax_valid,
        "compiled": compiled,
        "fixture_behavior_valid": fixture_behavior_valid,
        "proposal_only": True,
    }


def _read_allowed_files(root: Path, files: Sequence[str]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for relative in files:
        path = root / relative
        data = path.read_bytes().replace(b"\r\n", b"\n")
        result.append({"path": relative, "sha256": sha256(data).hexdigest(), "content": data.decode("utf-8")})
    return result


def run_code_implementation_catalog_pass(
    *,
    provider: Any,
    fixture_root: str | Path,
    fixture_files: Sequence[str],
    evaluator: Callable[[Path, Sequence[Mapping[str, str]]], Any],
    now: str = "",
) -> dict[str, Any]:
    """Invoke a live provider against static source and evaluate in isolation.

    The provider receives source bytes only.  The source fixture is never used
    as the evaluator's write target; a source digest mismatch after invocation
    is a hard failure and is retained in the receipt.
    """

    root = Path(fixture_root).resolve()
    files = tuple(str(item).replace("\\", "/") for item in fixture_files)
    if not files or len(files) > 4 or any(not item or item.startswith("/") or ".." in Path(item).parts for item in files):
        return {"ok": False, "reason": "catalog_fixture_file_invalid"}
    try:
        allowed = _read_allowed_files(root, files)
        allowed_before = _tree_digest(root, files)
        before = _complete_fixture_digest(root)
        request = build_request(
            transaction_id="catalog-code-implementation",
            request_id="catalog-code-implementation-request",
            nonce=sha256(f"{before}:{now}".encode()).hexdigest()[:32],
            incident={
                "incident_id": "catalog-code-implementation",
                "incident_digest": sha256(b"catalog-code-implementation").hexdigest(),
                "incident_class": "catalog.provider_contract",
                "title": "Catalog provider contract",
                "summary": "Return one bounded replacement proposal for the protected fixture.",
                "diagnostic_codes": ["catalog_contract"],
                "acceptance_requirements": ["schema_valid", "source_unchanged"],
            },
            base={"commit": _BASE_COMMIT, "tree_digest": allowed_before},
            allowed_files=allowed,
            bounds={
                "maximum_files": len(allowed),
                "maximum_bytes_per_file": 49_152,
                "maximum_total_bytes": 96_000,
                "maximum_changed_lines": 400,
            },
            test_plan_id=CATALOG_TEST_PLAN_ID,
            test_plan_digest=protected_test_plan_digest(CATALOG_TEST_PLAN_ID),
        )
        method = getattr(provider, "propose_patch_v2", None)
        if not callable(method):
            return {"ok": False, "reason": "provider_operation_unavailable", "provider_invoked": False}
        raw_response = method(request)
        after = _complete_fixture_digest(root)
        if after != before:
            return {
                "ok": False,
                "reason": "provider_mutated_fixture",
                "provider_invoked": True,
                "fixture_before_digest": before,
                "fixture_after_digest": after,
            }
        if not isinstance(raw_response, Mapping) or set(raw_response) != {
            "ok",
            "operation",
            "attestation",
            "response",
        }:
            return {
                "ok": False,
                "reason": "provider_attestation_invalid",
                "provider_invoked": True,
                "fixture_before_digest": before,
                "fixture_after_digest": after,
            }
        response = raw_response.get("response")
        try:
            normalized = validate_response(response, request=request)
            attestation = validate_attestation(
                raw_response.get("attestation"),
                request=request,
                response=normalized,
            )
            if raw_response.get("ok") is not True or raw_response.get("operation") != OPERATION:
                raise CodeImplementationError("provider_envelope_identity_mismatch")
        except CodeImplementationError:
            return {
                "ok": False,
                "reason": "provider_attestation_invalid",
                "provider_invoked": True,
                "fixture_before_digest": before,
                "fixture_after_digest": after,
            }
        with tempfile.TemporaryDirectory(prefix="code-implementation-evaluator-") as temporary:
            evaluator_root = Path(temporary)
            for item in allowed:
                destination = evaluator_root / item["path"]
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(item["content"], encoding="utf-8")
            for update in normalized["file_updates"]:
                destination = evaluator_root / update["path"]
                destination.write_text(update["content"], encoding="utf-8")
            evaluated = evaluator(evaluator_root, normalized["file_updates"])
            evaluation = evaluated if isinstance(evaluated, Mapping) else {"ok": evaluated is True}
        completed_at = now or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        receipt_material = {
            "schema": "code_implementation_catalog_receipt.v1",
            "provider_instance_id": PROVIDER_INSTANCE_ID,
            "binding_id": BINDING_ID,
            "revision_id": REVISION_ID,
            "operation": OPERATION,
            "implementation_digest": IMPLEMENTATION_DIGEST,
            "fixture_before_digest": before,
            "fixture_after_digest": after,
            "request_digest": request["request_digest"],
            "response_digest": sha256(canonical_json(normalized).encode()).hexdigest(),
            "provider_attestation_digest": sha256(canonical_json(attestation).encode()).hexdigest(),
            "evaluation": dict(evaluation),
            "completed_at": completed_at,
        }
        return {
            "ok": bool(evaluation.get("ok") is True),
            "provider_invoked": True,
            "provider_instance_id": PROVIDER_INSTANCE_ID,
            "capability_id": CAPABILITY_ID,
            "revision_id": REVISION_ID,
            "binding_id": BINDING_ID,
            "operation": OPERATION,
            "fixture_before_digest": before,
            "fixture_after_digest": after,
            "request_digest": request["request_digest"],
            "provider_attestation_digest": receipt_material["provider_attestation_digest"],
            "evaluation": dict(evaluation),
            "receipt_digest": sha256(canonical_json(receipt_material).encode()).hexdigest(),
            "receipt": receipt_material,
            "completed_at": completed_at,
        }
    except Exception as exc:
        return {"ok": False, "reason": f"catalog_pass_failed:{type(exc).__name__}", "provider_invoked": False}


def validate_code_implementation_catalog_receipt(
    value: Mapping[str, Any],
    *,
    receipt_digest: str,
) -> dict[str, Any]:
    """Validate one durable provider-specific incubation receipt."""

    required = {
        "schema",
        "provider_instance_id",
        "binding_id",
        "revision_id",
        "operation",
        "implementation_digest",
        "fixture_before_digest",
        "fixture_after_digest",
        "request_digest",
        "response_digest",
        "provider_attestation_digest",
        "evaluation",
        "completed_at",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise CodeImplementationError("catalog_receipt_fields_invalid")
    if (
        value.get("schema") != "code_implementation_catalog_receipt.v1"
        or value.get("provider_instance_id") != PROVIDER_INSTANCE_ID
        or value.get("binding_id") != BINDING_ID
        or value.get("revision_id") != REVISION_ID
        or value.get("operation") != OPERATION
        or value.get("implementation_digest") != IMPLEMENTATION_DIGEST
        or value.get("fixture_before_digest") != value.get("fixture_after_digest")
        or not isinstance(value.get("evaluation"), Mapping)
        or value["evaluation"].get("ok") is not True
    ):
        raise CodeImplementationError("catalog_receipt_contract_invalid")
    for key in (
        "implementation_digest",
        "fixture_before_digest",
        "fixture_after_digest",
        "request_digest",
        "response_digest",
        "provider_attestation_digest",
    ):
        if not isinstance(value.get(key), str) or len(value[key]) != 64 or any(
            char not in "0123456789abcdef" for char in value[key]
        ):
            raise CodeImplementationError("catalog_receipt_digest_invalid")
    completed_at = str(value.get("completed_at") or "")
    try:
        parsed = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CodeImplementationError("catalog_receipt_timestamp_invalid") from exc
    if parsed.tzinfo is None:
        raise CodeImplementationError("catalog_receipt_timestamp_invalid")
    actual_digest = sha256(canonical_json(value).encode()).hexdigest()
    if receipt_digest != actual_digest:
        raise CodeImplementationError("catalog_receipt_digest_mismatch")
    return dict(value)


def evaluate_code_implementation(input_data: dict[str, Any], fixture: dict[str, Any], runtime: Any) -> dict[str, Any]:
    """Catalog executor hook using a fresh client and sealed static fixture."""

    if input_data != {"operation": OPERATION} or fixture != {
        "fixture_schema": "code_implementation_static_fixture.v1",
        "fixture_sha256": _STATIC_FIXTURE_DIGEST,
    }:
        return {"execution_ok": False, "provider_ready": False, "reason": "catalog_fixture_contract_mismatch"}
    with tempfile.TemporaryDirectory(prefix="code-implementation-source-fixture-") as temporary:
        fixture_root = Path(temporary)
        (fixture_root / "fixture.py").write_text(_STATIC_FIXTURE_CONTENT, encoding="utf-8")
        result = run_code_implementation_catalog_pass(
            provider=CodeImplementationSocketClient(),
            fixture_root=fixture_root,
            fixture_files=("fixture.py",),
            evaluator=evaluate_static_code_implementation_proposal,
            now=datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
        )
    return {
        "execution_ok": result.get("ok") is True,
        "provider_ready": result.get("provider_invoked") is True,
        "proposal_only": True,
        "receipt_digest": str(result.get("receipt_digest") or ""),
        "provider_attestation_digest": str(result.get("provider_attestation_digest") or ""),
        "receipt": dict(result.get("receipt") or {}),
        "reason": str(result.get("reason") or ""),
    }


def install_code_implementation_catalog(bootstrap: Any) -> None:
    registration = bootstrap.register_executor(
        executor_id=CATALOG_EXECUTOR_ID,
        revision=CATALOG_EXECUTOR_REVISION,
        handler=evaluate_code_implementation,
        contract_descriptor={
            "operation": OPERATION,
            "binding_id": BINDING_ID,
            "proposal_only": True,
            "source_write": False,
        },
    )
    from eimemory.evaluation.capability_catalog import CatalogCase

    bootstrap.register_case(
        CatalogCase(
            case_id=CATALOG_CASE_ID,
            capability_id=CAPABILITY_ID,
            executor_id=CATALOG_EXECUTOR_ID,
            executor_revision=CATALOG_EXECUTOR_REVISION,
            executor_contract_digest=registration.contract_digest,
            input_data={"operation": OPERATION},
            fixture={
                "fixture_schema": "code_implementation_static_fixture.v1",
                "fixture_sha256": _STATIC_FIXTURE_DIGEST,
            },
            expected_invariants=[
                {"field": "execution_ok", "op": "eq", "value": True},
                {"field": "provider_ready", "op": "eq", "value": True},
                {"field": "proposal_only", "op": "eq", "value": True},
            ],
            binding_selector={"binding_ids": [BINDING_ID]},
            revision=REVISION_ID,
        )
    )


__all__ = [
    "CATALOG_CASE_ID",
    "CATALOG_EXECUTOR_ID",
    "CATALOG_EXECUTOR_REVISION",
    "evaluate_static_code_implementation_proposal",
    "evaluate_code_implementation",
    "install_code_implementation_catalog",
    "run_code_implementation_catalog_pass",
    "validate_code_implementation_catalog_receipt",
]
