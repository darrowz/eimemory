"""Operator-authorized existing-memory recall and historical business qualification.

This certificate proves bounded current recall. Explicit protected historical
business purpose also qualifies the exact real business source memories. It
never invents historical queries, host delivery or new natural observations.
Authorization comes only from a protected root policy; reports cannot grant it.
"""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from math import isfinite
import os
from pathlib import Path
import re
from types import SimpleNamespace
from uuid import uuid4

from eimemory import identity_authority
from eimemory.core import strict_json
from eimemory.core.clock import now_iso
from eimemory.evaluation import controlled_memory_benchmark as benchmark
from eimemory.evaluation import historical_business_qualification as historical
from eimemory.governance.learning.effect_policy import canonical, digest, seal, verify
from eimemory.governance.release.evidence_contract import current_release_identity, release_identity_payload
from eimemory.identity_ops import _repair_snapshot, _RepairSkipped
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.models.source_partitions import normalize_source_ids
from eimemory.ops.candidate_readiness_barrier import (
    _explicit_pg_bypass, candidate_readiness_barrier_ok, wait_for_candidate_readiness,
)
from eimemory.ops.projection_acceptance_restore import ensure_candidate_projection_restored
from eimemory.retrieval.diagnostics import STAGES
from eimemory.retrieval.postgres_vector import (PostgresVectorCandidateSource,
    _CANDIDATE_ERROR_CODES, postgres_config_fingerprint)

SCHEMA = 'controlled_production_acceptance.v1'
POLICY_SCHEMA = 'controlled_production_acceptance_policy.v1'
EXACT_SCOPE_SCHEMA = 'controlled_production_acceptance.v2'
EXACT_SCOPE_POLICY_SCHEMA = 'controlled_production_acceptance_policy.v2'
MODE = 'controlled_existing_memory'
SOURCE = 'eimemory.evaluation.controlled_production_acceptance'
EVIDENCE_CLASS = 'operator_controlled_existing_memory'
POLICY_ENV = 'EIMEMORY_CONTROLLED_PRODUCTION_ACCEPTANCE_POLICY'
DEFAULT_POLICY = '/etc/eimemory-controlled-production-acceptance/policy.json'
from eimemory.contracts.user_control_plane import user_mode, coordinates, read_file as read_user_authority
if user_mode():
    DEFAULT_POLICY = str(coordinates()['authority']/'controlled-production-acceptance/policy.json')
_SCOPE = {'tenant_id', 'agent_id', 'workspace_id', 'user_id'}


# Fixed diagnostic vocabulary; arbitrary exception text is never receipt evidence.
VERIFICATION_FAILURE_REASONS = frozenset(
    "controlled_acceptance_" + code for code in """
    agent_evidence_invalid authority_changed_during_run authority_changed_during_verification business_origin_changed business_qualification_invalid
    business_origin_invalid business_origin_parent_invalid business_policy_invalid business_source_changed business_source_not_authorized
    business_sources_invalid hermes_business_scope_inventory_mismatch
    candidate_readiness_unverified cascade_evidence_authority_unavailable cascade_evidence_invalid cascade_evidence_parent_invalid cascade_evidence_projection_changed
    cascade_evidence_summary_changed case_evidence_invalid compact_result_boundary_invalid compact_result_projection_failed compact_result_size_exceeded
    compact_result_status_invalid compact_result_structure_invalid current_release_unavailable current_report_missing engine_changed_during_run
    engine_identity_unavailable exact_scope_required high_water_changed high_water_invalid index_contract_unavailable
    insufficient_existing_records not_configured persist_invalid policy_agents_invalid policy_cutoff_in_future
    policy_engine_contract_invalid policy_invalid policy_limits_invalid policy_owner_scopes_invalid policy_path_invalid
    policy_untrusted production_engine_contract_mismatch release_mismatch release_receipt_invalid report_authority_invalid
    report_expired report_not_accepted retrieval_incomplete returned_boundary_invalid returned_evidence_invalid
    source_authority_unavailable source_boundary_invalid source_cutoff_invalid source_query_material_empty source_version_changed
    source_version_unavailable timestamp_invalid traffic_agent_not_authorized traffic_discovery_unavailable verification_failed
    """.split()
)


class ControlledAcceptanceError(ValueError):
    pass


def _error(code):
    raise ControlledAcceptanceError('controlled_acceptance_' + code)


def _schema_for_policy(policy):
    return EXACT_SCOPE_SCHEMA if policy['schema'] == EXACT_SCOPE_POLICY_SCHEMA else SCHEMA


def _instant(value):
    if not isinstance(value, str) or not value.strip():
        _error('timestamp_invalid')
    try:
        instant = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        _error('timestamp_invalid')
    if instant.tzinfo is None:
        _error('timestamp_invalid')
    return instant.astimezone(timezone.utc)


def _scope(value):
    if isinstance(value, ScopeRef):
        value = asdict(value)
    if (not isinstance(value, dict) or set(value) != _SCOPE
            or any(not isinstance(v, str) or not v or v.strip() != v or '*' in v for v in value.values())):
        _error('exact_scope_required')
    return ScopeRef.from_dict(value)


def _read_policy(path):
    """Host-root ownership, all ancestors, no symlinks, bounded strict JSON.

    The native authority reader also understands PrivateUsers host-root seals.
    Its policy-file convention is retained so namespace sealing stays correct.
    """
    if path.name != 'policy.json':
        _error('policy_path_invalid')
    try:
        if user_mode():
            return strict_json.loads(read_user_authority(path), max_bytes=65536, max_depth=12)
        identity_authority._directory(path.parent)
        fd = identity_authority._open(path, os.O_RDONLY)
        try:
            return strict_json.loads(identity_authority._read(fd, 65536), max_bytes=65536, max_depth=12)
        finally:
            os.close(fd)
    except (OSError, ValueError, RuntimeError):
        _error('policy_untrusted')


