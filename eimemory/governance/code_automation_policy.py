"""Trusted machine policy for autonomous code evolution.

The policy is intentionally read only from a deployment-controlled environment
variable.  Proposer, incident, candidate, and patch payloads are data and are
never consulted as policy authority.  Returned values are bounded diagnostics
only: the raw environment JSON is never persisted or returned.
"""

from __future__ import annotations

import json
import os
import re
from hashlib import sha256
from typing import Any, Mapping

from eimemory.governance.code_patch_command_policy import AUTOMATION_POLICY_ACTIONS


CODE_AUTOMATION_POLICY_ENV = "EIMEMORY_CODE_AUTOMATION_POLICY_JSON"
CODE_AUTOMATION_POLICY_SCHEMA_VERSION = "code_automation_policy.v1"
CODE_AUTOMATION_POLICY_SOURCE = "machine_environment"
MAX_POLICY_JSON_CHARS = 16_384
MAX_POLICY_IDENTIFIER_CHARS = 160
MAX_POLICY_COORDINATE_CHARS = 512
MAX_POLICY_CONSTRAINT_VALUES = 128

_POLICY_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_CONSTRAINT_KEYS = {
    "profile_keys": "profile_key",
    "capability_ids": "capability_id",
    "capability_revision_ids": "capability_revision_id",
    "capability_scopes": "capability_scope",
    "provider_binding_ids": "provider_binding_id",
}


def load_code_automation_policy(
    *,
    profile_key: str = "",
    capability_id: str = "",
    capability_revision_id: str = "",
    capability_scope: str = "",
    provider_binding_id: str = "",
) -> dict[str, Any]:
    """Load the sole authority for automatic code side effects.

    The returned envelope is safe for proposal, transaction, and diagnostic
    records.  It contains no raw configuration or unknown fields.  A missing,
    malformed or nonmatching policy is a direct machine
    block rather than a deferred or operator-mediated state.
    """
    context = _policy_context(
        profile_key=profile_key,
        capability_id=capability_id,
        capability_revision_id=capability_revision_id,
        capability_scope=capability_scope,
        provider_binding_id=provider_binding_id,
    )
    raw_text = str(os.environ.get(CODE_AUTOMATION_POLICY_ENV) or "")
    if not raw_text.strip():
        return _blocked_policy("machine_policy_environment_missing", context=context)
    if len(raw_text) > MAX_POLICY_JSON_CHARS:
        return _blocked_policy("machine_policy_environment_too_large", context=context)
    try:
        raw = json.loads(raw_text, object_pairs_hook=_strict_json_object)
    except (json.JSONDecodeError, ValueError):
        return _blocked_policy("machine_policy_environment_invalid_json", context=context)
    if not isinstance(raw, dict):
        return _blocked_policy("machine_policy_environment_not_object", context=context)
    return _validated_policy(raw, context=context)


def machine_policy_context_from_mapping(value: Mapping[str, Any] | None) -> dict[str, str]:
    """Select only stable capability coordinates from untrusted request data.

    This helper does not grant authority.  It merely supplies matching inputs
    for the independent environment policy loader above.
    """
    raw = value if isinstance(value, Mapping) else {}
    hypothesis = raw.get("capability_hypothesis")
    hypothesis = hypothesis if isinstance(hypothesis, Mapping) else {}
    return _policy_context(
        profile_key=_text(raw.get("profile_key")),
        capability_id=_text(raw.get("target_capability") or raw.get("capability_id") or hypothesis.get("capability_id")),
        capability_revision_id=_text(raw.get("capability_revision_id") or hypothesis.get("capability_revision_id")),
        capability_scope=_text(raw.get("capability_scope") or hypothesis.get("capability_scope")),
        provider_binding_id=_text(raw.get("provider_binding_id") or hypothesis.get("provider_binding_id")),
    )


