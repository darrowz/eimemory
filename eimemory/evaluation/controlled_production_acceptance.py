"""Operator-authorized existing-memory operational acceptance.

This separate certificate proves bounded current known-item retrieval. It never
creates natural queries, relevance gold, host delivery or natural quality.
Authorization comes only from a protected root policy; reports cannot grant it.
"""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import os
from pathlib import Path
import re
from types import SimpleNamespace
from uuid import uuid4

from eimemory import identity_authority
from eimemory.core import strict_json
from eimemory.core.clock import now_iso
from eimemory.evaluation import controlled_memory_benchmark as benchmark
from eimemory.governance.learning.effect_policy import canonical, digest, seal, verify
from eimemory.governance.release.evidence_contract import current_release_identity, release_identity_payload
from eimemory.identity_ops import _repair_snapshot, _RepairSkipped
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.models.source_partitions import normalize_source_ids

SCHEMA = 'controlled_production_acceptance.v1'
POLICY_SCHEMA = 'controlled_production_acceptance_policy.v1'
MODE = 'controlled_existing_memory'
SOURCE = 'eimemory.evaluation.controlled_production_acceptance'
EVIDENCE_CLASS = 'operator_controlled_existing_memory'
POLICY_ENV = 'EIMEMORY_CONTROLLED_PRODUCTION_ACCEPTANCE_POLICY'
DEFAULT_POLICY = '/etc/eimemory-controlled-production-acceptance/policy.json'
_SCOPE = {'tenant_id', 'agent_id', 'workspace_id', 'user_id'}


class ControlledAcceptanceError(ValueError):
    pass


def _error(code):
    raise ControlledAcceptanceError('controlled_acceptance_' + code)


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
    if (not isinstance(value, dict) or set(value) != fields
            or value.get('schema') != POLICY_SCHEMA or value.get('mode') != MODE
            or re.fullmatch('[0-9a-f]{64}', str(value.get('request_digest', ''))) is None
            or value.get('request_digest') == '0' * 64 or _scope(value.get('owner_scope')) != owner):
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
    if not isinstance(entries, list) or not 1 <= len(entries) <= 16:
        _error('policy_agents_invalid')
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {'scope', 'source_ids'}:
            _error('policy_agents_invalid')
        exact = _scope(entry['scope'])
        sources = entry['source_ids']
        if (exact.tenant_id != owner.tenant_id or exact.agent_id in seen
                or not exact.workspace_id.endswith('::channel::hermes')
                or not isinstance(sources, list) or not sources or len(sources) > 16
                or any(not isinstance(s, str) or not s or s.strip() != s or '*' in s for s in sources)
                or len(set(sources)) != len(sources) or tuple(sorted(sources)) != normalize_source_ids(sources)):
            _error('policy_agents_invalid')
        seen.add(exact.agent_id)
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
        try:
            state = runtime.memory.recall_engine.candidate_source.repository.read_index_state()
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
                     *zip(sample['returned'], sample['returned_versions'])]
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


def _physical(runtime, binding, scope, sources, cutoff, *, require_old=True):
    if (not isinstance(binding, dict) or binding.get('scope') != asdict(scope)
            or binding.get('source_id') not in sources):
        _error('source_boundary_invalid')
    ref = (*[getattr(scope, k) for k in ('tenant_id', 'agent_id', 'workspace_id', 'user_id')],
           binding['source_id'], str(binding.get('record_id') or ''))
    try:
        record, version = runtime.store.read_consistent(lambda sqlite: _repair_snapshot(sqlite, ref))
    except (_RepairSkipped, ValueError, RuntimeError):
        _error('source_version_unavailable')
    if (benchmark._eligible_reason(record, scope, sources)
            or (require_old and benchmark._stable_source_reason(record, _instant(cutoff)))
            or benchmark._binding(record) != binding):
        _error('source_version_changed')
    from eimemory.subject_projection_consumption import projection_is_current
    if not projection_is_current(runtime.store, record):
        _error('source_authority_unavailable')
    return list(version)


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
                 if type(engine.get('fallback')) is bool else None)
    good = (state['collection_complete'] is True and state['status'] == 'evidence_found'
            and state['retrieval_status'] == 'evidence_found'
            and type(state['source_searches']) is int and state['source_searches'] > 0
            and all(type(state[k]) is int and state[k] == 0 for k in
                    ('source_budget_exhausted', 'recall_budget_exhausted')) and state['fallback'] is False)
    return state, good