def _validate_policy(value, owner):
    fields = {'schema', 'mode', 'request_digest', 'owner_scope', 'agents', 'stable_before',
              'minimum_cases_per_scope', 'max_cases_per_scope', 'freshness_seconds', 'engine_contract_digest'}
    if (not isinstance(value, dict) or not fields.issubset(value)
            or set(value) - fields - {'additional_owner_scopes', 'purpose', 'business_sources'}
            or value.get('schema') not in {POLICY_SCHEMA, EXACT_SCOPE_POLICY_SCHEMA} or value.get('mode') != MODE
            or re.fullmatch('[0-9a-f]{64}', str(value.get('request_digest', ''))) is None
            or value.get('request_digest') == '0' * 64):
        _error('policy_invalid')
    primary_owner = _scope(value['owner_scope'])
    authorized_owners = [primary_owner]
    if 'additional_owner_scopes' in value:
        additional = value['additional_owner_scopes']
        if not isinstance(additional, list) or not 1 <= len(additional) <= 4:
            _error('policy_owner_scopes_invalid')
        for entry in additional:
            if not isinstance(entry, dict):
                _error('policy_owner_scopes_invalid')
            exact_owner = _scope(entry)
            if (exact_owner.tenant_id != primary_owner.tenant_id
                    or exact_owner.agent_id != primary_owner.agent_id
                    or any(any(mark in text for mark in '?[]') for text in entry.values())
                    or exact_owner in authorized_owners):
                _error('policy_owner_scopes_invalid')
            authorized_owners.append(exact_owner)
    # An evaluation-agent scope is not caller authority. Only the protected
    # primary owner or an explicitly listed exact owner may issue a receipt.
    if _scope(owner) not in authorized_owners:
        _error('policy_invalid')
    if re.fullmatch('[0-9a-f]{64}', str(value.get('engine_contract_digest', ''))) is None:
        _error('policy_engine_contract_invalid')
    for key, low, high in (('minimum_cases_per_scope', 3, 50), ('max_cases_per_scope', 3, 50),
                           ('freshness_seconds', 1, 86400)):
        if type(value.get(key)) is not int or not low <= value[key] <= high:
            _error('policy_limits_invalid')
    if value['max_cases_per_scope'] < value['minimum_cases_per_scope']:
        _error('policy_limits_invalid')
    if _instant(value['stable_before']) > datetime.now(timezone.utc):
        _error('policy_cutoff_in_future')
    entries = value['agents']
    exact_scopes = value['schema'] == EXACT_SCOPE_POLICY_SCHEMA
    if not isinstance(entries, list) or not 1 <= len(entries) <= (64 if exact_scopes else 16):
        _error('policy_agents_invalid')
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {'scope', 'source_ids'}:
            _error('policy_agents_invalid')
        exact = _scope(entry['scope'])
        sources = entry['source_ids']
        if (exact.tenant_id != owner.tenant_id or (not exact_scopes and exact.agent_id in seen)
                or not exact.workspace_id.endswith('::channel::hermes')
                or not isinstance(sources, list) or not sources or len(sources) > 16
                or any(not isinstance(s, str) or not s or s.strip() != s or '*' in s for s in sources)
                or len(set(sources)) != len(sources) or tuple(sorted(sources)) != normalize_source_ids(sources)):
            _error('policy_agents_invalid')
        if exact_scopes:
            coordinates = tuple(asdict(exact).values())
            tuples = {(*coordinates, source) for source in sources}
            if tuples.intersection(seen):
                _error('policy_agents_invalid')
            seen.update(tuples)
        else:
            seen.add(exact.agent_id)
    historical.validate_business_policy(value)
    normalized = deepcopy(value)
    for entry in normalized['agents']:
        entry['source_ids'] = sorted(entry['source_ids'])
    return normalized


def _traffic_coverage(runtime, policy):
    from eimemory.evaluation.recall_agent_scopes import recall_agent_scopes
    # Discovery's compatibility API treats read errors as no traffic. An
    # acceptance authority must first prove that the discovery table is readable.
    try:
        runtime.store.run_locked(lambda sqlite: sqlite.execute('SELECT agent_id FROM proactive_decisions LIMIT 1').fetchall())
        traffic = recall_agent_scopes(runtime, policy['owner_scope'])
    except Exception:
        _error('traffic_discovery_unavailable')
    allowed = {entry['scope']['agent_id'] for entry in policy['agents']}
    if any(entry['agent_id'] not in allowed for entry in traffic):
        _error('traffic_agent_not_authorized')


def load_controlled_policy(runtime, scope):
    """None only for an absent unconfigured default; invalid opt-in fails closed."""
    override = os.environ.get(POLICY_ENV, '').strip()
    path = Path(override or DEFAULT_POLICY)
    if not path.exists() and not path.is_symlink() and not override:
        return None
    owner = _scope(scope)
    value = _validate_policy(_read_policy(path), owner)
    from eimemory.evaluation.hermes_profile_coverage import load_topology, policy_covers_topology
    topology = load_topology()
    if topology is not None:
        try:
            policy_covers_topology(value, topology)
        except ValueError as exc:
            _error(str(exc))
    _traffic_coverage(runtime, value)
    return value


def _release(runtime, scope):
    from eimemory.evaluation.explicit_recall import _release_reference
    identity = current_release_identity(runtime, scope)
    if identity is None or not identity.complete:
        _error('current_release_unavailable')
    payload = release_identity_payload(identity)
    try:
        reference = _release_reference(runtime, payload, channel='hermes', scope=scope)
    except ValueError:
        _error('release_receipt_invalid')
    if not reference:
        _error('release_receipt_invalid')
    return payload, reference