def code_automation_policy_summary(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return the fixed safe subset permitted in proposal and ledger records."""
    raw = value if isinstance(value, Mapping) else {}
    raw_actions = raw.get("actions") if isinstance(raw.get("actions"), Mapping) else {}
    actions = {
        action: raw_actions.get(action) is True
        for action in AUTOMATION_POLICY_ACTIONS
    }
    policy_id = _text(raw.get("policy_id"), limit=MAX_POLICY_IDENTIFIER_CHARS)
    digest = str(raw.get("policy_digest") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        digest = ""
    declared = raw.get("declared") is True and bool(policy_id)
    schema_version = (
        CODE_AUTOMATION_POLICY_SCHEMA_VERSION
        if str(raw.get("schema_version") or "") == CODE_AUTOMATION_POLICY_SCHEMA_VERSION
        else ""
    )
    source = (
        CODE_AUTOMATION_POLICY_SOURCE
        if str(raw.get("source") or "") == CODE_AUTOMATION_POLICY_SOURCE
        else ""
    )
    # A summary is often passed through untrusted proposal-shaped payloads.
    # Never let an ``ok: true`` field from such data become authority merely
    # because it resembles a policy receipt.  Only a complete receipt emitted
    # by the environment loader can be enabled; callers that need to apply a
    # change may safely gate on this compact object alone.
    trusted_enabled = (
        raw.get("ok") is True
        and str(raw.get("status") or "") == "enabled"
        and declared
        and bool(schema_version)
        and bool(source)
        and bool(digest)
    )
    reason = _text(raw.get("reason"), limit=160)
    if raw.get("ok") is True and not trusted_enabled and not reason:
        reason = "machine_policy_summary_untrusted"
    return {
        "ok": trusted_enabled,
        "status": "enabled" if trusted_enabled else "blocked",
        "reason": reason,
        "declared": declared,
        "schema_version": schema_version,
        "source": source,
        "policy_id": policy_id,
        "policy_digest": digest,
        "actions": actions,
    }


def _validated_policy(raw: dict[str, Any], *, context: dict[str, str]) -> dict[str, Any]:
    unknown = set(raw) - {"schema_version", "policy_id", "actions", "constraints"}
    if unknown:
        return _blocked_policy("machine_policy_fields_unknown", context=context)
    if str(raw.get("schema_version") or "") != CODE_AUTOMATION_POLICY_SCHEMA_VERSION:
        return _blocked_policy("machine_policy_schema_invalid", context=context)
    if not isinstance(raw.get("policy_id"), str):
        return _blocked_policy("machine_policy_id_invalid", context=context)
    policy_id = _text(raw.get("policy_id"), limit=MAX_POLICY_IDENTIFIER_CHARS)
    if not _POLICY_ID_PATTERN.fullmatch(policy_id):
        return _blocked_policy("machine_policy_id_invalid", context=context)
    actions, action_error = _strict_actions(raw.get("actions"))
    if action_error:
        return _blocked_policy(action_error, context=context, policy_id=policy_id)
    constraints, constraint_error = _strict_constraints(raw.get("constraints"))
    if constraint_error:
        return _blocked_policy(constraint_error, context=context, policy_id=policy_id, actions=actions)
    digest = _policy_digest(raw)
    mismatch_reason = _constraint_mismatch_reason(constraints, context)
    if mismatch_reason:
        return _blocked_policy(
            mismatch_reason,
            context=context,
            policy_id=policy_id,
            policy_digest=digest,
            actions=actions,
            constraints=constraints,
        )
    if not actions["local_apply"]:
        return _blocked_policy(
            "machine_policy_local_apply_not_enabled",
            context=context,
            policy_id=policy_id,
            policy_digest=digest,
            actions=actions,
            constraints=constraints,
        )
    return {
        "ok": True,
        "status": "enabled",
        "reason": "",
        "declared": True,
        "schema_version": CODE_AUTOMATION_POLICY_SCHEMA_VERSION,
        "source": CODE_AUTOMATION_POLICY_SOURCE,
        "policy_id": policy_id,
        "policy_digest": digest,
        "actions": actions,
        "constraints": constraints,
        "context": context,
    }


def _blocked_policy(
    reason: str,
    *,
    context: dict[str, str],
    policy_id: str = "",
    policy_digest: str = "",
    actions: dict[str, bool] | None = None,
    constraints: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    return {
        "ok": False,
        "status": "blocked",
        "reason": str(reason),
        "declared": bool(policy_id),
        "schema_version": CODE_AUTOMATION_POLICY_SCHEMA_VERSION,
        "source": CODE_AUTOMATION_POLICY_SOURCE,
        "policy_id": str(policy_id),
        "policy_digest": str(policy_digest),
        "actions": dict(actions or {action: False for action in AUTOMATION_POLICY_ACTIONS}),
        "constraints": dict(constraints or {}),
        "context": context,
    }


def _strict_actions(value: Any) -> tuple[dict[str, bool], str]:
    if not isinstance(value, dict):
        return {}, "machine_policy_actions_missing"
    names = {str(key) for key in value}
    expected = set(AUTOMATION_POLICY_ACTIONS)
    if names - expected:
        return {}, "machine_policy_actions_unknown"
    if names != expected:
        return {}, "machine_policy_actions_incomplete"
    actions: dict[str, bool] = {}
    for name in AUTOMATION_POLICY_ACTIONS:
        action = value.get(name)
        if not isinstance(action, bool):
            return {}, "machine_policy_actions_invalid"
        actions[name] = action
    return actions, ""


def _strict_constraints(value: Any) -> tuple[dict[str, list[str]], str]:
    if value is None:
        return {}, ""
    if not isinstance(value, dict):
        return {}, "machine_policy_constraints_invalid"
    unknown = {str(key) for key in value} - set(_CONSTRAINT_KEYS)
    if unknown:
        return {}, "machine_policy_constraints_unknown"
    constraints: dict[str, list[str]] = {}
    for key in _CONSTRAINT_KEYS:
        if key not in value:
            continue
        raw_values = value.get(key)
        if isinstance(raw_values, (str, bytes)) or not isinstance(raw_values, (list, tuple)):
            return {}, "machine_policy_constraints_invalid"
        if any(not isinstance(item, str) for item in raw_values):
            return {}, "machine_policy_constraints_invalid"
        normalized = [_coordinate(item) for item in raw_values]
        if not normalized or len(normalized) > MAX_POLICY_CONSTRAINT_VALUES:
            return {}, "machine_policy_constraints_invalid"
        if any(not item for item in normalized) or len(set(normalized)) != len(normalized):
            return {}, "machine_policy_constraints_invalid"
        constraints[key] = sorted(normalized)
    return constraints, ""


def _constraint_mismatch_reason(constraints: dict[str, list[str]], context: dict[str, str]) -> str:
    for key, context_key in _CONSTRAINT_KEYS.items():
        allowed = constraints.get(key)
        if not allowed:
            continue
        value = context.get(context_key) or ""
        if not value:
            return f"machine_policy_{context_key}_missing"
        if value not in allowed:
            return f"machine_policy_{context_key}_not_allowed"
    return ""


def _policy_context(
    *,
    profile_key: str,
    capability_id: str,
    capability_revision_id: str,
    capability_scope: str,
    provider_binding_id: str,
) -> dict[str, str]:
    return {
        "profile_key": _coordinate(profile_key),
        "capability_id": _coordinate(capability_id),
        "capability_revision_id": _coordinate(capability_revision_id),
        "capability_scope": _coordinate(capability_scope),
        "provider_binding_id": _coordinate(provider_binding_id),
    }


def _policy_digest(raw: dict[str, Any]) -> str:
    encoded = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


def _strict_json_object(pairs: list[tuple[Any, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON keys instead of accepting a parser overwrite."""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if not isinstance(key, str) or key in result:
            raise ValueError("duplicate_or_nontext_policy_key")
        result[key] = value
    return result


def _text(value: Any, *, limit: int = MAX_POLICY_IDENTIFIER_CHARS) -> str:
    return " ".join(str(value or "").split())[:limit]


def _coordinate(value: Any) -> str:
    """Keep policy matching exact: overlong coordinates never truncate-match."""

    text = " ".join(str(value or "").split())
    return text if len(text) <= MAX_POLICY_COORDINATE_CHARS else ""


__all__ = [
    "CODE_AUTOMATION_POLICY_ENV",
    "CODE_AUTOMATION_POLICY_SCHEMA_VERSION",
    "CODE_AUTOMATION_POLICY_SOURCE",
    "code_automation_policy_summary",
    "load_code_automation_policy",
    "machine_policy_context_from_mapping",
]
