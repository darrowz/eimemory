"""Strict proposal-only Hermes code-implementation adapter.

The model-facing side of this module is deliberately data-only.  It accepts
one pre-built request and returns a bounded replacement proposal; it has no
shell, argv, Git, deployment, environment, or secret authority.  Repository
effects belong to the promotion/transaction owner elsewhere in EIMemory.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
import difflib
import json
import os
from hashlib import sha256
from pathlib import Path, PurePosixPath
import re
import socket
import stat
import struct
import threading
import time
from typing import Any


CAPABILITY_ID = "code.implementation"
REVISION_ID = "code.implementation:v2"
BINDING_ID = "binding.hermes.code-implementation:v2"
PROVIDER_KIND = "hermes"
PROVIDER_INSTANCE_ID = "hermes.eimemory.code-implementation.production"
OPERATION = "propose_patch_v2"
SIDE_EFFECT_CLASS = "network"
REQUEST_SCHEMA = "code_implementation_request.v2"
RESPONSE_SCHEMA = "code_implementation_response.v2"
ATTESTATION_SCHEMA = "code_implementation_attestation.v2"
DEFAULT_SOCKET_PATH = Path("/var/lib/eimemory/run/hermes-code-implementation.v2.sock")
REQUEST_LIMIT = 128 * 1024
RESPONSE_LIMIT = 256 * 1024
MAX_ALLOWED_FILES = 4
MAX_FILE_BYTES = 48 * 1024
MAX_TOTAL_BYTES = 96 * 1024
MAX_CHANGED_LINES = 400
MAX_DIFF_BYTES = 256 * 1024
FIXED_COMPLETION_TASK = "eimemory_code_implementation"
FIXED_COMPLETION_MAX_TOKENS = 8192
FIXED_COMPLETION_TIMEOUT_SECONDS = 120.0
PROVIDER_RATE_LIMIT = 8
PROVIDER_RATE_WINDOW_SECONDS = 60.0
FIXED_COMPLETION_INSTRUCTIONS = (
    "Return only the declared code_implementation_response.v2 object. "
    "Propose bounded file replacements; never emit or request shell, argv, "
    "commands, Git, deployment, environment, secrets, credentials, paths, "
    "or policy instructions."
)
_HEX64 = set("0123456789abcdef")
_FORBIDDEN_KEYS = {
    "argv",
    "command",
    "commands",
    "cwd",
    "env",
    "environment",
    "executable",
    "git",
    "shell",
    "secret",
    "secrets",
    "token",
    "tokens",
}
_SECRET_PATH_PARTS = frozenset({"credential", "credentials", "secret", "secrets", "token", "tokens"})
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----"),
    re.compile(r"https?://[^\s/:@]+:[^\s/@]+@", re.IGNORECASE),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{32,})\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    re.compile(
        r"(?im)^\s*[A-Z][A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PRIVATE_KEY|ACCESS_KEY)[A-Z0-9_]*\s*=\s*\S+"
    ),
)
_HIGH_ENTROPY_TOKEN = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z0-9+/=_-]{48,}(?![A-Za-z0-9_])")
_EXECUTION_AUTHORITY = re.compile(
    r"(?:\bsubprocess\b|\bos\.system\s*\(|\bos\.popen\s*\(|\bpty\.spawn\s*\(|"
    r"\bshell\s*=\s*True\b|\b(?:eval|exec|__import__)\s*\(|"
    r"\bgit\s+(?:push|commit|reset|checkout)\b|/dev-project/eimemory|/opt/eimemory)",
    re.IGNORECASE,
)


class CodeImplementationError(ValueError):
    """A provider request, response, digest, or transport is unsafe."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _strict_pairs(pairs: list[tuple[Any, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if not isinstance(key, str) or key in result:
            raise CodeImplementationError("duplicate_or_nontext_json_key")
        result[key] = value
    return result


def _digest(value: Any) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _text(value: Any, *, field: str, maximum: int = 512) -> str:
    if not isinstance(value, str):
        raise CodeImplementationError(f"{field}_must_be_text")
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized or len(normalized) > maximum or "\x00" in normalized:
        raise CodeImplementationError(f"{field}_invalid")
    return normalized


def _sha(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in _HEX64 for char in value):
        raise CodeImplementationError(f"{field}_invalid")
    return value


def _safe_relative_path(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise CodeImplementationError(f"{field}_invalid")
    raw = value.replace("\\", "/")
    path = PurePosixPath(raw)
    if (
        not raw
        or path.is_absolute()
        or raw.startswith("//")
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(token in raw for token in ("*", "?", "[", "]", ":"))
        or "\x00" in raw
    ):
        raise CodeImplementationError(f"{field}_unsafe")
    lowered_parts = {
        token
        for part in path.parts
        for token in re.split(r"[^a-z0-9]+", part.lower())
        if token
    }
    if lowered_parts & _SECRET_PATH_PARTS:
        raise CodeImplementationError(f"{field}_secret_like")
    return "/".join(path.parts)


def _contains_secret_material(value: str) -> bool:
    if any(pattern.search(value) for pattern in _SECRET_PATTERNS):
        return True
    for match in _HIGH_ENTROPY_TOKEN.finditer(value):
        token = match.group(0).rstrip("=")
        if all(char in "0123456789abcdefABCDEF" for char in token):
            continue
        classes = sum(
            any(predicate(char) for char in token)
            for predicate in (
                str.islower,
                str.isupper,
                str.isdigit,
                lambda char: char in "+/=_-",
            )
        )
        if classes >= 3:
            return True
    return False


def _added_lines(prior: str, replacement: str) -> tuple[str, ...]:
    return tuple(
        line[2:]
        for line in difflib.ndiff(prior.splitlines(), replacement.splitlines())
        if line.startswith("+ ")
    )


def _reject_forbidden_keys(value: Any, *, location: str = "payload") -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key).strip().lower()
            if key in _FORBIDDEN_KEYS:
                raise CodeImplementationError(f"{location}_{key}_forbidden")
            _reject_forbidden_keys(child, location=f"{location}.{key}")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            _reject_forbidden_keys(child, location=f"{location}[{index}]")