def _engine(runtime):
    try:
        identity = runtime.memory.recall_engine.effective_identity()
        copied = strict_json.loads(canonical(identity), max_bytes=65536, max_depth=16)
    except Exception:
        _error('engine_identity_unavailable')
    if not isinstance(copied, dict) or not copied or not copied.get('identity_digest'):
        _error('engine_identity_unavailable')
    # The certificate's own append increments the SQLite authority revision;
    # background index watermarks and circuit probes also change without a
    # configuration change. Bind the stable executable/configuration contract
    # here. Actual collection health is separately signed per native call, and
    # every source envelope/SQL version is independently revalidated.
    copied.pop('identity_digest', None)
    source = copied.get('candidate_source')
    if not isinstance(source, dict) or not source.get('sqlite_authority'):
        _error('engine_identity_unavailable')
    source.pop('authority_revision', None)
    source.pop('postgres', None)
    if source.get('candidate_source_type') == 'PostgresVectorCandidateSource':
        native_source = runtime.memory.recall_engine.candidate_source
        bypass, configuration = _explicit_pg_bypass(native_source)
        if bypass:
            # An explicitly unavailable optional projection has no durable
            # index to bind. Use the same exact native configuration boundary
            # as the measured barrier, and bind its current stable semantics.
            # This is a different policy contract; old ready-index certificates
            # are never rewritten or accepted under this exemption.
            if (source.get('enabled') is not configuration['enabled']
                    or source.get('configured') is not (configuration['configured']
                        and native_source.embedding_provider is not None)):
                _error('engine_identity_unavailable')
            source['candidate_index_requirement'] = dict(mode='not_applicable',
                reason=bypass, source_configuration=configuration,
                config_fingerprint=postgres_config_fingerprint(native_source.config))
        else:
            try:
                state = native_source.repository.read_index_state()
                source['durable_index_contract'] = dict(ready=state.ready,
                    embedding_fingerprint=state.embedding_fingerprint,
                    projection_digest_schema=state.projection_digest_schema,
                    projection_fingerprint=state.projection_fingerprint)
            except Exception:
                _error('index_contract_unavailable')
            if state.ready is not True:
                _error('index_contract_unavailable')
    copied['contract_schema'] = 'controlled_retrieval_engine_contract.v1'
    copied['identity_digest'] = digest(copied)
    return copied


def controlled_retrieval_contract(runtime):
    """Secret-free stable contract for protected operator policy provisioning."""
    return _engine(runtime)


def _validate_versions(sqlite, agents):
    """Recheck every measured source in one snapshot, or the append transaction."""
    for agent in agents:
        for sample in agent['samples']:
            pairs = [(sample['source_binding'], sample['source_version']),
                     *zip(sample['returned'], sample['returned_versions']),
                     *[(item['binding'], item['version']) for item in sample.get('cascade_evidence', [])],
                     *[(item['binding'], item['version']) for item in sample.get('business_provenance', {}).get('parents', [])]]
            for binding, expected in pairs:
                scope = _scope(binding['scope'])
                ref = (*[getattr(scope, k) for k in ('tenant_id', 'agent_id', 'workspace_id', 'user_id')],
                       binding['source_id'], binding['record_id'])
                try:
                    record, version = _repair_snapshot(sqlite, ref)
                except (_RepairSkipped, ValueError, RuntimeError):
                    _error('source_version_changed')
                if list(version) != expected or benchmark._binding(record) != binding:
                    _error('source_version_changed')


def _physical(runtime, binding, scope, sources, cutoff, *, require_old=True, episode=False):
    if (not isinstance(binding, dict) or binding.get('scope') != asdict(scope)
            or binding.get('source_id') not in sources):
        _error('source_boundary_invalid')
    ref = (*[getattr(scope, k) for k in ('tenant_id', 'agent_id', 'workspace_id', 'user_id')],
           binding['source_id'], str(binding.get('record_id') or ''))
    try:
        record, version = runtime.store.read_consistent(lambda sqlite: _repair_snapshot(sqlite, ref))
    except (_RepairSkipped, ValueError, RuntimeError):
        _error('source_version_unavailable')
    reason = benchmark._eligible_reason(record, scope, sources)
    if episode:
        from eimemory.recall import is_episode_evidence_record, is_inactive_or_superseded_record, is_outcome_pollution_record
        from eimemory.metadata import business_metadata
        from eimemory.contracts.recall_boundary import default_recall_blocked_lane
        metadata = business_metadata(record.meta)
        quality = metadata.get('quality')
        lane = default_recall_blocked_lane(metadata, record.content, record.provenance)
        reason = (record.kind not in {'memory', 'raw_chunk'} or not is_episode_evidence_record(record)
            or record.status != 'active' or is_inactive_or_superseded_record(record)
            or is_outcome_pollution_record(record) or lane not in {'', 'task_context', 'raw'}
            or (isinstance(quality, dict) and quality.get('capture_decision') == 'reject')
            or benchmark.MemoryAPI._is_temporally_stale_memory(record))
    if (reason
            or (require_old and benchmark._stable_source_reason(record, _instant(cutoff)))
            or benchmark._binding(record) != binding):
        _error('source_version_changed')
    from eimemory.subject_projection_consumption import projection_is_current
    if not projection_is_current(runtime.store, record):
        _error('source_authority_unavailable')
    return list(version)


def _parent_links(record):
    """The native cascade producer's exact, authoritative derivation edges."""
    linked = {str(link.target_id).strip() for link in record.links
              if link.relation in {'derived_from', 'extracted_from', 'evidence'}}
    linked.update(str(ref).strip() for ref in record.evidence
                  if str(ref).strip().startswith(('mem_', 'rec_')))
    return linked


def _cascade_evidence(runtime, compact, scope, sources, cutoff, primary, *, raw=None):
    """Bind native legacy summaries to exact linked physical episode parents.

    A summary's ID alone grants no authority. It must be the native complete
    summary of exactly one allowed source in this scope and a derivation edge
    of an actually selected item. No title/text becomes a fact or natural label.
    """
    summaries = compact.get('evidence', [])
    if not isinstance(summaries, list) or len(summaries) > 2:
        _error('cascade_evidence_invalid')
    if raw is not None and (not isinstance(raw, list) or len(raw) > 2 or summaries != raw):
        _error('cascade_evidence_projection_changed')
    linked = set().union(*(_parent_links(record) for record in primary)) if primary else set()
    evidence, seen = [], set()
    for summary in summaries:
        ref = summary.get('record_id') if isinstance(summary, dict) else None
        if not isinstance(ref, str) or not ref or ref in seen or ref not in linked:
            _error('cascade_evidence_parent_invalid')
        matches = [record for source in sources if (record := runtime.store.get_by_exact_ref(
            ref, scope=scope, source_id=source)) is not None]
        if len(matches) != 1:
            _error('cascade_evidence_authority_unavailable')
        parent = matches[0]
        expected = runtime.memory._selected_record_summaries([parent])[0]
        if summary != expected:
            _error('cascade_evidence_summary_changed')
        binding = benchmark._binding(parent)
        version = _physical(runtime, binding, scope, sources, cutoff, require_old=False, episode=True)
        evidence.append(dict(binding=binding, version=version, summary_digest=digest(summary)))
        seen.add(ref)
    return evidence


