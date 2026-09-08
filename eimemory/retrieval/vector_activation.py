"""Technical proof for enabling PostgreSQL reads; explicitly not a quality report."""
from dataclasses import asdict
from hashlib import sha256
from time import perf_counter

from eimemory.core.clock import now_iso
from eimemory.governance.evidence_contract import same_scope
from eimemory.models.records import ScopeRef
from .postgres_vector import PostgresVectorCandidateSource, embedding_provider_fingerprint, projection_fingerprint
from .sqlite_source import SQLiteCandidateSource


def prove_vector_reads(runtime, *, probes):
    if not isinstance(probes, list) or not 1 <= len(probes) <= 10:
        raise ValueError('vector_activation_probes_invalid')
    source = runtime.memory.recall_engine.candidate_source
    if not isinstance(source, PostgresVectorCandidateSource) or not source.config.enabled:
        raise ValueError('postgres_reads_not_enabled')
    config = source.config
    authority = SQLiteCandidateSource(runtime.store, projection_memory_only=config.projection_memory_only)
    before = source.repository.read_index_state()
    if (not before.ready or not before.watermark
            or before.authority_revision != authority.authority_revision()
            or before.embedding_fingerprint != embedding_provider_fingerprint(source.embedding_provider, config)
            or before.projection_fingerprint != projection_fingerprint(config)):
        raise ValueError('vector_activation_index_not_current')
    prepared = []
    for probe in probes:
        if not isinstance(probe, dict) or set(probe) != {'query','scope','source_id'}:
            raise ValueError('vector_activation_probe_invalid')
        if not isinstance(probe['query'], str) or not 1 <= len(probe['query'].strip()) <= 8000:
            raise ValueError('vector_activation_query_invalid')
        if not isinstance(probe['source_id'], str) or not probe['source_id'] or probe['source_id'] == '*':
            raise ValueError('vector_activation_source_invalid')
        prepared.append((probe, ScopeRef.from_dict(probe['scope'])))
    samples = []
    for probe, scope in prepared:
        started = perf_counter()
        bundle = runtime.memory.recall(query=probe['query'], scope=asdict(scope), limit=5,
            task_context={'exact_scope_only':True, 'source_ids':[probe['source_id']],
                          'target_source_id':probe['source_id']})
        pg = source.effective_identity()['postgres']
        if pg['state'] != 'available' or not pg['query_valid'] or not pg['index_verified']:
            raise ValueError('vector_activation_read_fell_back')
        if any(not same_scope(record.scope, scope) or record.source_id != probe['source_id'] for record in bundle.items):
            raise ValueError('vector_activation_boundary_violation')
        if bundle.explanation.get('retrieval_status') == 'unavailable':
            raise ValueError('vector_activation_retrieval_unavailable')
        samples.append({'query_digest':sha256(probe['query'].encode()).hexdigest(),
            'scope':asdict(scope), 'source_id':probe['source_id'],
            'result_refs':[record.record_id for record in bundle.items],
            'latency_ms':round((perf_counter()-started)*1000,3), 'postgres_read_verified':True})
    after = source.repository.read_index_state()
    if (after.watermark, after.authority_revision) != (before.watermark, before.authority_revision):
        raise ValueError('vector_activation_generation_changed')
    if after.authority_revision != authority.authority_revision():
        raise ValueError('vector_activation_authority_changed')
    return {'ok':True, 'schema':'vector_read_activation_proof.v1', 'created_at':now_iso(),
        'quality_gate_passed':False, 'proof_role':'technical_read_activation_only',
        'table':config.table, 'schema_name':config.schema, 'watermark':after.watermark,
        'authority_revision':after.authority_revision, 'embedding_fingerprint':after.embedding_fingerprint,
        'projection_fingerprint':after.projection_fingerprint,
        'engine_identity':runtime.memory.recall_engine.effective_identity(), 'samples':samples}
