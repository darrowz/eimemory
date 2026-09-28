"""Safe profile-resolution diagnostics. No fallbacks, mutations or approval."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from hashlib import sha256
from typing import Any


def profile_resolution_diagnostics(error: Exception, *, profile_key: str,
                                   capability_scope: str, runtime_scope: Any) -> dict:
    text = str(error)
    code = 'profile_resolution_exception'
    # Diagnostic classification only; every result remains a blocked selection.
    # Matching is limited to messages emitted by CapabilityProfiles itself.
    if 'profile' in text and 'not available in this exact scope' in text:
        code = 'profile_not_available_in_exact_scope'
    elif 'does not satisfy its typed contract' in text:
        code = 'stored_descriptor_contract_invalid'
    elif 'digest does not match its descriptor' in text:
        code = 'stored_descriptor_digest_mismatch'
    elif 'lineage index' in text:
        code = 'profile_lineage_index_mismatch'
    elif 'has no active capability definition' in text:
        code = 'profile_required_capability_unavailable'
    elif 'refusing truncation' in text:
        code = 'profile_resolution_budget_exceeded'
    elif 'conflicting same-priority' in text:
        code = 'profile_selector_conflict'
    scope = asdict(runtime_scope) if is_dataclass(runtime_scope) else runtime_scope
    scope = scope if isinstance(scope, Mapping) else {}
    causes, seen, current = [], set(), error
    while current is not None and id(current) not in seen and len(causes) < 8:
        seen.add(id(current))
        causes.append({'exception_type': type(current).__name__,
                       'message_sha256': sha256(str(current).encode('utf-8', errors='replace')).hexdigest()})
        current = current.__cause__ or current.__context__
    return {'schema': 'capability.profile_resolution_diagnostics.v1', 'reason_code': code,
            'profile_key': profile_key, 'capability_scope': capability_scope,
            'runtime_scope': {key: scope.get(key, '') for key in
                              ('tenant_id', 'agent_id', 'workspace_id', 'user_id')},
            'exception_chain': causes, 'diagnostic_only': True,
            'fallback_applied': False, 'profile_mutated': False}