def _fallback_codes(engine):
    raw = engine.get('fallback_error_codes')
    if not isinstance(raw, dict) or not raw:
        return None
    codes = {}
    for key, value in list(raw.items())[:8]:
        if not isinstance(key, str) or not key or type(value) is not int or isinstance(value, bool) or value < 1:
            return None
        codes[key] = min(value, 1_000_000)
    return codes


def _complete(bundle):
    """Strict native sparse-counter contract; no coercion of missing values."""
    explanation = bundle.explanation if isinstance(bundle.explanation, dict) else {}
    selector = explanation.get('relevance_selector')
    engine = explanation.get('engine_diagnostics')
    selector = selector if isinstance(selector, dict) else {}
    engine = engine if isinstance(engine, dict) else {}
    drops = engine.get('drops')
    recall = drops.get('recall_budget_exhausted', 0 if len(drops) < 8 else None) if isinstance(drops, dict) else None
    def count(value):
        return value if type(value) is int and value >= 0 else None
    statuses = {'evidence_found', 'no_evidence', 'unavailable', 'degraded', 'ambiguous'}
    state = dict(collection_complete=selector.get('collection_complete')
                 if type(selector.get('collection_complete')) is bool else None,
                 status=selector.get('status') if selector.get('status') in statuses else 'unknown',
                 retrieval_status=explanation.get('retrieval_status') if explanation.get('retrieval_status') in statuses else 'unknown',
                 source_searches=count(engine.get('source_searches')), source_budget_exhausted=count(engine.get('source_budget_exhausted')),
                 recall_budget_exhausted=count(recall), fallback=engine.get('fallback')
                 if type(engine.get('fallback')) is bool else None,
                 fallback_error_codes=_fallback_codes(engine))
    lexical_complete = (state['collection_complete'] is True and state['status'] == 'evidence_found'
            and state['retrieval_status'] == 'evidence_found'
            and type(state['source_searches']) is int and state['source_searches'] > 0
            and all(type(state[k]) is int and state[k] == 0 for k in
                    ('source_budget_exhausted', 'recall_budget_exhausted')))
    fallback_ok = state['fallback'] is False or (
        state['fallback'] is True and state['fallback_error_codes']
        and set(state['fallback_error_codes']) <= {'disabled', 'not_configured'})
    return state, lexical_complete and fallback_ok


def _recall_diagnostics(bundle):
    """Retain only measured, enumerated engine failure/timing diagnostics."""
    explanation = bundle.explanation if isinstance(bundle.explanation, dict) else {}
    engine = explanation.get('engine_diagnostics')
    engine = engine if isinstance(engine, dict) else {}
    result = dict(schema='controlled_recall_diagnostics.v1', measurement_origin='native_engine_bundle',
                  fallback_error_codes={}, source_stages_ms={})
    def number(value):
        try:
            return type(value) in (int, float) and isfinite(value) and value >= 0
        except OverflowError:
            return False
    if number(engine.get('elapsed_ms')):
        result['elapsed_ms'] = round(min(engine['elapsed_ms'], 1_000_000), 3)
    stages = engine.get('source_stages_ms')
    if isinstance(stages, dict):
        result['source_stages_ms'] = {key: round(min(stages[key], 1_000_000), 3)
            for key in STAGES if number(stages.get(key))}
    codes = engine.get('fallback_error_codes')
    if isinstance(codes, dict):
        for key, value in list(codes.items())[:8]:
            if type(value) is int and value >= 0:
                code = key if key in _CANDIDATE_ERROR_CODES else 'unknown'
                result['fallback_error_codes'][code] = min(1_000_000,
                    result['fallback_error_codes'].get(code, 0) + value)
    samples = engine.get('source_samples')
    if isinstance(samples, list):
        result['source_samples'] = []
        for sample in samples[:4]:
            if not isinstance(sample, dict):
                continue
            row = {}
            if type(sample.get('search_index')) is int and 0 <= sample['search_index'] <= 1_000_000:
                row['search_index'] = sample['search_index']
            if number(sample.get('elapsed_ms')):
                row['elapsed_ms'] = round(min(sample['elapsed_ms'], 1_000_000), 3)
            if sample.get('failed_stage') in STAGES or sample.get('failed_stage') == '':
                row['failed_stage'] = sample['failed_stage']
            if type(sample.get('cache_hit')) is bool:
                row['cache_hit'] = sample['cache_hit']
            result['source_samples'].append(row)
    return result


def _requires_candidate_barrier(release):
    # Published 1.14.150/151 producers did not emit this field. This unreleased
    # feature must first ship in >=1.14.152. Preserve pre-152 signed contracts;
    # an explicitly present barrier is still fully checked at every version.
    version = release.get('release_version') if isinstance(release, dict) else None
    match = re.fullmatch(r'(\d{1,6})\.(\d{1,6})\.(\d{1,6})', str(version or ''))
    return match is None or tuple(int(part) for part in match.groups()) >= (1, 14, 152)


