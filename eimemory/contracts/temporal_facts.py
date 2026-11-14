"""Opt-in structured temporal evidence contract; never inferred from legacy time.

TemporalFactAccess is server configuration at a trusted Python boundary. It is
not a wire/model payload or a replacement for authenticating a future adapter.
All supplied links are rejected: the protocol generates only validated edges.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from typing import Callable

from eimemory.core.record_ids import validate_record_id
from eimemory.metadata import business_metadata
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.models.source_partitions import normalize_source_id


SCHEMA = "temporal_fact.v1"
MAX_EVENTS = 512
MAX_BATCH = 32
MAX_EVIDENCE = 16
_INSTANT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})\Z")


def canonical_json(value) -> str:
    """Only actual JSON types; no coercion of identifiers or nonfinite numbers."""
    def check(item):
        if item is None or type(item) in (str, bool, int, float):
            return
        if type(item) is list:
            for member in item:
                check(member)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for member in item.values():
                check(member)
            return
        raise ValueError("invalid_json_value")
    check(value)
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _text(value, label):
    if type(value) is not str or not value or value != value.strip() or len(value) > 512:
        raise ValueError(label + "_invalid")
    return value


@dataclass(frozen=True)
class TemporalFactAccess:
    scope: ScopeRef
    source_id: str
    authority: str
    source: str
    admit_evidence: Callable[[RecordEnvelope], bool]

    def __post_init__(self):
        if type(self.scope) is not ScopeRef:
            raise ValueError("binding_scope_invalid")
        for key, value in asdict(self.scope).items():
            if type(value) is not str or value != value.strip():
                raise ValueError("binding_scope_invalid")
            if key == "tenant_id" and not value:
                raise ValueError("binding_scope_invalid")
        for key in ("source_id", "authority", "source"):
            _text(getattr(self, key), "binding_" + key)
        # Identity hashes, persisted envelopes and exact queries must use the
        # same literal. Legacy envelopes normalize; this protocol rejects drift.
        if self.source_id != normalize_source_id(self.source_id):
            raise ValueError("binding_source_id_noncanonical")
        if not callable(self.admit_evidence):
            raise ValueError("binding_evidence_authorizer_required")
        # Detach the caller's mutable ScopeRef.
        object.__setattr__(self, "scope", ScopeRef(**asdict(self.scope)))


def access_partition(access: TemporalFactAccess) -> dict:
    return dict(scope=asdict(access.scope), source_id=access.source_id,
                authority=access.authority, source=access.source)


def revision_id_for(access: TemporalFactAccess, idempotency_key: str) -> str:
    _text(idempotency_key, "idempotency_key")
    return "mem_tf_" + digest([SCHEMA, access_partition(access), idempotency_key])


def normalize_fact(fact: dict) -> dict:
    if type(fact) is not dict or set(fact) != {"namespace", "entity_id", "attribute", "cardinality", "applicability"}:
        raise ValueError("fact_fields_invalid")
    for key in ("namespace", "entity_id", "attribute"):
        if fact[key] is not None:
            _text(fact[key], key)
    if fact["cardinality"] not in (None, "single", "multi"):
        raise ValueError("cardinality_invalid")
    applicability = fact["applicability"]
    if applicability is not None and type(applicability) is not dict:
        raise ValueError("applicability_invalid")
    encoded = canonical_json(fact)
    if len(encoded.encode("utf-8")) > 4096:
        raise ValueError("fact_too_large")
    return json.loads(encoded)


def family_id_for(access: TemporalFactAccess, fact: dict) -> str:
    identity = normalize_fact(fact)
    # Cardinality is a declaration to validate within the family, not a way
    # to create a second, incompatible family for the same exact attribute.
    if identity["cardinality"] is not None:
        identity.pop("cardinality")
    return "tf_" + digest([SCHEMA, access_partition(access), identity])


def parse_instant(value: str) -> datetime:
    # Phase 1 supports explicit offset timestamps at second precision only.
    # Date-only, naive, fractional, leap-second and ambiguous inputs fail closed.
    if type(value) is not str or _INSTANT.fullmatch(value) is None:
        raise ValueError("timezone_or_precision_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if value[-6:-5] in ("+", "-") and (int(value[-5:-3]) > 23 or int(value[-2:]) > 59):
            raise ValueError("timezone_invalid")
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise ValueError("instant_invalid") from exc


def normalize_interval(interval: dict) -> dict:
    if type(interval) is not dict or set(interval) != {"valid_from", "valid_until", "until_kind", "precision"}:
        raise ValueError("interval_fields_invalid")
    if interval["precision"] != "second":
        raise ValueError("precision_unsupported")
    kind = interval["until_kind"]
    if kind not in ("bounded", "unknown", "unbounded"):
        raise ValueError("until_kind_invalid")
    start, until = interval["valid_from"], interval["valid_until"]
    start_dt = parse_instant(start) if start is not None else None
    until_dt = parse_instant(until) if until is not None else None
    if (kind == "bounded") != (until_dt is not None):
        raise ValueError("until_kind_mismatch")
    if start_dt is not None and until_dt is not None and start_dt >= until_dt:
        raise ValueError("interval_empty_or_reversed")
    return dict(valid_from=start_dt.isoformat() if start_dt else None,
                valid_until=until_dt.isoformat() if until_dt else None,
                until_kind=kind, precision="second")


def is_resolved(fact: dict, interval: dict) -> bool:
    return all(value is not None for value in fact.values()) and interval["valid_from"] is not None and interval["until_kind"] != "unknown"


def covers(interval: dict, at: datetime) -> bool:
    if interval["valid_from"] is None or interval["until_kind"] == "unknown":
        return False
    return parse_instant(interval["valid_from"]) <= at and (
        interval["until_kind"] == "unbounded" or at < parse_instant(interval["valid_until"]))


def normalize_operation(operation: dict) -> dict:
    fields = {"operation", "fact", "value", "interval", "targets", "expected_revision", "evidence", "idempotency_key"}
    if type(operation) is not dict or set(operation) != fields:
        raise ValueError("operation_fields_invalid")
    # Copy and reject unsupported JSON types before touching authoritative data.
    encoded = canonical_json(operation)
    if len(encoded.encode("utf-8")) > 16384:
        raise ValueError("operation_too_large")
    result = json.loads(encoded)
    result["fact"] = normalize_fact(result["fact"])
    result["interval"] = normalize_interval(result["interval"])
    kind = result["operation"]
    if kind not in ("claim", "transition", "correction", "retraction"):
        raise ValueError("operation_invalid")
    _text(result["idempotency_key"], "idempotency_key")
    if result["expected_revision"] is not None:
        validate_record_id(_text(result["expected_revision"], "expected_revision"))
    for field, maximum in (("targets", MAX_EVENTS), ("evidence", MAX_EVIDENCE)):
        ids = result[field]
        if (type(ids) is not list or len(ids) > maximum or any(type(item) is not str for item in ids)
                or len(set(ids)) != len(ids)):
            raise ValueError(field + "_invalid")
        for item in ids:
            validate_record_id(_text(item, field))
    if not result["evidence"]:
        raise ValueError("source_evidence_required")
    if (kind == "claim") != (not result["targets"]):
        raise ValueError("operation_targets_invalid")
    if kind == "retraction" and result["value"] is not None:
        raise ValueError("retraction_value_invalid")
    if kind != "claim" and not is_resolved(result["fact"], result["interval"]):
        raise ValueError("unresolved_operation")
    return result


def temporal_payload(record: RecordEnvelope) -> dict:
    living = business_metadata(record.meta).get("living_memory_v1")
    temporal = living.get("temporal") if isinstance(living, dict) else None
    return temporal if isinstance(temporal, dict) else {}


def validate_target_interval(operation: dict, target: dict):
    if not is_resolved(target["fact"], target["interval"]):
        raise ValueError("unresolved_operation")
    if target["operation"] == "retraction":
        raise ValueError("retraction_target_invalid")
    interval, old = operation["interval"], target["interval"]
    start, old_start = parse_instant(interval["valid_from"]), parse_instant(old["valid_from"])
    if operation["operation"] == "transition" and start <= old_start:
        raise ValueError("transition_must_advance")
    if (old["until_kind"] == "bounded" and start >= parse_instant(old["valid_until"])) or (
        interval["until_kind"] == "bounded" and parse_instant(interval["valid_until"]) <= old_start
    ):
        raise ValueError("target_interval_disjoint")


def reject_batch_cycles(operations: list[dict], access: TemporalFactAccess):
    graph = {revision_id_for(access, op["idempotency_key"]): op["targets"] for op in operations}
    visiting, visited = set(), set()
    def visit(node):
        if node in visiting:
            raise ValueError("target_cycle")
        if node in visited:
            return
        visiting.add(node)
        for target in graph.get(node, []):
            visit(target)
        visiting.remove(node)
        visited.add(node)
    for node in graph:
        visit(node)


def resolve_family(records: list[RecordEnvelope], *, at: datetime, limit: int) -> dict:
    """Resolve only a certified complete family. Masks persist after retraction.

    Each operation masks its named assertions at the affected instant. A later
    retraction can remove that operation's assertion but never erase its mask;
    reverting a business value therefore requires a new explicit assertion.
    """
    masked, candidates, unresolved = set(), [], []
    for record in records:
        payload = temporal_payload(record)
        request = payload["request"]
        if not is_resolved(request["fact"], request["interval"]):
            unresolved.append(record.record_id)
            continue
        if covers(request["interval"], at):
            masked.update(request["targets"])
            if request["operation"] != "retraction":
                candidates.append(record)
    selected = [record for record in candidates if record.record_id not in masked]
    distinct = {canonical_json(temporal_payload(record)["request"]["value"]) for record in selected}
    cardinality = temporal_payload(records[0])["request"]["fact"]["cardinality"] if records else None
    status = "unresolved" if unresolved else (
        "conflict" if cardinality == "single" and len(distinct) > 1 else "resolved" if selected else "absent")
    return dict(status=status, items=[dict(revision_id=record.record_id,
                value=temporal_payload(record)["request"]["value"], evidence=list(record.evidence),
                interval=temporal_payload(record)["request"]["interval"]) for record in selected[:limit]],
                unresolved=unresolved, head_revision=records[-1].record_id if records else None,
                event_count=len(records), selected_count=len(selected), truncated=len(selected) > limit)
