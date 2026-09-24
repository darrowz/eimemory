"""Pure final-output/receipt binding. Diagnostic nesting is not authority.

These checks bind a trusted runtime's verifier verdict to its final output.
They do not re-verify a quote against a truncated compact record and must not
be used to authenticate arbitrary caller-provided tool output.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
import re
from typing import Any

_SUPPORTED = frozenset({"evidence_found", "degraded"})
_HEX64 = re.compile(r"[0-9a-f]{64}")


def _get(record: Any, name: str):
    return record.get(name) if isinstance(record, Mapping) else getattr(record, name, None)


def _record_id(record: Any) -> str:
    value = _get(record, "record_id")
    return value if isinstance(value, str) and value.strip() and len(value) <= 128 else ""


def valid_proof(proof: Any) -> bool:
    if not isinstance(proof, Mapping) or not _record_id(proof):
        return False
    digest = proof.get("quote_digest")
    start, end = proof.get("span_start"), proof.get("span_end")
    return (isinstance(digest, str) and _HEX64.fullmatch(digest) is not None
            and type(start) is int and type(end) is int and 0 <= start < end <= 16_000)


def bind_selected_proofs(proofs: Any, records: Sequence[Any]) -> list[dict]:
    if not isinstance(proofs, list) or len(proofs) > 8:
        return []
    record_counts = Counter(_record_id(record) for record in records)
    proof_counts = Counter(_record_id(proof) for proof in proofs if isinstance(proof, Mapping))
    return [dict(proof) for proof in proofs if valid_proof(proof)
            and record_counts[_record_id(proof)] == 1 and proof_counts[_record_id(proof)] == 1
            and _record_id(proof)]


def invalidate_empty_selection(state: Mapping[str, Any], *, selected_count: int) -> dict:
    result = dict(state)
    caller = result.get("caller_assistance")
    if selected_count or not isinstance(caller, Mapping):
        return result
    caller = dict(caller)
    caller.pop("proofs", None)
    caller.pop("independent_scored", None)
    unavailable = result.get("status") != "no_evidence"
    caller.update(status="unavailable" if unavailable else "no_evidence",
                  outcome="unavailable" if unavailable else "no_support")
    if unavailable:
        caller["reason"] = "final_selection_unavailable"
    result["caller_assistance"] = caller
    result.pop("scored", None)
    return result


def bind_final_selection(state: Mapping[str, Any], records: Sequence[Any]) -> dict:
    result = dict(state)
    caller = result.get("caller_assistance")
    if not isinstance(caller, Mapping):
        return result
    caller = dict(caller)
    if caller.get("status") != "evidence_found" or caller.get("outcome") != "supported":
        caller.pop("proofs", None)
        caller.pop("independent_scored", None)
    else:
        proofs = bind_selected_proofs(caller.get("proofs"), records)
        if proofs:
            caller["proofs"] = proofs
        else:
            caller.pop("proofs", None)
            caller.pop("independent_scored", None)
            caller.update(status="unavailable", outcome="unavailable",
                          reason="final_selection_unavailable")
    result["caller_assistance"] = caller
    return invalidate_empty_selection(result, selected_count=len(records))


def _final_records(payload: Mapping[str, Any]) -> list[Any] | None:
    # assemble_loadout moves admitted preferences out of items into persona.
    # Rules/reflections are auxiliary sections, not an alternative success path.
    items, persona = payload.get("items"), payload.get("persona", [])
    if (not isinstance(items, list) or len(items) > 50
            or not isinstance(persona, list) or len(persona) > 2):
        return None
    return [*items, *persona]


def bind_compact_evidence(payload: Mapping[str, Any]) -> dict:
    """Run after every final item truncation/loadout, without walking content."""
    result = dict(payload)
    diagnostics = result.get("recall_diagnostics")
    if not isinstance(diagnostics, Mapping):
        return result
    items = _final_records(result) or []
    diagnostics = bind_final_selection(diagnostics, items)
    diagnostics["selected_count"] = len(items)
    if not items and isinstance(diagnostics.get("caller_assistance"), Mapping):
        # Do not turn an authority failure into a successful absence claim.
        absent = result.get("retrieval_status") == "no_evidence"
        diagnostics["admission_status"] = "no_evidence" if absent else "unavailable"
        caller = dict(diagnostics["caller_assistance"])
        caller.update(status="no_evidence" if absent else "unavailable",
                      outcome="no_support" if absent else "unavailable")
        diagnostics["caller_assistance"] = caller
    result["recall_diagnostics"] = diagnostics
    return result


def business_recall_supported(parsed: Any) -> bool:
    """Accept only the documented RPC/service bundle, never nested user data."""
    node = parsed
    for _ in range(3):
        if not isinstance(node, Mapping) or node.get("ok") is not True:
            return False
        if (node.get("error") or node.get("bypassed") is True or node.get("isError") is True
                or node.get("status") in {"unavailable", "error", "failed", "timeout", "cancelled", "blocked", "ambiguous"}):
            return False
        if "bundle" in node:
            break
        node = node.get("result")
    else:
        return False
    bundle = node.get("bundle")
    if not isinstance(bundle, Mapping) or bundle.get("retrieval_status") not in _SUPPORTED:
        return False
    items = _final_records(bundle)
    if not items:
        return False
    if any(not isinstance(item, Mapping) or not _record_id(item)
           or item.get("status") != "active" for item in items):
        return False
    ids = [_record_id(item) for item in items]
    if len(set(ids)) != len(ids):
        # ID-only legacy proofs cannot disambiguate two source/scope partitions.
        return False
    diagnostics = bundle.get("recall_diagnostics")
    if not isinstance(diagnostics, Mapping) or diagnostics.get("admission_status") not in _SUPPORTED:
        return False
    count = diagnostics.get("selected_count")
    if count is not None and (type(count) is not int or count != len(items)):
        return False
    caller = diagnostics.get("caller_assistance")
    if (not isinstance(caller, Mapping) or caller.get("status") != "evidence_found"
            or caller.get("outcome") != "supported"):
        return False
    proofs = caller.get("proofs")
    if not isinstance(proofs, list) or not proofs:
        return False
    bound = bind_selected_proofs(proofs, items)
    # Compact diagnostics intentionally retain only a bounded proof subset.
    # Certify existence of supported returned evidence, not every result item.
    return bool(bound) and len(bound) == len(proofs)
