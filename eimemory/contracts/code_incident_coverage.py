"""Bounded ingress chronology. This contract never certifies global novelty."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from hashlib import sha256
import json
from typing import Any

CHECKPOINT_SCHEMA = 'code_incident_coverage_checkpoint.v1'
OBSERVATION_SCHEMA = 'code_incident_coverage_observation.v1'
DOMAIN = 'sqlite_records_and_events_since_native_checkpoint'
SCOPE_FIELDS = ('tenant_id', 'agent_id', 'workspace_id', 'user_id')
FACT_FIELDS = ('known_before_detection', 'prior_user_reported', 'manual_bootstrap')
NATIVE_SOURCES = frozenset({'eimemory.runtime_identity_drift', 'eimemory.release_closure_failure', 'eimemory.system_code_repair_failure'})
MAX_CHECKPOINT_AGE_SECONDS = 3600
MAX_JOURNAL_PAGE = 5000
MAX_EPOCH_CHECKPOINTS = 128


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def digest(value: Any) -> str:
    return sha256(canonical(value).encode()).hexdigest()


def exact_scope(value: Any) -> dict:
    if (not isinstance(value, Mapping) or set(value) != set(SCOPE_FIELDS)
            or any(not isinstance(value[key], str) or not value[key] or value[key] != value[key].strip()
                   or len(value[key]) > 256 or any(ord(char) < 32 for char in value[key]) for key in SCOPE_FIELDS)):
        raise ValueError('incident_coverage_scope_invalid')
    return {key: value[key] for key in SCOPE_FIELDS}


def instant(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError('incident_coverage_time_invalid')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('incident_coverage_time_invalid')
    return result


def incident_key(incident_class: Any, release_commit: Any, codes: Any) -> str | None:
    """Exact typed identity only; never infer semantic absence from free text."""
    if (not isinstance(incident_class, str) or not incident_class or len(incident_class) > 200
            or not isinstance(release_commit, str) or len(release_commit) != 40
            or any(char not in '0123456789abcdef' for char in release_commit)
            or not isinstance(codes, list) or not codes or len(codes) > 128
            or any(not isinstance(code, str) or not code or len(code) > 512 for code in codes)):
        return None
    return digest(dict(incident_class=incident_class, release_commit=release_commit, diagnostic_codes=sorted(set(codes))))


def unknown(reason: str) -> dict:
    return dict(schema=OBSERVATION_SCHEMA, status='unproven', diagnostic_only=True,
        claim_domain=DOMAIN, reason=reason, global_facts={key: None for key in FACT_FIELDS},
        covered_known_before_detection=None, covered_prior_user_reported=None,
        external_prior_knowledge=None, external_prior_user_reports=None,
        all_backend_knowledge_covered=False, qualifies_for_product_completion=False)


def validate_diagnostic(value: Any, *, schema: str, scope: Any) -> dict:
    """Check shape only. HMAC and physical chronology are separate authorities."""
    owner = exact_scope(scope)
    if (not isinstance(value, Mapping) or value.get('schema') != schema or value.get('scope') != owner
            or value.get('diagnostic_only') is not True or value.get('claim_domain') != DOMAIN
            or value.get('qualifies_for_product_completion') is not False
            or value.get('all_backend_knowledge_covered') is not False
            or value.get('external_prior_knowledge') is not None
            or value.get('external_prior_user_reports') is not None):
        raise ValueError('incident_coverage_contract_invalid')
    if schema == OBSERVATION_SCHEMA:
        facts = value.get('global_facts')
        if (not isinstance(facts, Mapping) or set(facts) != set(FACT_FIELDS)
                or any(item is not None and type(item) is not bool for item in facts.values())):
            raise ValueError('incident_coverage_global_facts_invalid')
        # A bounded receipt is not authority to manufacture global negatives.
        if any(item is False for item in facts.values()):
            raise ValueError('incident_coverage_global_negative_forbidden')
        for field in ('covered_known_before_detection', 'covered_prior_user_reported'):
            if value.get(field) is not None and type(value[field]) is not bool:
                raise ValueError('incident_coverage_bounded_fact_invalid')
    elif schema == CHECKPOINT_SCHEMA:
        for field in ('high_water', 'prior_high_water', 'epoch_start_sequence', 'incremental_count', 'epoch_checkpoint_count'):
            if type(value.get(field)) is not int or value[field] < 0:
                raise ValueError('incident_coverage_checkpoint_coordinates_invalid')
        if not 1 <= value['epoch_checkpoint_count'] <= MAX_EPOCH_CHECKPOINTS:
            raise ValueError('incident_coverage_checkpoint_chain_bound_invalid')
        if (type(value.get('continuity')) is not bool or type(value.get('incremental_truncated')) is not bool
                or value['high_water'] < value['prior_high_water'] or value['high_water'] < value['epoch_start_sequence']):
            raise ValueError('incident_coverage_checkpoint_coordinates_invalid')
        instant(value.get('observed_at')); instant(value.get('epoch_started_at'))
    else:
        raise ValueError('incident_coverage_schema_unsupported')
    if value.get('backend_coverage') != {'sqlite.records': True, 'sqlite.events': True, 'postgres': None, 'external_inputs': None}:
        raise ValueError('incident_coverage_backend_claim_invalid')
    return dict(value)
