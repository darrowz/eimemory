"""Private, scoped delegation for data-only recall changes and signed receipts."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import hmac
import json
import os
from pathlib import Path
from uuid import uuid4

from eimemory.governance.learning.effect_dataset import instant, namespace
from eimemory.governance.tool_receipts import receipt_key_set, _read_secure_file

POLICY_SCHEMA = "recall.effect_data_policy.v1"
RECEIPT_SCHEMA = "recall.effect_change_receipt.v1"
DEFAULT_LIMITS = {"daily_changes": 3, "canary_percent": 25, "min_hypothesis_decisions": 10,
                  "min_trial_samples": 20, "trial_days": 7, "max_targets": 64,
                  "weight_step": .15, "min_weight": .5, "threshold_step": .05, "max_threshold": .9}
ACTIONS = ["lower_injection_weight", "raise_injection_threshold"]


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest(value):
    return sha256(canonical(value).encode()).hexdigest()


def namespace_key(channel, scope, source_ids):
    channel, scope, sources = namespace(channel, scope, source_ids)
    if "*" in sources:
        raise ValueError("effect_policy_exact_sources_required")
    return digest({"channel": channel, "scope": scope, "source_ids": sources})


def seal(payload, domain):
    keys = receipt_key_set()
    if keys is None:
        raise ValueError("effect_signing_key_unavailable")
    body = {**payload, "key_id": keys.active_id}
    body["signature"] = hmac.new(keys.active_key.encode(),
        domain.encode() + b"\0" + canonical(body).encode(), sha256).hexdigest()
    return body


def verify(payload, domain):
    keys = receipt_key_set()
    if keys is None or not isinstance(payload, dict):
        return False
    body = dict(payload)
    signature = body.pop("signature", "")
    secret = keys.verification_keys.get(body.get("key_id"))
    if not secret or not isinstance(signature, str):
        return False
    try:
        expected = hmac.new(secret.encode(), domain.encode() + b"\0" + canonical(body).encode(), sha256).hexdigest()
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(expected, signature)


def policy_path(store, channel, scope, source_ids):
    override = os.environ.get("EIMEMORY_REAL_EFFECT_POLICY_FILE", "").strip()
    return Path(override) if override else store.root / "state" / ("real-effect-policy-" + namespace_key(channel, scope, source_ids)[:24] + ".json")


def stop_path(store):
    return store.root / "state" / "real-effect.stop"


def stopped(store):
    path = stop_path(store)
    return path.exists() or path.is_symlink() or os.environ.get("EIMEMORY_REAL_EFFECT_STOP", "0").lower() in {"1", "true", "on"}


def write_private_json(path, value):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("effect_policy_symlink_forbidden")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def validate_limits(limits):
    if not isinstance(limits, dict) or set(limits) != set(DEFAULT_LIMITS):
        raise ValueError("effect_policy_limits_invalid")
    bounds = {"daily_changes": (1, 3), "canary_percent": (1, 25),
              "min_hypothesis_decisions": (10, 1000), "min_trial_samples": (10, 10000), "trial_days": (1, 7)}
    for key, (low, high) in bounds.items():
        if type(limits[key]) is not int or not low <= limits[key] <= high:
            raise ValueError("effect_policy_limits_invalid")
    for key in set(limits) - set(bounds):
        if limits[key] != DEFAULT_LIMITS[key] or isinstance(limits[key], bool):
            raise ValueError("effect_policy_limits_invalid")


def issue_effect_policy(runtime, *, channel="hermes", scope, source_ids=None,
                        days=30, daily_changes=3, canary_percent=25,
                        min_hypothesis_decisions=10, min_trial_samples=20):
    if type(days) is not int or not 1 <= days <= 30:
        raise ValueError("effect_policy_duration_invalid")
    channel, scope, sources = namespace(channel, scope, source_ids)
    namespace_key(channel, scope, sources)  # Reject wildcard grants even with a path override.
    limits = {**DEFAULT_LIMITS, "daily_changes": daily_changes, "canary_percent": canary_percent,
              "min_hypothesis_decisions": min_hypothesis_decisions, "min_trial_samples": min_trial_samples}
    validate_limits(limits)
    now = datetime.now(timezone.utc)
    body = {"schema": POLICY_SCHEMA, "channel": channel, "scope": scope, "source_ids": sources,
            "storage_root_digest": digest(str(runtime.store.root.resolve())),
            "issued_at": now.isoformat(), "expires_at": (now + timedelta(days=days)).isoformat(),
            "actions": ACTIONS, "limits": limits, "code_changes_authorized": False}
    body["policy_id"] = "effect_policy_" + digest(body)[:24]
    policy = seal(body, POLICY_SCHEMA)
    path = policy_path(runtime.store, channel, scope, sources)
    write_private_json(path, policy)
    return {"ok": True, "policy_id": policy["policy_id"], "policy_path": str(path),
            "expires_at": policy["expires_at"], "limits": limits, "code_changes_authorized": False}


def load_effect_policy(store, *, channel, scope, source_ids, now=None, allow_stopped=False, allow_expired=False):
    channel, scope, sources = namespace(channel, scope, source_ids)
    namespace_key(channel, scope, sources)
    if stopped(store) and not allow_stopped:
        raise ValueError("effect_kill_switch_present")
    raw = _read_secure_file(policy_path(store, channel, scope, sources), max_bytes=32768)
    if not raw:
        raise ValueError("effect_policy_unavailable_or_not_private")
    try:
        policy = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ValueError("effect_policy_invalid") from None
    fields = {"schema", "channel", "scope", "source_ids", "storage_root_digest", "issued_at", "expires_at",
              "actions", "limits", "code_changes_authorized", "policy_id", "key_id", "signature"}
    if not isinstance(policy, dict) or set(policy) != fields or policy["schema"] != POLICY_SCHEMA or not verify(policy, POLICY_SCHEMA):
        raise ValueError("effect_policy_signature_invalid")
    if (policy["channel"], policy["scope"], policy["source_ids"]) != (channel, scope, sources) or policy["storage_root_digest"] != digest(str(store.root.resolve())):
        raise ValueError("effect_policy_namespace_mismatch")
    if policy["actions"] != ACTIONS or policy["code_changes_authorized"] is not False:
        raise ValueError("effect_policy_actions_forbidden")
    validate_limits(policy["limits"])
    now = now or datetime.now(timezone.utc)
    if now < instant(policy["issued_at"]) or (now >= instant(policy["expires_at"]) and not allow_expired) or instant(policy["expires_at"]) - instant(policy["issued_at"]) > timedelta(days=30):
        raise ValueError("effect_policy_expired_or_future")
    return policy
