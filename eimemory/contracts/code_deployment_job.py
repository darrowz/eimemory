"""Data-only contract for the fixed, independently managed deploy worker."""
from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json
import re
from collections.abc import Mapping

SCHEMA = "eimemory.code_deployment_job.v1"
KEYS = frozenset({"schema", "operation", "transaction_id", "scope", "base_commit",
    "candidate_commit", "target_commit", "controller_commit", "installer_digest",
    "policy_digest", "authorization_digest", "patch_digest", "candidate_tree_digest",
    "verification_receipt_digests", "observation_deadline"})
SCOPE_KEYS = frozenset({"tenant_id", "agent_id", "workspace_id", "user_id"})

def canonical_bytes(value: Mapping) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")

def validate_request(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != KEYS or value.get("schema") != SCHEMA:
        raise ValueError("deployment_job_fields_invalid")
    if value["operation"] not in {"deploy", "rollback"}:
        raise ValueError("deployment_job_operation_invalid")
    if not isinstance(value["transaction_id"], str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,160}", value["transaction_id"]):
        raise ValueError("deployment_job_transaction_invalid")
    scope = value["scope"]
    if not isinstance(scope, dict) or set(scope) != SCOPE_KEYS or any(
        not isinstance(v, str) or not v.strip() or v != v.strip() or len(v) > 256 for v in scope.values()
    ):
        raise ValueError("deployment_job_scope_invalid")
    for key in ("base_commit", "candidate_commit", "target_commit", "controller_commit"):
        if not isinstance(value[key], str) or not re.fullmatch(r"[0-9a-f]{40}", value[key]):
            raise ValueError("deployment_job_commit_invalid")
    target = value["candidate_commit"] if value["operation"] == "deploy" else value["base_commit"]
    if value["target_commit"] != target:
        raise ValueError("deployment_job_target_invalid")
    for key in ("installer_digest", "policy_digest", "authorization_digest", "patch_digest", "candidate_tree_digest"):
        if not isinstance(value[key], str) or not re.fullmatch(r"[0-9a-f]{64}", value[key]):
            raise ValueError("deployment_job_digest_invalid")
    receipts = value["verification_receipt_digests"]
    if (not isinstance(receipts, list) or len(receipts) != 3
        or any(not isinstance(x, str) or not re.fullmatch(r"[0-9a-f]{64}", x) for x in receipts)
        or receipts != sorted(set(receipts))):
        raise ValueError("deployment_job_receipts_invalid")
    try:
        deadline = datetime.fromisoformat(value["observation_deadline"].replace("Z", "+00:00"))
        if deadline.utcoffset() is None:
            raise ValueError()
    except (TypeError, ValueError, AttributeError):
        raise ValueError("deployment_job_deadline_invalid") from None
    if len(canonical_bytes(value)) > 65536:
        raise ValueError("deployment_job_too_large")
    return dict(value)

def request_key(value: object) -> str:
    return sha256(canonical_bytes(validate_request(value))).hexdigest()