def _evaluate_agent(runtime, entry, policy, cutoff):
    scope, sources = _scope(entry['scope']), tuple(sorted(entry['source_ids']))
    if policy.get('purpose') == historical.PURPOSE:
        # The protected grant already bounds the exact historical targets.
        # Unrelated newer memories must not displace them from a latest-N page.
        # Every target still passes the physical, temporal and origin checks
        # below; a missing or changed grant never falls back to another memory.
        cases = []
        for allowed in policy['business_sources']:
            binding = allowed['source_binding']
            if binding['scope'] != entry['scope'] or binding['source_id'] not in sources:
                continue
            record = runtime.store.get_by_exact_ref(binding['record_id'], scope=scope,
                source_id=binding['source_id'])
            if record is None or benchmark._binding(record) != binding:
                _error('source_version_changed')
            case = benchmark._case(record)
            if case is None:
                _error('source_query_material_empty')
            cases.append(case)
    else:
        dataset = benchmark.build_controlled_memory_benchmark(runtime, scope=scope, source_ids=sources,
            stable_before=cutoff, limit=policy['max_cases_per_scope'])
        cases = dataset.get('cases', [])
    if len(cases) < policy['minimum_cases_per_scope']:
        _error('insufficient_existing_records')
    before = {case['case_id']: _physical(runtime, case['source_binding'], scope, sources, cutoff) for case in cases}
    business_before = {}
    if policy.get('purpose') == historical.PURPOSE:
        business_before = {case['case_id']: historical.business_provenance(
            runtime, case['source_binding'], before[case['case_id']], scope, sources, cutoff, policy) for case in cases}
    observation = {}

    def recall(**kwargs):
        observation['call_count'] += 1
        try:
            bundle = runtime.memory.recall(**kwargs)
            state, complete = _complete(bundle)
            observation.update(state=state, complete=complete, diagnostics=_recall_diagnostics(bundle),
                bundle_observed=True,
                returned_counts={section:len(getattr(bundle,section)) for section in ('items','rules','reflections')})
            returned = [*bundle.items, *bundle.rules, *bundle.reflections]
            bindings = [benchmark._binding(record) for record in returned]
            # Preserve the real returned references even when a later boundary,
            # completeness or compact projection check rejects this call.
            observation['returned'] = bindings
            if any(record.scope != scope or record.source_id not in sources for record in returned):
                _error('returned_boundary_invalid')
            # Only targets must predate cutoff; live results remain version-bound.
            versions = [_physical(runtime, binding, scope, sources, cutoff, require_old=False) for binding in bindings]
            observation['returned_versions'] = versions
            if not complete:
                _error('retrieval_incomplete')
            observation['compact_observation']['attempted'] = True
            try:
                compact = bundle.to_compact_dict(limit=5, include_explanation=False)
            except Exception:
                _error('compact_result_projection_failed')
            if not isinstance(compact,dict):
                observation['compact_observation']['structure_valid'] = False
                _error('compact_result_structure_invalid')
            compact_state = compact.get('retrieval_status')
            observation['compact_observation']['retrieval_status'] = compact_state if compact_state in {
                'evidence_found','no_evidence','unavailable','degraded','ambiguous'} else 'unknown'
            try:
                compact_size = len(canonical(compact).encode())
            except (TypeError,ValueError,OverflowError,RecursionError):
                observation['compact_observation']['structure_valid'] = False
                _error('compact_result_structure_invalid')
            observation['compact_observation']['size_bytes'] = compact_size
            if compact_state != 'evidence_found':
                _error('compact_result_status_invalid')
            if compact_size > 16384:
                _error('compact_result_size_exceeded')
            for section in ('items', 'rules', 'reflections'):
                rows = compact.get(section, [])
                if not isinstance(rows,list) or any(not isinstance(item,dict) for item in rows):
                    observation['compact_observation']['structure_valid'] = False
                    _error('compact_result_structure_invalid')
                allowed = {(record.record_id, record.source_id) for record in getattr(bundle, section)}
                if any((item.get('record_id'), item.get('source_id')) not in allowed
                        or ('scope' in item and item['scope'] != asdict(scope)) for item in rows):
                    _error('compact_result_boundary_invalid')
            observation['compact_observation']['structure_valid'] = True
            compact_refs = [[item.get('record_id'), item.get('source_id')] for item in compact.get('items', [])]
            observation.update(compact_refs=compact_refs, compact_digest=digest(compact))
            raw = bundle.explanation.get('cascade_evidence', [])
            cascade = _cascade_evidence(runtime, compact, scope, sources, cutoff, bundle.items[:5], raw=raw)
            observation['cascade_evidence'] = cascade
            return bundle
        except Exception as exc:
            code = str(exc)
            observation.update(complete=False, failure_reason=code if
                re.fullmatch(r'controlled_acceptance_[a-z_]+', code) else 'controlled_acceptance_native_recall_failed')
            raise

    proxy = SimpleNamespace(store=runtime.store,
        memory=SimpleNamespace(recall=recall, recall_engine=runtime.memory.recall_engine))
    evidence = []
    for case in cases:
        # A failed native callback is still this case's failed observation.
        # Never lose alignment or erase all cases behind a length assertion.
        observation = dict(state={}, diagnostics={}, complete=False, returned=[], returned_versions=[], compact_refs=[],
                           cascade_evidence=[], compact_digest='', call_count=0, failure_reason='',
                           bundle_observed=False,returned_counts={},
                           compact_observation=dict(attempted=False,structure_valid=None))
        sample = benchmark._run_case(proxy, case, scope, sources)
        binding = case['source_binding']
        version = _physical(runtime, binding, scope, sources, cutoff)
        if version != before[case['case_id']]:
            _error('source_version_changed')
        hit = [binding['record_id'], binding['source_id']] in observation['compact_refs']
        passed = (sample.get('passed') is True and sample.get('evaluable') is True
                  and observation['complete'] and observation['call_count'] == 1 and hit)
        if not passed and not observation['failure_reason']:
            code = sample.get('failure_reason', '')
            observation['failure_reason'] = ('controlled_acceptance_' + code if
                isinstance(code, str) and re.fullmatch('[a-z_]+', code) else 'controlled_acceptance_case_failed')
        evidence.append(dict(case_id=case['case_id'], source_binding=binding, source_version=version,
            query_digest=digest(case['query']), context_digest=digest(case['task_context']),
            passed=passed, expected_rank=sample.get('expected_rank'), **observation))
        if policy.get('purpose') == historical.PURPOSE:
            provenance = historical.business_provenance(runtime, binding, version, scope, sources, cutoff, policy)
            if provenance != business_before[case['case_id']]:
                _error('business_origin_changed')
            evidence[-1]['business_provenance'] = provenance
    accepted = all(s['passed'] for s in evidence)
    reason = next((sample['failure_reason'] for sample in evidence if not sample['passed']), '')
    return dict(scope=asdict(scope), source_ids=list(sources), sample_count=len(evidence),
        pass_count=sum(s['passed'] for s in evidence), accepted=accepted,
        blocked_reason=reason, samples=evidence)