def _evaluate_agent(runtime, entry, policy, cutoff):
    scope, sources = _scope(entry['scope']), tuple(sorted(entry['source_ids']))
    dataset = benchmark.build_controlled_memory_benchmark(runtime, scope=scope, source_ids=sources,
        stable_before=cutoff, limit=policy['max_cases_per_scope'])
    cases = dataset.get('cases', [])
    if len(cases) < policy['minimum_cases_per_scope']:
        _error('insufficient_existing_records')
    before = {case['case_id']: _physical(runtime, case['source_binding'], scope, sources, cutoff) for case in cases}
    observations = []

    def recall(**kwargs):
        bundle = runtime.memory.recall(**kwargs)
        state, complete = _complete(bundle)
        returned = [*bundle.items, *bundle.rules, *bundle.reflections]
        if any(record.scope != scope or record.source_id not in sources for record in returned):
            _error('returned_boundary_invalid')
        bindings = [benchmark._binding(record) for record in returned]
        # The target must predate the authorized cutoff. Additional live
        # results may be newer, but retain identical scope/source/version gates.
        versions = [_physical(runtime, binding, scope, sources, cutoff, require_old=False) for binding in bindings]
        compact = bundle.to_compact_dict(limit=5, include_explanation=False)
        if (compact.get('retrieval_status') != 'evidence_found' or compact.get('evidence')
                or len(canonical(compact).encode()) > 16384):
            _error('compact_result_invalid')
        compact_refs = [[item.get('record_id'), item.get('source_id')] for item in compact.get('items', [])]
        observations.append(dict(state=state, complete=complete, returned=bindings,
                                 returned_versions=versions, compact_refs=compact_refs))
        return bundle

    proxy = SimpleNamespace(store=runtime.store,
        memory=SimpleNamespace(recall=recall, recall_engine=runtime.memory.recall_engine))
    report = benchmark.run_controlled_memory_benchmark(proxy, dataset, scope=scope, source_ids=sources)
    samples = report.get('samples', [])
    if len(observations) != len(cases) or len(samples) != len(cases):
        _error('native_execution_incomplete')
    evidence = []
    for case, sample, observation in zip(cases, samples, observations):
        binding = case['source_binding']
        version = _physical(runtime, binding, scope, sources, cutoff)
        if version != before[case['case_id']]:
            _error('source_version_changed')
        hit = [binding['record_id'], binding['source_id']] in observation['compact_refs']
        passed = sample.get('passed') is True and sample.get('evaluable') is True and observation['complete'] and hit
        evidence.append(dict(case_id=case['case_id'], source_binding=binding, source_version=version,
            query_digest=digest(case['query']), context_digest=digest(case['task_context']),
            passed=passed, expected_rank=sample.get('expected_rank'), **observation))
    accepted = report.get('controlled_benchmark_passed_threshold') is True and all(s['passed'] for s in evidence)
    return dict(scope=asdict(scope), source_ids=list(sources), sample_count=len(evidence),
        pass_count=sum(s['passed'] for s in evidence), accepted=accepted,
        blocked_reason='' if accepted else 'controlled_acceptance_case_failed', samples=evidence)


def _public(body, *, persisted=False):
    agents = []
    for entry in body.get('agents', []):
        agents.append(dict(agent_id=entry['scope']['agent_id'], scope_digest=digest(entry['scope']),
            source_ids=entry['source_ids'], sample_count=entry['sample_count'], pass_count=entry['pass_count'],
            fail_count=entry['sample_count'] - entry['pass_count'], accepted=entry['accepted'],
            complete=bool(entry['samples']) and all(s['complete'] for s in entry['samples']), blocked_reason=entry['blocked_reason']))
    passed = body.get('accepted') is True
    accepted = passed and persisted
    return dict(ok=passed, accepted=accepted, status='accepted' if accepted else 'evaluated' if passed else 'failed',
        passed_threshold=passed,
        schema=SCHEMA, mode=MODE, evidence_class=EVIDENCE_CLASS,
        policy_digest=body.get('policy_digest', ''), release_identity=body.get('release_identity', {}),
        record_id=body.get('record_id', '') if persisted else '', persisted=persisted,
        certifies_natural_queries=False, certifies_no_answer=False, host_delivery_certified=False,
        memory_contribution_certified=False, natural_samples_created=0, agents=agents,
        blocked_reason=body.get('blocked_reason', ''))


def _unavailable(reason):
    return {**_public({}), 'status': 'unavailable', 'blocked_reason': reason}