def _exact_keys(value: Mapping[str, Any], expected: set[str], *, field: str) -> None:
    unknown = set(value).difference(expected)
    missing = expected.difference(value)
    if unknown:
        raise CodeImplementationError(f"{field}_unknown_fields")
    if missing:
        raise CodeImplementationError(f"{field}_missing_fields")


def _validate_incident(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CodeImplementationError("incident_must_be_object")
    expected = {
        "incident_id",
        "incident_digest",
        "incident_class",
        "title",
        "summary",
        "diagnostic_codes",
        "acceptance_requirements",
    }
    _exact_keys(value, expected, field="incident")
    _reject_forbidden_keys(value, location="incident")
    result = dict(value)
    _text(result["incident_id"], field="incident_id", maximum=160)
    _sha(result["incident_digest"], field="incident_digest")
    _text(result["incident_class"], field="incident_class", maximum=160)
    _text(result["title"], field="incident_title", maximum=512)
    _text(result["summary"], field="incident_summary", maximum=4096)
    for key in ("diagnostic_codes", "acceptance_requirements"):
        values = result[key]
        if not isinstance(values, list) or not values or len(values) > 64 or any(not isinstance(item, str) for item in values):
            raise CodeImplementationError(f"{key}_invalid")
        result[key] = [_text(item, field=key, maximum=256) for item in values]
    incident_text = "\n".join(
        [str(result["title"]), str(result["summary"])]
        + list(result["diagnostic_codes"])
        + list(result["acceptance_requirements"])
    )
    if _EXECUTION_AUTHORITY.search(incident_text):
        raise CodeImplementationError("incident_execution_authority")
    if _contains_secret_material(incident_text):
        raise CodeImplementationError("incident_secret_material")
    return result


def _validate_allowed_files(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value or len(value) > MAX_ALLOWED_FILES:
        raise CodeImplementationError("allowed_files_invalid")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    total = 0
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise CodeImplementationError("allowed_file_invalid")
        _exact_keys(item, {"path", "sha256", "content"}, field=f"allowed_files[{index}]")
        path = _safe_relative_path(item["path"], field=f"allowed_files[{index}].path")
        if path in seen:
            raise CodeImplementationError("allowed_files_duplicate")
        seen.add(path)
        digest = _sha(item["sha256"], field="allowed_file_sha256")
        content = _text(item["content"], field="allowed_file_content", maximum=MAX_FILE_BYTES)
        if _contains_secret_material(content):
            raise CodeImplementationError("allowed_file_secret_material")
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_FILE_BYTES or sha256(encoded).hexdigest() != digest:
            raise CodeImplementationError("allowed_file_digest_mismatch")
        total += len(encoded)
        result.append({"path": path, "sha256": digest, "content": content})
    if total > MAX_TOTAL_BYTES:
        raise CodeImplementationError("allowed_files_total_bytes_exceeded")
    return result


def _validate_bounds(value: Any) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise CodeImplementationError("bounds_must_be_object")
    expected = {
        "maximum_files",
        "maximum_bytes_per_file",
        "maximum_total_bytes",
        "maximum_changed_lines",
    }
    _exact_keys(value, expected, field="bounds")
    result: dict[str, int] = {}
    for key in expected:
        raw = value[key]
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
            raise CodeImplementationError(f"bounds_{key}_invalid")
        result[key] = raw
    if result["maximum_files"] > MAX_ALLOWED_FILES or result["maximum_bytes_per_file"] > MAX_FILE_BYTES:
        raise CodeImplementationError("bounds_exceed_provider_limits")
    if result["maximum_total_bytes"] > MAX_TOTAL_BYTES or result["maximum_changed_lines"] > MAX_CHANGED_LINES:
        raise CodeImplementationError("bounds_exceed_provider_limits")
    return result


def build_request(
    *,
    transaction_id: str,
    request_id: str,
    nonce: str,
    incident: Mapping[str, Any],
    base: Mapping[str, Any],
    allowed_files: Sequence[Mapping[str, Any]],
    bounds: Mapping[str, Any],
    test_plan_id: str,
    test_plan_digest: str,
) -> dict[str, Any]:
    body = {
        "schema": REQUEST_SCHEMA,
        "operation": OPERATION,
        "capability_id": CAPABILITY_ID,
        "revision_id": REVISION_ID,
        "binding_id": BINDING_ID,
        "provider_instance_id": PROVIDER_INSTANCE_ID,
        "transaction_id": _text(transaction_id, field="transaction_id", maximum=160),
        "request_id": _text(request_id, field="request_id", maximum=160),
        "nonce": _text(nonce, field="nonce", maximum=256),
        "incident": dict(incident),
        "base": dict(base),
        "allowed_files": [dict(item) for item in allowed_files],
        "bounds": dict(bounds),
        "test_plan_id": _text(test_plan_id, field="test_plan_id", maximum=256),
        "test_plan_digest": _sha(test_plan_digest, field="test_plan_digest"),
    }
    normalized = validate_request({**body, "request_digest": _digest(body)}, verify_digest=True)
    return normalized


def validate_request(value: Mapping[str, Any], *, verify_digest: bool = True) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CodeImplementationError("request_must_be_object")
    expected = {
        "schema",
        "operation",
        "capability_id",
        "revision_id",
        "binding_id",
        "provider_instance_id",
        "transaction_id",
        "request_id",
        "nonce",
        "request_digest",
        "incident",
        "base",
        "allowed_files",
        "bounds",
        "test_plan_id",
        "test_plan_digest",
    }
    _exact_keys(value, expected, field="request")
    _reject_forbidden_keys({key: child for key, child in value.items() if key not in {"allowed_files"}}, location="request")
    if value.get("schema") != REQUEST_SCHEMA or value.get("operation") != OPERATION:
        raise CodeImplementationError("request_contract_mismatch")
    if value.get("capability_id") != CAPABILITY_ID or value.get("revision_id") != REVISION_ID:
        raise CodeImplementationError("request_capability_mismatch")
    if value.get("binding_id") != BINDING_ID or value.get("provider_instance_id") != PROVIDER_INSTANCE_ID:
        raise CodeImplementationError("request_binding_mismatch")
    result = dict(value)
    for key in ("transaction_id", "request_id", "nonce", "test_plan_id"):
        result[key] = _text(result[key], field=key)
    _sha(result["request_digest"], field="request_digest")
    result["incident"] = _validate_incident(result["incident"])
    if not isinstance(result["base"], Mapping):
        raise CodeImplementationError("base_must_be_object")
    _exact_keys(result["base"], {"commit", "tree_digest"}, field="base")
    result["base"] = {
        "commit": _text(result["base"]["commit"], field="base_commit", maximum=40),
        "tree_digest": _sha(result["base"]["tree_digest"], field="base_tree_digest"),
    }
    if len(result["base"]["commit"]) != 40 or any(char not in _HEX64 for char in result["base"]["commit"]):
        raise CodeImplementationError("base_commit_invalid")
    result["allowed_files"] = _validate_allowed_files(result["allowed_files"])
    result["bounds"] = _validate_bounds(result["bounds"])
    result["test_plan_digest"] = _sha(result["test_plan_digest"], field="test_plan_digest")
    from eimemory.governance.code_evolution_test_plans import protected_test_plan

    plan = protected_test_plan(result["test_plan_id"])
    if plan is None or plan.digest != result["test_plan_digest"]:
        raise CodeImplementationError("test_plan_not_protected")
    if tuple(item["path"] for item in result["allowed_files"]) != tuple(plan.allowed_files):
        raise CodeImplementationError("allowed_files_not_protected")
    if verify_digest:
        body = {key: result[key] for key in result if key != "request_digest"}
        if _digest(body) != result["request_digest"]:
            raise CodeImplementationError("request_digest_mismatch")
    return result


def _validate_file_updates(value: Any, allowed: Mapping[str, Mapping[str, str]]) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value or len(value) > MAX_ALLOWED_FILES:
        raise CodeImplementationError("file_updates_invalid")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    total = 0
    changed_lines = 0
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise CodeImplementationError("file_update_invalid")
        _exact_keys(item, {"path", "prior_sha256", "content"}, field=f"file_updates[{index}]")
        path = _safe_relative_path(item["path"], field=f"file_updates[{index}].path")
        if path in seen or path not in allowed:
            raise CodeImplementationError("file_update_path_not_allowed")
        seen.add(path)
        prior = _sha(item["prior_sha256"], field="prior_sha256")
        if prior != allowed[path]["sha256"]:
            raise CodeImplementationError("file_update_prior_digest_mismatch")
        content = _text(item["content"], field="file_update_content", maximum=MAX_FILE_BYTES)
        encoded = content.encode("utf-8")
        total += len(encoded)
        prior_content = str(allowed[path].get("content") or "")
        additions = "\n".join(_added_lines(prior_content, content))
        if _EXECUTION_AUTHORITY.search(additions):
            raise CodeImplementationError("file_update_execution_authority")
        if _contains_secret_material(additions):
            raise CodeImplementationError("file_update_secret_material")
        if prior_content:
            diff_lines = difflib.unified_diff(
                prior_content.splitlines(),
                content.splitlines(),
                lineterm="",
            )
            changed_lines += sum(
                1
                for line in diff_lines
                if (line.startswith("+") or line.startswith("-"))
                and not line.startswith(("+++", "---"))
            )
        else:
            changed_lines += max(content.count("\n"), 1)
        if len(encoded) > MAX_FILE_BYTES:
            raise CodeImplementationError("file_update_file_bytes_exceeded")
        result.append({"path": path, "prior_sha256": prior, "content": content})
    if total > MAX_TOTAL_BYTES or changed_lines > MAX_CHANGED_LINES:
        raise CodeImplementationError("file_update_bounds_exceeded")
    return result


def validate_response(value: Mapping[str, Any], *, request: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CodeImplementationError("response_must_be_object")
    expected = {"schema", "request_id", "request_digest", "file_updates", "rationale", "assumptions"}
    _exact_keys(value, expected, field="response")
    if value.get("schema") != RESPONSE_SCHEMA:
        raise CodeImplementationError("response_schema_invalid")
    request_id = _text(value.get("request_id"), field="response_request_id", maximum=160)
    request_digest = _sha(value.get("request_digest"), field="response_request_digest")
    rationale = _text(value.get("rationale"), field="rationale", maximum=4096)
    assumptions = value.get("assumptions")
    if not isinstance(assumptions, list) or len(assumptions) > 32 or any(not isinstance(item, str) for item in assumptions):
        raise CodeImplementationError("assumptions_invalid")
    response_text = "\n".join([rationale, *assumptions])
    if _EXECUTION_AUTHORITY.search(response_text):
        raise CodeImplementationError("response_execution_authority")
    if _contains_secret_material(response_text):
        raise CodeImplementationError("response_secret_material")
    allowed: dict[str, Mapping[str, str]] = {}
    if request is not None:
        normalized_request = validate_request(request)
        if request_id != normalized_request["request_id"] or request_digest != normalized_request["request_digest"]:
            raise CodeImplementationError("response_request_identity_mismatch")
        allowed = {item["path"]: item for item in normalized_request["allowed_files"]}
    else:
        raw_updates = value.get("file_updates")
        if isinstance(raw_updates, list):
            for item in raw_updates:
                if isinstance(item, Mapping) and isinstance(item.get("path"), str):
                    # A response received without its request is still
                    # checked against the protected bootstrap allowlist.  It
                    # must never be able to widen its own file authority.
                    if item["path"] != "eimemory/governance/l5_reader.py":
                        raise CodeImplementationError("file_update_path_not_allowed")
                    allowed[item["path"]] = {"sha256": str(item.get("prior_sha256") or "")}
    updates = _validate_file_updates(value.get("file_updates"), allowed)
    return {
        "schema": RESPONSE_SCHEMA,
        "request_id": request_id,
        "request_digest": request_digest,
        "file_updates": updates,
        "rationale": rationale,
        "assumptions": [_text(item, field="assumption", maximum=512) for item in assumptions],
    }


def _response_json_schema(request: Mapping[str, Any]) -> dict[str, Any]:
    normalized = validate_request(request)
    allowed_paths = [str(item["path"]) for item in normalized["allowed_files"]]
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema",
            "request_id",
            "request_digest",
            "file_updates",
            "rationale",
            "assumptions",
        ],
        "properties": {
            "schema": {"const": RESPONSE_SCHEMA},
            "request_id": {"const": normalized["request_id"]},
            "request_digest": {"const": normalized["request_digest"]},
            "file_updates": {
                "type": "array",
                "minItems": 1,
                "maxItems": min(MAX_ALLOWED_FILES, len(allowed_paths)),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["path", "prior_sha256", "content"],
                    "properties": {
                        "path": {"enum": allowed_paths},
                        "prior_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                        "content": {"type": "string", "maxLength": MAX_FILE_BYTES},
                    },
                },
            },
            "rationale": {"type": "string", "minLength": 1, "maxLength": 4096},
            "assumptions": {
                "type": "array",
                "maxItems": 32,
                "items": {"type": "string", "maxLength": 512},
            },
        },
    }