def _public(body, *, persisted=False):
    agents = []
    for entry in body.get('agents', []):
        agents.append(dict(agent_id=entry['scope']['agent_id'], scope_digest=digest(entry['scope']),
            source_ids=entry['source_ids'], sample_count=entry['sample_count'], pass_count=entry['pass_count'],
            fail_count=entry['sample_count'] - entry['pass_count'], accepted=entry['accepted'],
            complete=bool(entry['samples']) and all(s['complete'] for s in entry['samples']), blocked_reason=entry['blocked_reason']))
    passed = body.get('accepted') is True
    accepted = passed and persisted
    qualification = deepcopy(body.get('business_qualification', {}))
    if qualification and not persisted:
        qualification['accepted'] = False
        qualification['record_id'] = ''
    return dict(ok=passed, accepted=accepted, status='accepted' if accepted else 'evaluated' if passed else 'failed',
        passed_threshold=passed,
        schema=body.get('schema', SCHEMA), mode=MODE, evidence_class=EVIDENCE_CLASS,
        acceptance_purpose=body.get('acceptance_purpose', 'operational'), business_qualification=qualification,
        policy_digest=body.get('policy_digest', ''), release_identity=body.get('release_identity', {}),
        scope=deepcopy(body.get('scope', {})),
        record_id=body.get('record_id', '') if persisted else '', persisted=persisted,
        certifies_natural_queries=False, certifies_no_answer=False, host_delivery_certified=False,
        memory_contribution_certified=False, natural_samples_created=0, agents=agents,
        candidate_readiness_barrier=deepcopy(body.get('candidate_readiness_barrier', {})),
        projection_acceptance_restore=deepcopy(body.get('projection_acceptance_restore', {})),
        blocked_reason=body.get('blocked_reason', ''))


def _unavailable(reason):
    return {**_public({}), 'status': 'unavailable', 'blocked_reason': reason}



def _report_owner_scope(owner, policy, receipt):
    """Use the explicitly authorized receipt owner for one acceptance high-water.

    Either the primary or an additional policy owner may hold the deployment
    receipt. Without an authorized receipt scope, retain the caller partition.
    Caller authorization is checked before resolving this persistence scope.
    """
    raw = receipt.get("scope") if isinstance(receipt, dict) else None
    if raw not in [policy['owner_scope'], *policy.get('additional_owner_scopes', [])]:
        return owner
    return _scope(raw)


def run_controlled_production_acceptance(runtime, *, scope, persist=True, stable_before=''):
    """Perform native seedless calls only; arbitrary report import is unsupported."""
    owner = _scope(scope)
    if type(persist) is not bool:
        _error('persist_invalid')
    policy = load_controlled_policy(runtime, owner)
    if policy is None:
        return {**_unavailable('controlled_acceptance_not_configured'), 'status': 'not_configured'}
    from eimemory.evaluation.controlled_acceptance_window import (
        controlled_acceptance_window, ControlledAcceptanceWindowError,
    )
    try:
        with controlled_acceptance_window(runtime):
            return _run_controlled_production_acceptance_locked(runtime, owner=owner,
                policy=policy, persist=persist, stable_before=stable_before)
    except ControlledAcceptanceWindowError as exc:
        return {**_unavailable(str(exc)), 'status': 'blocked'}


