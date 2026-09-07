"""Server-observed explicit recall acceptance, separate from natural proactive gold.

The existing receipt keyring attests the request and actual server result. Normal
record/outbox transactions persist evidence; a rewritten record digest alone
cannot manufacture an observation. No client supplied result is accepted.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
import hmac
import json
import re
from typing import Any

from eimemory.adapters.runtime.channel import base_scope_from_channel, resolve_channel_scope, SUPPORTED_RUNTIME_CHANNELS, RUNTIME_ADAPTER_CONTRACT_VERSION
from eimemory.core.clock import now_iso
from eimemory.governance.evidence_contract import same_scope, verified_deployment_receipt_identity, release_identity_payload
from eimemory.governance.tool_receipts import _receipt_key_set
from eimemory.identity import hongtu_query_scopes, hongtu_query_scopes_with_aliases
from eimemory.models.records import RecordEnvelope, ScopeRef, _compact_record
from eimemory.storage.jsonl import payload_digest
from eimemory.recall.loadout import render_loadout

CAPTURE_SOURCE = "eimemory.production_recall.explicit_capture"
INTENT_SOURCE = "eimemory.production_recall.explicit_intent"
LABEL_SOURCE = "eimemory.production_recall.explicit_label"
REPORT_SOURCE = "eimemory.production_recall.explicit_acceptance"
SCHEMA = "production_recall_explicit_capture.v1"


def _digest(value: Any) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _sign(content: dict) -> dict:
    keys = _receipt_key_set()
    if keys is None:
        raise ValueError("explicit capture attestation key unavailable")
    body = {**deepcopy(content), "key_id": keys.active_id}
    body["signature"] = hmac.new(keys.active_key.encode(), (SCHEMA + ":" + _digest(body)).encode(), sha256).hexdigest()
    return body


def _verify(record: RecordEnvelope, source: str, scope: ScopeRef) -> dict:
    if record.kind != "evaluation_packet" or record.source != source or record.status != "active" or not same_scope(record.scope, scope):
        raise ValueError("explicit capture boundary mismatch")
    body = deepcopy(record.content)
    signature = str(body.pop("signature", ""))
    keys = _receipt_key_set()
    key = keys.verification_keys.get(str(body.get("key_id")), "") if keys else ""
    expected = hmac.new(key.encode(), (SCHEMA + ":" + _digest(body)).encode(), sha256).hexdigest()
    if not key or not hmac.compare_digest(signature, expected):
        raise ValueError("explicit capture signature invalid")
    if body.get("record_id") != record.record_id or body.get("scope") != asdict(scope) or body.get("source") != source:
        raise ValueError("explicit capture signed identity mismatch")
    return record.content


def _record(source: str, record_id: str, scope: ScopeRef, body: dict) -> RecordEnvelope:
    content = _sign({**body, "record_id": record_id, "scope": asdict(scope), "source": source})
    record = RecordEnvelope.create(kind="evaluation_packet", title="Explicit recall observation", summary="Typed explicit recall evidence; not a natural proactive decision.",
                                   content=content, scope=scope, source=source, meta={"report_type": source.rsplit(".", 1)[-1]})
    record.record_id = record_id
    return record


def _insert_once(runtime: Any, record: RecordEnvelope) -> tuple[RecordEnvelope, bool]:
    def mutation(sqlite):
        old = sqlite.get_by_exact_ref(record.record_id, scope=record.scope, source_id=record.source_id)
        if old is not None:
            return (old, False), [], []
        sqlite.upsert(record, commit=False)
        return (record, True), [record], []
    return runtime.store.mutate_records_atomically(mutation)


def _authorized_scopes(scope: ScopeRef) -> list[ScopeRef]:
    # Same default alias/shared visibility used by RetrievalEngine. Explicit
    # MCP calls have no caller-selected task_context or scope aliases.
    scopes = []
    for logical in hongtu_query_scopes_with_aliases(scope):
        for physical in hongtu_query_scopes(logical):
            scopes.extend([physical, ScopeRef(physical.tenant_id, physical.agent_id, physical.workspace_id, "")])
    return scopes


def _reference(runtime: Any, record: RecordEnvelope, scope: ScopeRef) -> dict:
    if not any(same_scope(record.scope, allowed) for allowed in _authorized_scopes(scope)):
        raise ValueError("explicit result read authorization mismatch")
    authoritative = runtime.store.get_by_exact_ref(record.record_id, scope=record.scope, source_id=record.source_id)
    if authoritative is None or authoritative.status != "active" or authoritative.source != record.source:
        raise ValueError("explicit result authority missing")
    return {"record_ref": authoritative.record_id, "scope": asdict(authoritative.scope), "source": authoritative.source,
            "source_id": authoritative.source_id, "record_digest": payload_digest(authoritative.to_dict())}


def observe_explicit_recall(service: Any, *, channel: str, scope: dict, query: str, task_type: str, limit: int, explicit_request: dict) -> dict:
    request = dict(explicit_request)
    if set(request) != {"session_id", "request_id", "acceptance_generated"} or not isinstance(request["acceptance_generated"], bool):
        raise ValueError("explicit request identity invalid")
    for key in ("session_id", "request_id"):
        if not isinstance(request[key], str) or not request[key].strip() or len(request[key]) > 256:
            raise ValueError("explicit request identity invalid")
    runtime = service.runtime
    exact = ScopeRef.from_dict(resolve_channel_scope(channel, scope))
    request.update(query=query, task_type=task_type, limit=limit, channel=channel, scope=asdict(exact))
    identity = {key: request[key] for key in ("session_id", "request_id", "channel", "scope")}
    intent_id, capture_id = "prei_" + _digest(identity)[:32], "prec_" + _digest(identity)[:32]
    intent, inserted = _insert_once(runtime, _record(INTENT_SOURCE, intent_id, exact, {"schema": SCHEMA, "request": request, "created_at": now_iso()}))
    _verify(intent, INTENT_SOURCE, exact)
    if intent.content["request"] != request:
        raise ValueError("explicit request identity conflict")
    if not inserted:
        capture = load_explicit_capture(runtime, capture_id, scope=exact)
        return _public_result(capture)
    references = []
    try:
        result, bundle = service._prefetch_result(channel=channel, scope=scope, query=query, task_type=task_type, limit=limit)
        assembled = service._assemble_recall_bundle(bundle, limit=max(1, min(50, service._positive_limit(limit, 8))))
        expected_result = {"ok": True, "adapter_contract_version": RUNTIME_ADAPTER_CONTRACT_VERSION,
                           "channel": channel, "scope": asdict(exact), "bundle": assembled,
                           "context": render_loadout(assembled, max_chars=service.max_context_chars)}
        if result != expected_result:
            raise ValueError("explicit result rendering mismatch")
        delivered = [*result["bundle"].get("items", []), *result["bundle"].get("persona", []), *result["bundle"].get("rules", []), *result["bundle"].get("reflections", [])]
        full = [*bundle.items, *bundle.rules, *bundle.reflections]
        seen = set()
        for item in delivered:
            matches = [rec for rec in full if rec.record_id == item.get("record_id") and rec.source_id == item.get("source_id")]
            if len(matches) != 1:
                raise ValueError("explicit delivered reference ambiguous")
            reference = _reference(runtime, matches[0], exact)
            authoritative = runtime.store.get_by_exact_ref(reference["record_ref"], scope=ScopeRef.from_dict(reference["scope"]), source_id=reference["source_id"])
            expected_item = _compact_record(authoritative)
            if item != expected_item and item != {**expected_item, "scope": reference["scope"]}:
                # Check the returned bytes before attesting them, not merely
                # the ID. Existing payload verification alone cannot catch a
                # substituted provider summary bearing a genuine record ID.
                raise ValueError("explicit delivered payload mismatch")
            key = _digest(reference)
            if key not in seen:
                references.append(reference)
                seen.add(key)
            item["scope"] = reference["scope"]
        error = ""
    except Exception as exc:
        result = {"ok": False, "channel": channel, "scope": asdict(exact), "error": type(exc).__name__}
        references = []
        error = type(exc).__name__
    release = service._proactive_release(channel, asdict(exact))
    release_reference = _release_reference(runtime, release, channel=channel, scope=exact)
    capture = _record(CAPTURE_SOURCE, capture_id, exact, {
        "schema": SCHEMA, "intent_ref": intent_id, "request": request, "request_digest": _digest(request),
        "invocation_kind": "explicit", "acceptance_generated": request["acceptance_generated"],
        "created_at": now_iso(), "release_identity": release, "release_reference": release_reference,
        "authorization": {"contract": "retrieval.default_alias_shared_visibility.v1", "request_scope": asdict(exact)},
        "result": result, "result_digest": _digest(result), "references": references, "error": error,
    })
    capture, _ = _insert_once(runtime, capture)
    return _public_result(load_explicit_capture(runtime, capture.record_id, scope=exact))


def _release_reference(runtime: Any, release: dict, *, channel: str, scope: ScopeRef) -> dict:
    receipt_id = str(release.get("deployment_receipt_id") or "")
    if not receipt_id:
        return {}  # isolated/unbound invocation, never release-qualified
    for receipt_scope in (scope, ScopeRef.from_dict(base_scope_from_channel(channel, scope))):
        receipt = runtime.store.get_by_id(receipt_id, scope=receipt_scope)
        if receipt is None or not same_scope(receipt.scope, receipt_scope):
            continue
        identity = verified_deployment_receipt_identity(receipt)
        if identity is not None and release_identity_payload(identity) == release:
            return {"record_ref": receipt_id, "scope": asdict(receipt.scope), "source": receipt.source,
                    "source_id": receipt.source_id, "record_digest": payload_digest(receipt.to_dict())}
    raise ValueError("explicit capture release receipt invalid")


def _public_result(capture: RecordEnvelope) -> dict:
    return {**deepcopy(capture.content["result"]), "capture": {"record_id": capture.record_id, "invocation_kind": "explicit",
            "acceptance_generated": capture.content["acceptance_generated"], "result_digest": capture.content["result_digest"]}}


def load_explicit_capture(runtime: Any, record_id: str, *, scope: dict | ScopeRef) -> RecordEnvelope:
    exact = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    record = runtime.store.get_by_exact_ref(record_id, scope=exact, source_id="default")
    if record is None:
        raise ValueError("explicit capture missing or wrong scope")
    body = _verify(record, CAPTURE_SOURCE, exact)
    intent = runtime.store.get_by_exact_ref(body["intent_ref"], scope=exact, source_id="default")
    if intent is None or _verify(intent, INTENT_SOURCE, exact)["request"] != body["request"]:
        raise ValueError("explicit capture intent mismatch")
    if body["request_digest"] != _digest(body["request"]) or body["result_digest"] != _digest(body["result"]):
        raise ValueError("explicit capture digest mismatch")
    if body["release_reference"] != _release_reference(runtime, body["release_identity"], channel=body["request"]["channel"], scope=exact):
        raise ValueError("explicit capture release receipt changed")
    for ref in body["references"]:
        actual = runtime.store.get_by_exact_ref(ref["record_ref"], scope=ScopeRef.from_dict(ref["scope"]), source_id=ref["source_id"])
        if actual is None or _reference(runtime, actual, exact) != ref:
            raise ValueError("explicit capture record changed or unauthorized")
    return record


def collect_explicit_queries(runtime: Any, *, scope: dict | ScopeRef, limit: int = 500) -> dict:
    base = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    valid, rejected = [], {}
    for channel in sorted(SUPPORTED_RUNTIME_CHANNELS):
        exact = ScopeRef.from_dict(resolve_channel_scope(channel, base))
        for record in runtime.store.list_records(kinds=["evaluation_packet"], scope=exact, limit=max(1, min(limit, 500))):
            if record.source != CAPTURE_SOURCE or not same_scope(record.scope, exact):
                continue
            try:
                load_explicit_capture(runtime, record.record_id, scope=exact)
                valid.append(record.record_id)
            except ValueError as exc:
                rejected[record.record_id] = str(exc)
    return {"ok": not rejected, "capture_record_ids": sorted(set(valid)), "rejected": rejected,
            "natural_benchmark_eligible": False, "reason": "explicit_acceptance_separate_from_proactive"}


def accept_explicit_query(runtime: Any, *, capture_record_id: str, operator_scope: dict | ScopeRef, labels: list[dict], packet_evidence: dict) -> dict:
    base = operator_scope if isinstance(operator_scope, ScopeRef) else ScopeRef.from_dict(operator_scope)
    # Exact identity is resolved within operator-authorized channels only.
    capture = None
    for channel in sorted(SUPPORTED_RUNTIME_CHANNELS):
        exact = ScopeRef.from_dict(resolve_channel_scope(channel, base))
        candidate = runtime.store.get_by_exact_ref(capture_record_id, scope=exact, source_id="default")
        if candidate is not None:
            capture = load_explicit_capture(runtime, capture_record_id, scope=exact)
            break
    if capture is None or not capture.content["result"].get("ok"):
        raise ValueError("successful operator-authorized explicit capture required")
    if packet_evidence.get("schema") != "secure_dataset_fingerprint.v1" or re.fullmatch(r"[0-9a-f]{64}", str(packet_evidence.get("digest", ""))) is None or any(type(packet_evidence.get(k)) is not int or packet_evidence[k] <= 0 for k in ("size", "device", "inode")):
        raise ValueError("secure operator label packet required")
    if not labels or len(labels) > 16:
        raise ValueError("explicit labels required")
    normalized = []
    for label in labels:
        if type(label.get("grade")) is not int or not 1 <= label["grade"] <= 3:
            raise ValueError("explicit label grade invalid")
        physical = ScopeRef.from_dict(label.get("scope") or {})
        record = runtime.store.get_by_exact_ref(str(label.get("record_ref", "")), scope=physical, source_id=str(label.get("source_id", "")))
        if record is None:
            raise ValueError("explicit label source or scope mismatch")
        reference = _reference(runtime, record, capture.scope)
        # Gold can be a missed answer, but must remain ordinary durable memory,
        # never raw evidence/diagnostics or a label packet laundered as memory.
        if record.kind not in {"memory", "rule", "knowledge_page", "knowledge_unit"} or runtime.memory._online_recall_pollution_reason(record):
            raise ValueError("explicit label is not durable recall gold")
        if not runtime.memory._is_returnable_memory_record(record) or runtime.memory._is_internal_audit_record(record):
            raise ValueError("explicit label is not durable recall gold")
        normalized.append({**reference, "grade": label["grade"]})
    if len({_digest({k: v for k, v in ref.items() if k != "grade"}) for ref in normalized}) != len(normalized):
        raise ValueError("duplicate explicit label")
    body = {"capture_ref": capture.record_id, "capture_digest": _digest(capture.content), "labels": normalized,
            "operator_scope": asdict(base), "operator_packet_evidence": packet_evidence, "labeler": "operator",
            "invocation_kind": "explicit", "acceptance_generated": capture.content["acceptance_generated"]}
    record_id = "prel_" + _digest(body)[:32]
    record, _ = _insert_once(runtime, _record(LABEL_SOURCE, record_id, capture.scope, body))
    _verify(record, LABEL_SOURCE, capture.scope)
    return {"ok": True, "record_id": record_id, "capture_ref": capture.record_id, "natural_benchmark_eligible": False}


def evaluate_explicit_queries(runtime: Any, *, scope: dict | ScopeRef, label_record_ids: list[str], persist: bool = False) -> dict:
    base = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    samples, seen = [], set()
    for record_id in label_record_ids:
        found = None
        for channel in sorted(SUPPORTED_RUNTIME_CHANNELS):
            exact = ScopeRef.from_dict(resolve_channel_scope(channel, base))
            found = runtime.store.get_by_exact_ref(record_id, scope=exact, source_id="default")
            if found is not None:
                break
        if found is None:
            raise ValueError("explicit label not authorized")
        body = _verify(found, LABEL_SOURCE, exact)
        capture = load_explicit_capture(runtime, body["capture_ref"], scope=exact)
        if body["capture_digest"] != _digest(capture.content):
            raise ValueError("explicit label capture mismatch")
        if capture.record_id in seen:
            raise ValueError("duplicate explicit event")
        seen.add(capture.record_id)
        for label in body["labels"]:
            gold = runtime.store.get_by_exact_ref(label["record_ref"], scope=ScopeRef.from_dict(label["scope"]), source_id=label["source_id"])
            if gold is None or _reference(runtime, gold, exact) != {k: v for k, v in label.items() if k != "grade"}:
                raise ValueError("explicit gold changed or unauthorized")
        returned = capture.content["result"]["bundle"]["items"]
        hits = [i + 1 for i, item in enumerate(returned) if any(item.get("record_id") == gold["record_ref"] and item.get("scope") == gold["scope"] and item.get("source_id") == gold["source_id"] for gold in body["labels"])]
        samples.append({"capture_ref": capture.record_id, "label_ref": record_id, "request": capture.content["request"],
                        "release_identity": capture.content["release_identity"], "acceptance_generated": capture.content["acceptance_generated"],
                        "hit": bool(hits), "reciprocal_rank": 1 / min(hits) if hits else 0, "gold": body["labels"], "result_digest": capture.content["result_digest"]})
    report = {"schema": "production_recall_explicit_acceptance.v1", "invocation_kind": "explicit", "sample_count": len(samples), "samples": samples,
              "release_bound_sample_count": sum(bool(s["release_identity"].get("deployment_receipt_id")) for s in samples),
              "hit_rate": sum(s["hit"] for s in samples) / len(samples) if samples else 0,
              "mrr": sum(s["reciprocal_rank"] for s in samples) / len(samples) if samples else 0,
              "ok": bool(samples) and all(s["hit"] for s in samples), "natural_benchmark_eligible": False,
              "gate_status": "acceptance_only", "reason": "explicit observations do not establish natural proactive or strict release inheritance"}
    if persist:
        record_id = "prea_" + _digest(report)[:32]
        stored, _ = _insert_once(runtime, _record(REPORT_SOURCE, record_id, base, report))
        report["persisted_record_id"] = stored.record_id
    return report