def run_controlled_production_acceptance(runtime, *, scope, persist=True, stable_before=''):
    """Perform native seedless calls only; arbitrary report import is unsupported."""
    owner = _scope(scope)
    if type(persist) is not bool:
        _error('persist_invalid')
    policy = load_controlled_policy(runtime, owner)
    if policy is None:
        return {**_unavailable('controlled_acceptance_not_configured'), 'status': 'not_configured'}
    try:
        release, receipt = _release(runtime, owner)
        engine = _engine(runtime)
        if digest(engine) != policy['engine_contract_digest']:
            _error('production_engine_contract_mismatch')
        cutoff = policy['stable_before']
        if stable_before and _instant(stable_before) < _instant(cutoff):
            cutoff = stable_before
        started = now_iso()
        agents = []
        for entry in policy['agents']:
            try:
                agents.append(_evaluate_agent(runtime, entry, policy, cutoff))
            except (ValueError, RuntimeError, TypeError) as exc:
                code = str(exc)
                agents.append(dict(scope=entry['scope'], source_ids=entry['source_ids'], sample_count=0,
                    pass_count=0, accepted=False, samples=[], blocked_reason=code if
                    re.fullmatch(r'controlled_acceptance_[a-z_]+', code) else 'controlled_acceptance_native_run_failed'))
        if load_controlled_policy(runtime, owner) != policy or _release(runtime, owner) != (release, receipt):
            _error('authority_changed_during_run')
        if _engine(runtime) != engine:
            _error('engine_changed_during_run')
        accepted = all(entry['accepted'] for entry in agents)
        body = dict(schema=SCHEMA, mode=MODE, evidence_class=EVIDENCE_CLASS, source=SOURCE,
            record_id='cpa_' + uuid4().hex, scope=asdict(owner), policy_digest=digest(policy),
            request_digest=policy['request_digest'], release_identity=release, release_reference=receipt,
            engine_identity=engine, started_at=started, completed_at=now_iso(), stable_before=cutoff,
            accepted=accepted, agents=agents, certifies_natural_queries=False, certifies_no_answer=False,
            host_delivery_certified=False, memory_contribution_certified=False, natural_samples_created=0,
            blocked_reason='' if accepted else 'controlled_acceptance_case_failed')
        content = seal(body, SCHEMA)
        if persist:
            record = RecordEnvelope.create(kind='evaluation_packet', title='Controlled existing-memory operational acceptance',
                summary='Operator-authorized known-item retrieval; natural evidence remains separate.',
                content=content, scope=owner, source=SOURCE, source_id='default',
                meta={'report_type': SCHEMA, 'release_key': digest(release), 'policy_digest': digest(policy),
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
        record = _latest(runtime, owner, current)
        if record is None or (record_id and record.record_id != record_id):
            _error('current_report_missing')
        body = deepcopy(record.content)
        if (record.kind != 'evaluation_packet' or record.source != SOURCE or record.source_id != 'default'
                or record.status != 'active' or record.scope != owner or not verify(body, SCHEMA)
                or body.get('record_id') != record.record_id or body.get('source') != SOURCE
                or body.get('scope') != asdict(owner) or body.get('schema') != SCHEMA
                or body.get('mode') != MODE or body.get('evidence_class') != EVIDENCE_CLASS
                or body.get('policy_digest') != digest(policy) or body.get('request_digest') != policy['request_digest']
                or body.get('release_identity') != current or body.get('release_reference') != receipt
                or record.meta.get('report_digest') != digest(body) or record.meta.get('report_type') != SCHEMA
                or record.meta.get('policy_digest') != digest(policy) or body.get('engine_identity') != _engine(runtime)
                or record.meta.get('release_key') != digest(current)
                or digest(body.get('engine_identity')) != policy['engine_contract_digest']
                or any(body.get(k) is not False for k in ('certifies_natural_queries', 'certifies_no_answer',
                    'host_delivery_certified', 'memory_contribution_certified'))
                or type(body.get('natural_samples_created')) is not int or body['natural_samples_created'] != 0):
            _error('report_authority_invalid')
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
                source = runtime.store.get_by_exact_ref(binding['record_id'], scope=exact, source_id=binding['source_id'])
                case = benchmark._case(source)
                if (case is None or sample.get('case_id') != case['case_id']
                        or sample.get('query_digest') != digest(case['query'])
                        or sample.get('context_digest') != digest(case['task_context'])
                        or sample.get('passed') is not True or sample.get('complete') is not True
                        or list(record_ref) not in sample.get('compact_refs', [])):
                    _error('case_evidence_invalid')
                state = sample['state']
                proxy = SimpleNamespace(explanation={'retrieval_status': state['retrieval_status'],
                    'relevance_selector': {'status': state['status'], 'collection_complete': state['collection_complete']},
                    'engine_diagnostics': {'source_searches': state['source_searches'],
                        'source_budget_exhausted': state['source_budget_exhausted'], 'fallback': state['fallback'],
                        'drops': {'recall_budget_exhausted': state['recall_budget_exhausted']}}})
                if not _complete(proxy)[1]:
                    _error('retrieval_incomplete')
                returned, versions = sample['returned'], sample['returned_versions']
                if not isinstance(returned, list) or len(returned) != len(versions) or not returned:
                    _error('returned_evidence_invalid')
                for ref, expected in zip(returned, versions):
                    if _physical(runtime, ref, exact, sources, body['stable_before'], require_old=False) != expected:
                        _error('source_version_changed')
        if body.get('accepted') is not True or body.get('blocked_reason') != '':
            _error('report_not_accepted')
        def final_snapshot(sqlite):
            _validate_versions(sqlite, entries)
            latest = _latest_sqlite(sqlite, owner)
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