def _run_controlled_production_acceptance_locked(runtime, *, owner, policy, persist, stable_before):
    try:
        release, receipt = _release(runtime, owner)
        cutoff = policy['stable_before']
        if stable_before and _instant(stable_before) < _instant(cutoff):
            cutoff = stable_before
        started = now_iso()
        # Accept budget must not start while deploy/storage guard still pauses
        # vector-sync; catch up in-process first (barrier stays read-only).
        projection_restore = ensure_candidate_projection_restored(runtime)
        if (projection_restore.get('applicable') is True
                and projection_restore.get('ok') is not True):
            barrier = dict(
                schema='candidate_readiness_barrier.v1', ok=False, applicable=True,
                ready=False, status='failed',
                blocked_reason='projection_acceptance_restore_failed',
                source_type='postgres_vector', started_at=started, completed_at=now_iso(),
                elapsed_seconds=0.0, timeout_seconds=300.0, probe_timeout_seconds=0.2,
                attempt_count=0,
                error_code_counts={'projection_acceptance_restore_failed': 1},
                final_measurement={},
            )
        else:
            barrier = wait_for_candidate_readiness(runtime, timeout_seconds=300)
        barrier_ok = candidate_readiness_barrier_ok(barrier,
            postgres=isinstance(runtime.memory.recall_engine.candidate_source, PostgresVectorCandidateSource),
            source=runtime.memory.recall_engine.candidate_source)
        if barrier_ok:
            engine = _engine(runtime)
            if digest(engine) != policy['engine_contract_digest']:
                _error('production_engine_contract_mismatch')
        else:
            # A failed wait is a signed failed attempt, even while the current
            # index contract cannot be read. Never invent an engine identity
            # or let the previous successful attempt remain the high-water.
            try:
                engine = _engine(runtime)
            except (ValueError, RuntimeError, TypeError):
                engine = {}
        agents = []
        for entry in policy['agents']:
            if not barrier_ok:
                agents.append(dict(scope=entry['scope'], source_ids=entry['source_ids'], sample_count=0,
                    pass_count=0, accepted=False, samples=[],
                    blocked_reason='controlled_acceptance_candidate_readiness_unverified'))
                continue
            try:
                agents.append(_evaluate_agent(runtime, entry, policy, cutoff))
            except (ValueError, RuntimeError, TypeError) as exc:
                code = str(exc)
                agents.append(dict(scope=entry['scope'], source_ids=entry['source_ids'], sample_count=0,
                    pass_count=0, accepted=False, samples=[], blocked_reason=code if
                    re.fullmatch(r'controlled_acceptance_[a-z_]+', code) else 'controlled_acceptance_native_run_failed'))
        if load_controlled_policy(runtime, owner) != policy or _release(runtime, owner) != (release, receipt):
            _error('authority_changed_during_run')
        if barrier_ok and _engine(runtime) != engine:
            _error('engine_changed_during_run')
        accepted = all(entry['accepted'] for entry in agents)
        report_schema = _schema_for_policy(policy)
        persist_owner = _report_owner_scope(owner, policy, receipt)
        body = dict(schema=report_schema, mode=MODE, evidence_class=EVIDENCE_CLASS, source=SOURCE,
            record_id='cpa_' + uuid4().hex, scope=asdict(persist_owner), policy_digest=digest(policy),
            request_digest=policy['request_digest'], release_identity=release, release_reference=receipt,
            engine_identity=engine, started_at=started, completed_at=now_iso(), stable_before=cutoff,
            candidate_readiness_barrier=barrier,
            projection_acceptance_restore=projection_restore,
            accepted=accepted, agents=agents, certifies_natural_queries=False, certifies_no_answer=False,
            host_delivery_certified=False, memory_contribution_certified=False, natural_samples_created=0,
            blocked_reason='' if accepted else ('controlled_acceptance_candidate_readiness_unverified'
                if not barrier_ok else 'controlled_acceptance_case_failed'))
        body['acceptance_purpose'] = policy.get('purpose', 'operational')
        body['business_qualification'] = historical.business_qualification(body)
        content = seal(body, report_schema)
        if persist:
            record = RecordEnvelope.create(kind='evaluation_packet', title=('Verified historical business-memory qualification' if policy.get('purpose') == historical.PURPOSE
                       else 'Controlled existing-memory operational acceptance'),
                summary=('Existing real business memories qualified by native controlled recall.' if policy.get('purpose') == historical.PURPOSE
                         else 'Operator-authorized known-item retrieval; natural evidence remains separate.'),
                content=content, scope=persist_owner, source=SOURCE, source_id='default',
                meta={'report_type': report_schema, 'release_key': digest(release), 'policy_digest': digest(policy),
                      'report_digest': digest(content)})
            record.record_id = body['record_id']
            def append(sqlite):
                _validate_versions(sqlite, agents)
                sqlite.upsert(record, commit=False)
                return None, [record], []
            runtime.store.mutate_records_atomically(append)
        return _public(body, persisted=persist)
    except (ValueError, RuntimeError, TypeError) as exc:
        reason = str(exc)
        return _unavailable(reason if re.fullmatch(r'controlled_acceptance_[a-z_]+', reason)
                            else 'controlled_acceptance_runtime_unavailable')


def _latest(runtime, owner, release):
    return runtime.store.read_consistent(lambda sqlite: _latest_sqlite(sqlite, owner))


def _latest_sqlite(sqlite, owner):
    # Mutable envelope status/metadata must never hide a newer failed attempt.
    # Release, active status and report metadata are checked after selection.
    row = sqlite.execute('SELECT record_id,source_id FROM records WHERE tenant_id=? AND agent_id=? '
        'AND workspace_id=? AND user_id=? AND (source=? OR record_id LIKE ? ESCAPE char(92)) '
        'ORDER BY rowid DESC LIMIT 1',
        (owner.tenant_id, owner.agent_id, owner.workspace_id, owner.user_id, SOURCE, 'cpa\\_%')).fetchone()
    if row is None:
        return None
    record = sqlite.get_by_exact_ref(row['record_id'], scope=owner, source_id=row['source_id'])
    if record is None:
        _error('high_water_invalid')
    return record