def _route_metadata(value: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    for key in ("provider", "model", "agent_id", "task"):
        raw = value.get(key) if isinstance(value, Mapping) else getattr(value, key, "")
        if isinstance(raw, str) and raw.strip():
            result[key] = _text(raw.strip(), field=f"route_{key}", maximum=256)
    return result


def build_attestation(
    request: Mapping[str, Any],
    response: Mapping[str, Any],
    *,
    completed_at: str,
    nonce: str,
    route: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_request = validate_request(request)
    normalized_response = validate_response(response, request=normalized_request)
    timestamp = _text(completed_at, field="completed_at", maximum=64)
    echoed_nonce = _text(nonce, field="nonce", maximum=256)
    if echoed_nonce != normalized_request["nonce"]:
        raise CodeImplementationError("attestation_nonce_mismatch")
    result = {
        "schema": ATTESTATION_SCHEMA,
        "operation": OPERATION,
        "capability_id": CAPABILITY_ID,
        "revision_id": REVISION_ID,
        "binding_id": BINDING_ID,
        "provider_instance_id": PROVIDER_INSTANCE_ID,
        "implementation_digest": IMPLEMENTATION_DIGEST,
        "completed_at": timestamp,
        "nonce": echoed_nonce,
        "request_id": normalized_request["request_id"],
        "request_digest": normalized_request["request_digest"],
        "response_digest": _digest(normalized_response),
        "response": normalized_response,
    }
    if route:
        result["route"] = _route_metadata(route)
    return result


def validate_attestation(
    value: Mapping[str, Any],
    *,
    request: Mapping[str, Any],
    response: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the complete provider identity around one proposal."""

    if not isinstance(value, Mapping):
        raise CodeImplementationError("attestation_must_be_object")
    required = {
        "schema",
        "operation",
        "capability_id",
        "revision_id",
        "binding_id",
        "provider_instance_id",
        "implementation_digest",
        "completed_at",
        "nonce",
        "request_id",
        "request_digest",
        "response_digest",
        "response",
    }
    unknown = set(value).difference(required | {"route"})
    missing = required.difference(value)
    if unknown:
        raise CodeImplementationError("attestation_unknown_fields")
    if missing:
        raise CodeImplementationError("attestation_missing_fields")
    normalized_request = validate_request(request)
    normalized_response = validate_response(response, request=normalized_request)
    if (
        value.get("schema") != ATTESTATION_SCHEMA
        or value.get("operation") != OPERATION
        or value.get("capability_id") != CAPABILITY_ID
        or value.get("revision_id") != REVISION_ID
        or value.get("binding_id") != BINDING_ID
        or value.get("provider_instance_id") != PROVIDER_INSTANCE_ID
        or value.get("implementation_digest") != IMPLEMENTATION_DIGEST
    ):
        raise CodeImplementationError("attestation_provider_identity_mismatch")
    if not IMPLEMENTATION_DIGEST:
        raise CodeImplementationError("attestation_implementation_digest_unavailable")
    if (
        value.get("nonce") != normalized_request["nonce"]
        or value.get("request_id") != normalized_request["request_id"]
        or value.get("request_digest") != normalized_request["request_digest"]
        or value.get("response_digest") != _digest(normalized_response)
        or value.get("response") != normalized_response
    ):
        raise CodeImplementationError("attestation_request_response_mismatch")
    completed_at = _text(value.get("completed_at"), field="completed_at", maximum=64)
    try:
        parsed = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CodeImplementationError("attestation_completed_at_invalid") from exc
    if parsed.tzinfo is None:
        raise CodeImplementationError("attestation_completed_at_invalid")
    result = {
        **dict(value),
        "completed_at": completed_at,
        "response": normalized_response,
    }
    if "route" in value:
        if not isinstance(value.get("route"), Mapping):
            raise CodeImplementationError("attestation_route_invalid")
        if set(value["route"]).difference({"provider", "model", "agent_id", "task"}):
            raise CodeImplementationError("attestation_route_invalid")
        result["route"] = _route_metadata(value["route"])
    return result


def _default_repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


DEFAULT_IMPLEMENTATION_PATHS = (
    "eimemory/adapters/hermes/code_implementation.py",
    "integrations/hermes/eimemory_hook/__init__.py",
    "integrations/hermes/eimemory_hook/plugin.yaml",
    "eimemory/capabilities/data/code_implementation.v2.json",
)


def implementation_digest(root: str | Path | None = None, *, relative_paths: Sequence[str] = DEFAULT_IMPLEMENTATION_PATHS) -> str:
    base = Path(root) if root is not None else _default_repo_root()
    operation_descriptor = {
        "capability_id": CAPABILITY_ID,
        "revision_id": REVISION_ID,
        "binding_id": BINDING_ID,
        "provider_kind": PROVIDER_KIND,
        "provider_instance_id": PROVIDER_INSTANCE_ID,
        "operation": OPERATION,
        "side_effect_class": SIDE_EFFECT_CLASS,
        "request_schema": REQUEST_SCHEMA,
        "response_schema": RESPONSE_SCHEMA,
    }
    contract_digest = _digest({"request": REQUEST_SCHEMA, "response": RESPONSE_SCHEMA})
    entries: list[dict[str, str]] = []
    for relative in sorted(str(item) for item in relative_paths):
        path = base / relative
        try:
            metadata = path.lstat()
        except FileNotFoundError as exc:
            raise CodeImplementationError("implementation_source_missing") from exc
        except OSError as exc:
            raise CodeImplementationError("implementation_source_unreadable") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise CodeImplementationError("implementation_source_symlinked")
        if not stat.S_ISREG(metadata.st_mode):
            raise CodeImplementationError("implementation_source_not_regular")
        try:
            normalized = path.read_bytes().replace(b"\r\n", b"\n")
        except OSError:
            raise CodeImplementationError("implementation_source_unreadable")
        entries.append({"path": relative, "sha256": sha256(normalized).hexdigest()})
    if not entries:
        raise CodeImplementationError("implementation_sources_missing")
    return sha256(
        (
            "code_implementation_provider.v2\n"
            + canonical_json(operation_descriptor)
            + contract_digest
            + canonical_json(entries)
        ).encode("utf-8")
    ).hexdigest()


try:
    IMPLEMENTATION_DIGEST = implementation_digest()
except CodeImplementationError:
    # Packaging/import validation may inspect the module before release assets
    # are copied.  A live resolver still rejects a missing complete set.
    IMPLEMENTATION_DIGEST = ""


def _recv_exact(connection: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            raise CodeImplementationError("socket_eof")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _validate_socket_path(path: Path) -> None:
    absolute = path.absolute()
    # sockaddr_un.sun_path is 108 bytes on Linux, including the terminating
    # NUL.  Validate the encoded byte length before bind/connect so an unsafe
    # deployment path fails with our stable contract error.
    if not absolute.is_absolute() or len(os.fsencode(absolute)) > 107:
        raise CodeImplementationError("socket_path_invalid")
    for component in (absolute.parent, *absolute.parent.parents):
        try:
            metadata = component.lstat()
        except OSError as exc:
            raise CodeImplementationError("socket_parent_missing") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise CodeImplementationError("socket_path_symlinked")
        if component == Path(absolute.anchor):
            break
    parent_metadata = absolute.parent.stat()
    if parent_metadata.st_uid != os.geteuid():
        raise CodeImplementationError("socket_parent_owner_invalid")
    if stat.S_IMODE(parent_metadata.st_mode) != 0o700:
        raise CodeImplementationError("socket_parent_permissions_invalid")
    try:
        metadata = absolute.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise CodeImplementationError("socket_path_unreadable") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise CodeImplementationError("socket_path_symlinked")
    if not stat.S_ISSOCK(metadata.st_mode):
        raise CodeImplementationError("socket_path_not_socket")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise CodeImplementationError("socket_permissions_invalid")
    if metadata.st_uid != os.geteuid():
        raise CodeImplementationError("socket_owner_invalid")


class CodeImplementationSocketClient:
    """Client for the fixed gateway-owned provider socket."""

    def __init__(self, *, socket_path: str | Path = DEFAULT_SOCKET_PATH, timeout_seconds: float = 15.0) -> None:
        self.socket_path = Path(socket_path)
        self.timeout_seconds = max(0.1, min(120.0, float(timeout_seconds)))

    def _call(self, operation: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if operation not in {"health", OPERATION}:
            raise CodeImplementationError("socket_operation_forbidden")
        request = {"operation": operation, **dict(payload)}
        encoded = canonical_json(request).encode("utf-8")
        if len(encoded) > REQUEST_LIMIT:
            raise CodeImplementationError("socket_request_too_large")
        _validate_socket_path(self.socket_path)
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.settimeout(self.timeout_seconds)
            connection.connect(str(self.socket_path))
            if hasattr(socket, "SO_PEERCRED"):
                credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
                _pid, uid, _gid = struct.unpack("3i", credentials)
                if uid != os.geteuid():
                    raise CodeImplementationError("socket_peer_uid_mismatch")
            connection.sendall(struct.pack(">I", len(encoded)) + encoded)
            header = _recv_exact(connection, 4)
            size = struct.unpack(">I", header)[0]
            if size > RESPONSE_LIMIT:
                raise CodeImplementationError("socket_response_too_large")
            raw = _recv_exact(connection, size)
        except (OSError, TimeoutError) as exc:
            raise CodeImplementationError("provider_transport_unavailable") from exc
        finally:
            connection.close()
        try:
            response = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_pairs)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CodeImplementationError("provider_response_invalid_json") from exc
        if not isinstance(response, dict):
            raise CodeImplementationError("provider_response_not_object")
        return response

    def health(self, *, nonce: str) -> dict[str, Any]:
        checked_nonce = _text(nonce, field="nonce", maximum=256)
        response = self._call("health", {"nonce": checked_nonce})
        if (
            response.get("ok") is not True
            or response.get("provider_instance_id") != PROVIDER_INSTANCE_ID
            or response.get("implementation_digest") != IMPLEMENTATION_DIGEST
            or response.get("nonce") != checked_nonce
        ):
            raise CodeImplementationError("provider_health_attestation_mismatch")
        return response

    def propose_patch_v2(self, request: Mapping[str, Any]) -> dict[str, Any]:
        normalized = validate_request(request)
        return self._call(OPERATION, normalized)


class CodeImplementationSocketServer:
    """Gateway-owned adapter for Hermes structured completion."""

    def __init__(
        self,
        ctx: Any,
        *,
        socket_path: str | Path = DEFAULT_SOCKET_PATH,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ctx = ctx
        self.socket_path = Path(socket_path)
        self._clock = clock
        self._listener: socket.socket | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._concurrency = threading.BoundedSemaphore(1)
        self._request_times: deque[float] = deque()

    def _admit_request(self) -> bool:
        now = float(self._clock())
        cutoff = now - PROVIDER_RATE_WINDOW_SECONDS
        while self._request_times and self._request_times[0] <= cutoff:
            self._request_times.popleft()
        if len(self._request_times) >= PROVIDER_RATE_LIMIT:
            return False
        self._request_times.append(now)
        return True

    @property
    def available(self) -> bool:
        llm = getattr(self.ctx, "llm", None)
        return (
            os.environ.get("EIMEMORY_HERMES_GATEWAY_PROCESS") == "1"
            and callable(getattr(llm, "complete_structured", None))
        )

    def start(self) -> bool:
        if not self.available:
            return False
        try:
            _prepare_server_socket_path(self.socket_path)
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(self.socket_path))
            os.chmod(self.socket_path, stat.S_IRUSR | stat.S_IWUSR)
            listener.listen(4)
            listener.settimeout(0.5)
        except (OSError, CodeImplementationError):
            try:
                if self.socket_path.is_socket():
                    self.socket_path.unlink()
            except OSError:
                pass
            return False
        self._listener = listener
        self._thread = threading.Thread(
            target=self._serve,
            name="eimemory-code-implementation",
            daemon=True,
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        listener = self._listener
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        try:
            if self.socket_path.is_socket() and stat.S_IMODE(self.socket_path.stat().st_mode) == 0o600:
                self.socket_path.unlink()
        except OSError:
            pass
        self._listener = None

    def _serve(self) -> None:
        listener = self._listener
        if listener is None:
            return
        while not self._stop.is_set():
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(
                target=self._serve_connection,
                args=(connection,),
                daemon=True,
            ).start()

    def _serve_connection(self, connection: socket.socket) -> None:
        if not self._concurrency.acquire(blocking=False):
            try:
                connection.close()
            except OSError:
                pass
            return
        try:
            self._serve_connection_serial(connection)
        finally:
            self._concurrency.release()

    def _serve_connection_serial(self, connection: socket.socket) -> None:
        try:
            if hasattr(socket, "SO_PEERCRED"):
                credentials = connection.getsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_PEERCRED,
                    struct.calcsize("3i"),
                )
                _pid, uid, _gid = struct.unpack("3i", credentials)
                if uid != os.geteuid():
                    raise CodeImplementationError("socket_peer_uid_mismatch")
            if not self._admit_request():
                raise CodeImplementationError("provider_rate_limited")
            header = _recv_exact(connection, 4)
            size = struct.unpack(">I", header)[0]
            if size > REQUEST_LIMIT:
                raise CodeImplementationError("socket_request_too_large")
            raw = _recv_exact(connection, size)
            request = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_pairs)
            if not isinstance(request, dict):
                raise CodeImplementationError("socket_request_not_object")
            operation = request.pop("operation", None)
            if operation == "health":
                response = {
                    "ok": True,
                    "operation": "health",
                    "provider_instance_id": PROVIDER_INSTANCE_ID,
                    "implementation_digest": IMPLEMENTATION_DIGEST,
                    "nonce": _text(request.get("nonce"), field="nonce", maximum=256),
                }
            elif operation == OPERATION:
                normalized = validate_request(request)
                response = self._complete(normalized)
            else:
                raise CodeImplementationError("socket_operation_forbidden")
        except CodeImplementationError as exc:
            response = {"ok": False, "reason": str(exc)[:256] or "provider_request_failed"}
        except (UnicodeDecodeError, json.JSONDecodeError, OSError):
            response = {"ok": False, "reason": "provider_request_invalid"}
        except Exception:
            # Host/provider exceptions can contain credentials or upstream
            # response bodies.  Keep the transport response fail-closed and
            # stable without reflecting exception text across the socket.
            response = {"ok": False, "reason": "structured_completion_failed"}
        encoded = canonical_json(response).encode("utf-8")
        if len(encoded) <= RESPONSE_LIMIT:
            try:
                connection.sendall(struct.pack(">I", len(encoded)) + encoded)
            except OSError:
                pass
        connection.close()

    def _complete(self, request: Mapping[str, Any]) -> dict[str, Any]:
        llm = getattr(self.ctx, "llm", None)
        complete = getattr(llm, "complete_structured", None)
        if not callable(complete):
            raise CodeImplementationError("structured_completion_unavailable")
        normalized_request = validate_request(request)
        raw = complete(
            task=FIXED_COMPLETION_TASK,
            instructions=FIXED_COMPLETION_INSTRUCTIONS,
            input=[{"type": "text", "text": canonical_json(normalized_request)}],
            json_schema=_response_json_schema(normalized_request),
            schema_name=RESPONSE_SCHEMA,
            temperature=0.0,
            max_tokens=FIXED_COMPLETION_MAX_TOKENS,
            timeout=FIXED_COMPLETION_TIMEOUT_SECONDS,
            purpose="bounded proposal-only code implementation",
        )
        candidate = getattr(raw, "parsed", None)
        if not isinstance(candidate, Mapping):
            raise CodeImplementationError("structured_completion_not_json")
        response = validate_response(candidate, request=normalized_request)
        audit = getattr(raw, "audit", None)
        route = {
            "provider": getattr(raw, "provider", ""),
            "model": getattr(raw, "model", ""),
            "agent_id": getattr(raw, "agent_id", ""),
            "task": audit.get("task") if isinstance(audit, Mapping) else "",
        }
        completed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        attestation = build_attestation(
            normalized_request,
            response,
            completed_at=completed_at,
            nonce=str(normalized_request["nonce"]),
            route=route,
        )
        return {
            "ok": True,
            "operation": OPERATION,
            "attestation": attestation,
            "response": response,
        }


def _prepare_server_socket_path(path: Path) -> None:
    absolute = path.absolute()
    _validate_socket_path(absolute)
    parent = absolute.parent
    if (
        not parent.is_dir()
        or parent.stat().st_uid != os.geteuid()
        or stat.S_IMODE(parent.stat().st_mode) != 0o700
    ):
        raise CodeImplementationError("socket_parent_permissions_invalid")
    if absolute.exists() or absolute.is_symlink():
        raise CodeImplementationError("socket_path_already_exists")


def resolve_code_implementation_provider(
    runtime: Any,
    *,
    runtime_scope: Mapping[str, Any],
    capability_scope: str,
    checked_at: str,
    implementation_digest_value: str = "",
    probe: bool = False,
) -> dict[str, Any]:
    """Resolve the exact v2 provider facts without selecting a fallback.

    A fresh advertisement is a separate fact from the binding.  The resolver
    therefore checks both against the same assessment timestamp and compares
    the implementation fingerprint before an optional socket health probe.
    """

    checked = _text(checked_at, field="checked_at", maximum=64)
    expected_digest = str(implementation_digest_value or IMPLEMENTATION_DIGEST or "").strip().lower()
    if len(expected_digest) != 64 or any(char not in _HEX64 for char in expected_digest):
        return {"ok": False, "reason": "implementation_digest_unavailable", "provider_ready": False}
    capabilities = getattr(runtime, "capabilities", None)
    if capabilities is None:
        return {"ok": False, "reason": "capability_service_unavailable", "provider_ready": False}
    try:
        resolution = capabilities.resolve(
            CAPABILITY_ID,
            runtime_scope=runtime_scope,
            capability_scope=capability_scope,
            revision_id=REVISION_ID,
            binding_id=BINDING_ID,
            provider_kind=PROVIDER_KIND,
            provider_instance_id=PROVIDER_INSTANCE_ID,
            operation=OPERATION,
            at_time=checked,
        )
        advertisements = capabilities.list_adapter_advertisements(
            runtime_scope=runtime_scope,
            capability_scope=capability_scope,
            binding_id=BINDING_ID,
            provider_kind=PROVIDER_KIND,
            provider_instance_id=PROVIDER_INSTANCE_ID,
            at_time=checked,
            fresh_at=checked,
            limit=4,
        )
    except Exception as exc:
        return {"ok": False, "reason": f"provider_resolution_failed:{type(exc).__name__}", "provider_ready": False}
    if not resolution.ok or len(resolution.bindings) != 1:
        return {"ok": False, "reason": str(resolution.reason or "binding_unavailable"), "provider_ready": False, "resolution": resolution.to_dict()}
    binding = dict(resolution.bindings[0].get("descriptor") or {})
    binding_digest = str(binding.get("implementation_digest") or "").lower()
    if (
        binding_digest != expected_digest
        or binding.get("binding_id") != BINDING_ID
        or binding.get("capability_revision_id") != REVISION_ID
        or binding.get("provider_kind") != PROVIDER_KIND
        or binding.get("provider_instance_id") != PROVIDER_INSTANCE_ID
        or OPERATION not in tuple(binding.get("operations") or ())
    ):
        return {"ok": False, "reason": "implementation_digest_mismatch", "provider_ready": False, "resolution": resolution.to_dict()}
    catalog_activation = _catalog_activation_snapshot(
        capabilities,
        runtime_scope=runtime_scope,
        capability_scope=capability_scope,
    )
    if catalog_activation is None:
        return {
            "ok": False,
            "reason": "catalog_activation_unavailable",
            "provider_ready": False,
            "resolution": resolution.to_dict(),
        }
    matching_ads = []
    for advertisement in advertisements:
        descriptor = dict(advertisement.get("descriptor") or {})
        freshness = dict(advertisement.get("freshness") or {})
        env = dict(descriptor.get("environment_fingerprint") or {})
        if (
            descriptor.get("binding_id") == BINDING_ID
            and descriptor.get("capability_revision_id") == REVISION_ID
            and descriptor.get("provider_kind") == PROVIDER_KIND
            and descriptor.get("provider_instance_id") == PROVIDER_INSTANCE_ID
            and OPERATION in tuple(descriptor.get("operations") or ())
            and env.get("implementation_digest") == expected_digest
            and descriptor.get("side_effect_class") == SIDE_EFFECT_CLASS
            and freshness.get("is_fresh") is True
        ):
            matching_ads.append(advertisement)
    if len(matching_ads) != 1:
        return {"ok": False, "reason": "fresh_advertisement_unavailable", "provider_ready": False, "advertisements": matching_ads}
    advertisement_id = str(matching_ads[0].get("entity_id") or "")
    advertisement_digest = str(matching_ads[0].get("entity_digest") or "").strip().lower()
    if (
        not advertisement_id
        or len(advertisement_digest) != 64
        or any(char not in _HEX64 for char in advertisement_digest)
    ):
        return {
            "ok": False,
            "reason": "advertisement_identity_invalid",
            "provider_ready": False,
        }
    provider = CodeImplementationSocketClient()
    health: dict[str, Any] = {"ok": True, "status": "not_probed"}
    if probe:
        try:
            health = provider.health(nonce=sha256(f"health:{checked}".encode()).hexdigest()[:32])
        except CodeImplementationError as exc:
            return {"ok": False, "reason": "provider_health_unavailable", "provider_ready": False, "health": {"ok": False, "reason": str(exc)}}
        if health.get("ok") is not True:
            return {"ok": False, "reason": "provider_health_failed", "provider_ready": False, "health": health}
    return {
        "ok": True,
        "ready": True,
        "provider_ready": True,
        "provider": provider,
        "resolution": resolution.to_dict(),
        "advertisement": dict(matching_ads[0]),
        "advertisement_id": advertisement_id,
        "advertisement_digest": advertisement_digest,
        "advertisement_fresh": True,
        "catalog_case_id": catalog_activation["catalog_case_id"],
        "catalog_snapshot_digest": catalog_activation["catalog_snapshot_digest"],
        "catalog_activation_state_digest": catalog_activation["activation_state_digest"],
        "implementation_digest": expected_digest,
        "health": health,
        "checked_at": checked,
    }


def _catalog_activation_snapshot(
    capabilities: Any,
    *,
    runtime_scope: Mapping[str, Any],
    capability_scope: str,
) -> dict[str, str] | None:
    """Revalidate the two live catalog passes that activated code v2.

    The generic definition may already be active from the legacy v1 seed on an
    upgrade.  Merely registering an active v2 revision/binding must therefore
    not make it executable: provider resolution requires a later lifecycle
    event carrying the exact sealed-case receipts.
    """

    list_events = getattr(capabilities, "list_lifecycle_events", None)
    if not callable(list_events):
        return None
    try:
        events = list_events(
            entity_type="definition",
            entity_id=CAPABILITY_ID,
            runtime_scope=runtime_scope,
            capability_scope=capability_scope,
            limit=32,
        )
    except Exception:
        return None
    from eimemory.evaluation.hongtu_code_implementation import (
        CATALOG_CASE_ID,
        validate_code_implementation_catalog_receipt,
    )

    for event in reversed(list(events or ())):
        if not isinstance(event, Mapping) or event.get("status") != "active":
            continue
        provenance = event.get("provenance") if isinstance(event.get("provenance"), Mapping) else {}
        passes_value = provenance.get("preflight_passes")
        if isinstance(passes_value, bool):
            continue
        try:
            passes = int(passes_value)
        except (TypeError, ValueError):
            continue
        execution_digests = [str(value or "") for value in provenance.get("preflight_execution_digests") or ()]
        receipt_digests = [str(value or "") for value in provenance.get("provider_evaluation_receipt_digests") or ()]
        receipts = provenance.get("provider_evaluation_receipts")
        if (
            provenance.get("source") != "eimemory.capability_incubation"
            or provenance.get("schema") != "capability.incubation.v1"
            or passes < 2
            or CATALOG_CASE_ID not in {str(value) for value in provenance.get("case_ids") or ()}
            or BINDING_ID not in {str(value) for value in provenance.get("binding_ids") or ()}
            or not isinstance(receipts, list)
            or len(receipts) != passes
            or len(execution_digests) != passes
            or len(receipt_digests) != passes
            or len(set(execution_digests)) != passes
            or len(set(receipt_digests)) != passes
            or any(len(value) != 64 or any(char not in _HEX64 for char in value) for value in execution_digests)
        ):
            continue
        try:
            for receipt, receipt_digest in zip(receipts, receipt_digests, strict=True):
                validate_code_implementation_catalog_receipt(
                    receipt,
                    receipt_digest=receipt_digest,
                )
        except (CodeImplementationError, TypeError, ValueError):
            continue
        activation_state_digest = str(event.get("state_digest") or "").strip().lower()
        if len(activation_state_digest) != 64 or any(char not in _HEX64 for char in activation_state_digest):
            continue
        snapshot = sha256(
            canonical_json(
                {
                    "activation_state_digest": activation_state_digest,
                    "binding_id": BINDING_ID,
                    "case_id": CATALOG_CASE_ID,
                    "execution_digests": execution_digests,
                    "receipt_digests": receipt_digests,
                }
            ).encode("utf-8")
        ).hexdigest()
        return {
            "catalog_case_id": CATALOG_CASE_ID,
            "catalog_snapshot_digest": snapshot,
            "activation_state_digest": activation_state_digest,
        }
    return None


__all__ = [
    "ATTESTATION_SCHEMA",
    "BINDING_ID",
    "CAPABILITY_ID",
    "CodeImplementationError",
    "CodeImplementationSocketClient",
    "CodeImplementationSocketServer",
    "DEFAULT_IMPLEMENTATION_PATHS",
    "DEFAULT_SOCKET_PATH",
    "FIXED_COMPLETION_INSTRUCTIONS",
    "FIXED_COMPLETION_TASK",
    "IMPLEMENTATION_DIGEST",
    "OPERATION",
    "PROVIDER_INSTANCE_ID",
    "REQUEST_SCHEMA",
    "RESPONSE_SCHEMA",
    "REVISION_ID",
    "build_attestation",
    "build_request",
    "canonical_json",
    "implementation_digest",
    "resolve_code_implementation_provider",
    "validate_request",
    "validate_response",
    "validate_attestation",
]
