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


_ABSENCE_REASONS = frozenset({
    '', 'no_candidates', 'model_no_selection', 'answer_requirements_rejected',
    'requested_attribute_absent', 'reviewed_original_evidence', 'no_supporting_evidence',
    # Legacy serialization added this to a genuine no-support result. It is
    # removed only when ALL explicit status fields still certify absence.
    'final_selection_unavailable',
})


def _state_status(state: Mapping[str, Any]) -> str:
    return str(state.get('status') or state.get('admission_status') or '')


def _set_state_status(state: dict, status: str) -> None:
    key = 'status' if 'status' in state or 'admission_status' not in state else 'admission_status'
    state[key] = status
    if 'status' in state and 'admission_status' in state:
        state['admission_status'] = status


def invalidate_empty_selection(state: Mapping[str, Any], *, selected_count: int) -> dict:
    result = dict(state)
    caller = result.get('caller_assistance')
    if selected_count or not isinstance(caller, Mapping):
        return result
    caller = dict(caller)
    supported = caller.get('status') == 'evidence_found' and caller.get('outcome') == 'supported'
    absent = (_state_status(result) == 'no_evidence'
              and caller.get('status') == 'no_evidence' and caller.get('outcome') == 'no_support'
              and caller.get('reason', '') in _ABSENCE_REASONS
              and result.get('collection_complete') is not False)
    caller.pop('proofs', None)
    caller.pop('independent_scored', None)
    if absent:
        caller.update(status='no_evidence', outcome='no_support')
        if caller.get('reason') in {'final_selection_unavailable', ''} or 'reason' not in caller:
            caller['reason'] = 'no_supporting_evidence'
    else:
        old_reason = caller.get('reason', '')
        caller.update(status='unavailable', outcome='unavailable')
        if supported:
            caller['reason'] = 'final_selection_empty'
        elif old_reason in _ABSENCE_REASONS:
            caller['reason'] = 'final_selection_unavailable'
        if _state_status(result) != 'ambiguous':
            _set_state_status(result, 'unavailable')
    result['caller_assistance'] = caller
    result.pop('scored', None)
    return result


def bind_final_selection(state: Mapping[str, Any], records: Sequence[Any]) -> dict:
    result = dict(state)
    caller = result.get('caller_assistance')
    if not isinstance(caller, Mapping):
        return result
    caller = dict(caller)
    if caller.get('status') != 'evidence_found' or caller.get('outcome') != 'supported':
        caller.pop('proofs', None)
        caller.pop('independent_scored', None)
    else:
        proofs = bind_selected_proofs(caller.get('proofs'), records)
        if proofs:
            caller['proofs'] = proofs
        else:
            caller.pop('proofs', None)
            caller.pop('independent_scored', None)
            caller.update(status='unavailable', outcome='unavailable',
                          reason='final_proof_binding_failed' if records else 'final_selection_empty')
            _set_state_status(result, 'unavailable')
    result['caller_assistance'] = caller
    return invalidate_empty_selection(result, selected_count=len(records))


def _final_records(payload: Mapping[str, Any]) -> list[Any] | None:
    # assemble_loadout moves admitted preferences out of items into persona.
    # Rules/reflections are auxiliary sections, not an alternative success path.
    items, persona = payload.get('items'), payload.get('persona', [])
    if (not isinstance(items, list) or len(items) > 50
            or not isinstance(persona, list) or len(persona) > 2):
        return None
    return [*items, *persona]


def bind_compact_evidence(payload: Mapping[str, Any]) -> dict:
    """Bind final items/persona without upgrading failure to absence or success."""
    result = dict(payload)
    diagnostics = result.get('recall_diagnostics')
    if not isinstance(diagnostics, Mapping):
        return result
    diagnostics = dict(diagnostics)
    records = _final_records(result)
    malformed = records is None
    records = records or []
    public_status = result.get('retrieval_status')
    admission_status = diagnostics.get('admission_status')
    contradictory = bool(records) and 'no_evidence' in {public_status, admission_status}
    # Operational failure dominates a stale no_evidence field. Preserve the
    # valid degraded+supported case, but never mint support from diagnostics.
    if malformed or contradictory or 'unavailable' in {public_status, admission_status}:
        diagnostics['admission_status'] = 'unavailable'
    elif 'ambiguous' in {public_status, admission_status}:
        diagnostics['admission_status'] = 'ambiguous'
    elif not records:
        diagnostics['admission_status'] = (
            'no_evidence' if public_status == 'no_evidence'
            and admission_status in {None, 'no_evidence'} else 'unavailable')
    elif 'degraded' in {public_status, admission_status}:
        diagnostics['admission_status'] = 'degraded'
    diagnostics = bind_final_selection(diagnostics, records)
    diagnostics['selected_count'] = len(records)
    caller = diagnostics.get('caller_assistance')
    if isinstance(caller, Mapping):
        caller = dict(caller)
        if malformed or contradictory:
            caller.update(status='unavailable', outcome='unavailable',
                          reason='final_payload_invalid' if malformed else 'final_outcome_mismatch')
            caller.pop('proofs', None)
            caller.pop('independent_scored', None)
            diagnostics['admission_status'] = 'unavailable'
        elif records and (caller.get('status'), caller.get('outcome')) != ('evidence_found', 'supported'):
            # A nonempty final result with a no-support verdict is inconsistent.
            # Do not silently drop those rows or certify them as supported.
            diagnostics['admission_status'] = 'unavailable'
            if caller.get('status') != 'unavailable':
                caller.update(status='unavailable', outcome='unavailable', reason='final_outcome_mismatch')
        diagnostics['caller_assistance'] = caller
    if diagnostics.get('admission_status') in {'unavailable', 'ambiguous', 'degraded'} or not records:
        result['retrieval_status'] = diagnostics.get('admission_status', 'unavailable')
    result['recall_diagnostics'] = diagnostics
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