def verify_controlled_production_acceptance(runtime, *, scope, release=None, record_id='', limit=500):
    """Recheck policy, current high-water, signature, physical versions and age."""
    owner = _scope(scope)
    try:
        policy = load_controlled_policy(runtime, owner)
        if policy is None:
            return _unavailable('controlled_acceptance_not_configured')
        current, receipt = _release(runtime, owner)
        if release is not None:
            requested = release_identity_payload(release) if hasattr(release, 'commit') else release
            if requested != current:
                _error('release_mismatch')
        report_owner = _report_owner_scope(owner, policy, receipt)
        record = _latest(runtime, report_owner, current)
        if record is None or (record_id and record.record_id != record_id):
            _error('current_report_missing')
        body = deepcopy(record.content)
        report_schema = _schema_for_policy(policy)
        if (record.kind != 'evaluation_packet' or record.source != SOURCE or record.source_id != 'default'
                or record.status != 'active' or record.scope != report_owner or not verify(body, report_schema)
                or body.get('record_id') != record.record_id or body.get('source') != SOURCE
                or body.get('scope') != asdict(report_owner) or body.get('schema') != report_schema
                or body.get('mode') != MODE or body.get('evidence_class') != EVIDENCE_CLASS
                or body.get('policy_digest') != digest(policy) or body.get('request_digest') != policy['request_digest']
                or body.get('release_identity') != current or body.get('release_reference') != receipt
                or record.meta.get('report_digest') != digest(body) or record.meta.get('report_type') != report_schema
                or record.meta.get('policy_digest') != digest(policy) or body.get('engine_identity') != _engine(runtime)
                or record.meta.get('release_key') != digest(current)
                or digest(body.get('engine_identity')) != policy['engine_contract_digest']
                or any(body.get(k) is not False for k in ('certifies_natural_queries', 'certifies_no_answer',
                    'host_delivery_certified', 'memory_contribution_certified'))
                or type(body.get('natural_samples_created')) is not int or body['natural_samples_created'] != 0):
            _error('report_authority_invalid')
        if (body.get('acceptance_purpose', 'operational') != policy.get('purpose', 'operational')
                or body.get('business_qualification', {}) != historical.business_qualification(body)):
            _error('business_qualification_invalid')
        postgres = body['engine_identity']['candidate_source'].get('candidate_source_type') == 'PostgresVectorCandidateSource'
        if 'candidate_readiness_barrier' in body or postgres and _requires_candidate_barrier(current):
            if (postgres and not isinstance(runtime.memory.recall_engine.candidate_source, PostgresVectorCandidateSource)
                    or not candidate_readiness_barrier_ok(body.get('candidate_readiness_barrier'), postgres=postgres,
                        source=runtime.memory.recall_engine.candidate_source)):
                _error('candidate_readiness_unverified')
        age = (datetime.now(timezone.utc) - _instant(body.get('completed_at'))).total_seconds()
        if age < 0 or age > policy['freshness_seconds'] or _instant(body['started_at']) > _instant(body['completed_at']):
            _error('report_expired')
        if _instant(body['stable_before']) > _instant(policy['stable_before']):
            _error('source_cutoff_invalid')
        entries = body.get('agents')
        if not isinstance(entries, list) or len(entries) != len(policy['agents']):
            _error('agent_evidence_invalid')
        for entry, allowed in zip(entries, policy['agents']):
            exact, sources = _scope(allowed['scope']), tuple(sorted(allowed['source_ids']))
            samples = entry.get('samples')
            if (entry.get('scope') != allowed['scope'] or entry.get('source_ids') != allowed['source_ids']
                    or not isinstance(samples, list) or not policy['minimum_cases_per_scope'] <= len(samples) <= policy['max_cases_per_scope']
                    or type(entry.get('sample_count')) is not int or entry['sample_count'] != len(samples)
                    or type(entry.get('pass_count')) is not int or entry['pass_count'] != len(samples)
                    or entry.get('accepted') is not True or entry.get('blocked_reason') != ''):
                _error('agent_evidence_invalid')
            seen = set()
            for sample in samples:
                binding = sample['source_binding']
                version = _physical(runtime, binding, exact, sources, body['stable_before'])
                record_ref = (binding['record_id'], binding['source_id'])
                if record_ref in seen or version != sample.get('source_version'):
                    _error('source_version_changed')
                seen.add(record_ref)
                if policy.get('purpose') == historical.PURPOSE:
                    provenance = historical.business_provenance(
                        runtime, binding, version, exact, sources, body['stable_before'], policy)
                    if provenance != sample.get('business_provenance'):
                        _error('business_origin_changed')
                source = runtime.store.get_by_exact_ref(binding['record_id'], scope=exact, source_id=binding['source_id'])
                case = benchmark._case(source)
                if (case is None or sample.get('case_id') != case['case_id']
                        or sample.get('query_digest') != digest(case['query'])
                        or sample.get('context_digest') != digest(case['task_context'])
                        or sample.get('passed') is not True or sample.get('complete') is not True
                        or type(sample.get('call_count')) is not int or sample['call_count'] != 1
                        or sample.get('failure_reason') != ''
                        or re.fullmatch('[0-9a-f]{64}', str(sample.get('compact_digest', ''))) is None
                        or list(record_ref) not in sample.get('compact_refs', [])):
                    _error('case_evidence_invalid')
                state = sample['state']
                proxy = SimpleNamespace(explanation={'retrieval_status': state['retrieval_status'],
                    'relevance_selector': {'status': state['status'], 'collection_complete': state['collection_complete']},
                    'engine_diagnostics': {'source_searches': state['source_searches'],
                        'source_budget_exhausted': state['source_budget_exhausted'], 'fallback': state['fallback'],
                        'fallback_error_codes': state.get('fallback_error_codes'),
                        'drops': {'recall_budget_exhausted': state['recall_budget_exhausted']}}})
                if not _complete(proxy)[1]:
                    _error('retrieval_incomplete')
                returned, versions = sample['returned'], sample['returned_versions']
                if not isinstance(returned, list) or len(returned) != len(versions) or not returned:
                    _error('returned_evidence_invalid')
                if any(ref not in [[binding['record_id'], binding['source_id']] for binding in returned]
                       for ref in sample['compact_refs']):
                    _error('compact_result_boundary_invalid')
                for ref, expected in zip(returned, versions):
                    if _physical(runtime, ref, exact, sources, body['stable_before'], require_old=False) != expected:
                        _error('source_version_changed')
                cascade = sample.get('cascade_evidence')
                if not isinstance(cascade, list) or len(cascade) > 2:
                    _error('cascade_evidence_invalid')
                primary = [runtime.store.get_by_exact_ref(ref['record_id'], scope=exact, source_id=ref['source_id'])
                           for ref in returned if [ref['record_id'], ref['source_id']] in sample['compact_refs']]
                summaries = []
                for item in cascade:
                    binding = item['binding']
                    if (_physical(runtime, binding, exact, sources, body['stable_before'], require_old=False,
                                  episode=True) != item.get('version')):
                        _error('source_version_changed')
                    parent = runtime.store.get_by_exact_ref(binding['record_id'], scope=exact, source_id=binding['source_id'])
                    summary = runtime.memory._selected_record_summaries([parent])[0]
                    if item.get('summary_digest') != digest(summary):
                        _error('cascade_evidence_summary_changed')
                    summaries.append(summary)
                if _cascade_evidence(runtime, {'evidence': summaries}, exact, sources,
                        body['stable_before'], primary) != cascade:
                    _error('cascade_evidence_parent_invalid')
        if body.get('accepted') is not True or body.get('blocked_reason') != '':
            _error('report_not_accepted')
        def final_snapshot(sqlite):
            _validate_versions(sqlite, entries)
            latest = _latest_sqlite(sqlite, report_owner)
            if latest is None or latest.record_id != record.record_id or digest(latest.content) != digest(body):
                _error('high_water_changed')
        runtime.store.read_consistent(final_snapshot)
        if load_controlled_policy(runtime, owner) != policy or _release(runtime, owner) != (current, receipt):
            _error('authority_changed_during_verification')
        return _public(body, persisted=True)
    except (ValueError, RuntimeError, TypeError, KeyError, AttributeError) as exc:
        reason = str(exc)
        return _unavailable(reason if re.fullmatch(r'controlled_acceptance_[a-z_]+', reason)
                            else 'controlled_acceptance_verification_failed')
