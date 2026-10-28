from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from types import SimpleNamespace

import pytest

from eimemory.contracts.code_incident_coverage import (
    CHECKPOINT_SCHEMA, OBSERVATION_SCHEMA, canonical, digest, validate_diagnostic,
)
from eimemory.governance.evolution.code_evolution_incident_coverage import (
    checkpoint_native_ingress, observe_native_incident, read_native_ingress_coverage, producer_identity,
)
from eimemory.governance.learning.effect_policy import seal, verify
from eimemory.governance.l5.l5_product_completion import build_product_completion
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.ops.release_closure_failure import record_release_closure_failure
from eimemory.storage.code_incident_coverage_store import CodeIncidentCoverageStore
from eimemory.storage.migrations.code_incident_coverage import (
    JOURNAL, CHECKPOINTS, OBSERVATIONS, ensure_schema, schema_ready, trigger_definitions,
)
from eimemory.storage.runtime_store import RuntimeStore

SCOPE = ScopeRef(tenant_id='default', agent_id='coverage-native', workspace_id='workspace', user_id='owner')
COMMIT = 'a' * 40


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv('EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY', 'coverage-fixture-key-' + 'a' * 64)
    # Runtime-only coverage fixtures have no authorized provider roster.
    monkeypatch.setattr('eimemory.governance.evolution.code_evolution_provider_authority.DELEGATION_PATH',
                        tmp_path / 'absent-authority' / 'code-provider-delegations.v1.json')
    store = RuntimeStore(tmp_path / 'runtime')
    try:
        yield SimpleNamespace(store=store)
    finally:
        store.close()


def incident(runtime, *, scope=SCOPE, reason='release_lineage_not_compatible', detected_at=None, adverse=None):
    report = dict(report_type='l5_release_closure', ok=False, closure_complete=False, data_accumulating=False,
        blocked_stage='release_lineage', blocked_reason=reason,
        deployment=dict(commit=COMMIT, version='1.14.154'),
        release_lineage=dict(ok=True, compatible=False, unknown_production_paths=[]))
    if adverse:
        report['source_provenance'] = adverse
    return record_release_closure_failure(runtime, scope=scope, closure_report=report,
        detected_at=detected_at or datetime.now(timezone.utc).isoformat())


def connection(runtime):
    return runtime.store.sqlite.conn


def test_real_producer_checkpoint_signature_restart_and_global_unknown(runtime):
    checkpoint = checkpoint_native_ingress(runtime, scope=SCOPE)
    assert verify(checkpoint, CHECKPOINT_SCHEMA) and checkpoint['continuity'] is False
    actual = incident(runtime)
    observation = actual['incident_coverage']
    assert verify(observation, OBSERVATION_SCHEMA)
    assert observation['covered_known_before_detection'] is False
    assert observation['covered_prior_user_reported'] is False
    assert observation['global_facts'] == dict(known_before_detection=None, prior_user_reported=None, manual_bootstrap=None)
    assert observation['external_prior_knowledge'] is None and observation['backend_coverage']['postgres'] is None
    assert observation['qualifies_for_product_completion'] is False
    assert all(actual[field] is None for field in observation['global_facts'])
    assert read_native_ingress_coverage(runtime, scope=SCOPE, record_id=actual['incident_record_id']) == observation
    root = runtime.store.root
    runtime.store.close()
    runtime.store = RuntimeStore(root)
    assert read_native_ingress_coverage(runtime, scope=SCOPE, record_id=actual['incident_record_id']) == observation


def test_bounded_proof_never_satisfies_original_l5_global_gates(runtime):
    checkpoint_native_ingress(runtime, scope=SCOPE)
    observation = incident(runtime)['incident_coverage']
    transaction = dict(transaction_id='fixture-terminal', terminal_receipt_digest='b' * 64,
        qualifying_terminal_outcome='succeeded_sedimented', evidence_verified=True, origin='system_detector',
        observation_valid=True, **observation['global_facts'], incident_coverage=observation)
    result = build_product_completion(dict(ok=True, status='ready'), provider=dict(ready=True, catalog_ready=True, advertisement_fresh=True),
        transaction=transaction, current_lineage=dict(ok=True, compatible=True))
    assert result['product_l5_complete'] is False
    assert {'incident_prior_knowledge_unproven', 'incident_not_user_reported_unproven', 'incident_not_manual_bootstrap_unproven'} <= set(result['gaps'])
    with pytest.raises(ValueError, match='global_negative_forbidden'):
        validate_diagnostic({**observation, 'global_facts': {**observation['global_facts'], 'prior_user_reported': False}}, schema=OBSERVATION_SCHEMA, scope=asdict(SCOPE))


def test_no_checkpoint_or_pre_anchor_incident_never_gains_absence(runtime):
    before = incident(runtime)
    assert before['incident_coverage']['reason'] == 'actual_checkpoint_missing'
    checkpoint_native_ingress(runtime, scope=SCOPE)
    result = observe_native_incident(runtime, scope=SCOPE, record_id=before['incident_record_id'])
    assert result['reason'] == 'incident_precedes_actual_coverage_epoch'
    assert result['covered_known_before_detection'] is None


def test_pre_anchor_source_time_and_unclassified_free_text_are_not_absence(runtime):
    checkpoint_native_ingress(runtime, scope=SCOPE)
    record = RecordEnvelope.create(kind='memory', source='hermes', scope=SCOPE, title='real unclassified input', content={'text': 'a failure was mentioned'})
    record.time.occurred_at = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    runtime.store.append(record)
    observation = incident(runtime)['incident_coverage']
    assert {'pre_anchor_source_received', 'unclassified_input_present'} <= set(observation['reasons'])
    assert observation['covered_known_before_detection'] is None and observation['covered_prior_user_reported'] is None


def test_actual_existing_user_report_entry_keeps_adverse_facts(runtime, monkeypatch):
    from eimemory.governance.evolution.code_maintenance import record_code_maintenance
    monkeypatch.setattr('eimemory.governance.evolution.code_maintenance._repository_context',
        lambda *_args: dict(ok=True, base_commit=COMMIT, repository_root=str(runtime.store.root)))
    checkpoint_native_ingress(runtime, scope=SCOPE)
    actual = record_code_maintenance(runtime, scope=SCOPE, base_commit=COMMIT, title='known real repair',
        summary='a user supplied this repair', evidence=['original issue'], repo_root=runtime.store.root)
    assert actual['known_before_detection'] is True and actual['prior_user_reported'] is True
    journal = connection(runtime).execute(f'SELECT * FROM {JOURNAL} WHERE record_id=?', (actual['record_id'],)).fetchone()
    assert journal['human_input'] == 1 and journal['origin'] == 'user_reported'
    report = incident(runtime, adverse=dict(known_before_detection=True, prior_user_reported=True, manual_bootstrap=True))
    assert report['incident_coverage']['global_facts'] == dict(known_before_detection=True, prior_user_reported=True, manual_bootstrap=True)
    assert report['incident_coverage']['covered_known_before_detection'] is True
    assert report['incident_coverage']['covered_prior_user_reported'] is True


def test_late_ingress_invalidates_signed_previous_observation(runtime):
    checkpoint_native_ingress(runtime, scope=SCOPE)
    actual = incident(runtime)
    original = actual['incident_coverage']
    record = RecordEnvelope.create(kind='memory', source='hermes', scope=SCOPE, title='late prior input')
    record.time.occurred_at = actual['detected_at']
    runtime.store.append(record)
    result = read_native_ingress_coverage(runtime, scope=SCOPE, record_id=actual['incident_record_id'])
    assert result['status'] == 'unproven'
    assert result['covered_prior_user_reported'] is None
    assert original['covered_prior_user_reported'] is False  # historical bytes are retained


@pytest.mark.parametrize('progress', ['future_ingress', 'future_ingress_checkpoint', 'checkpoint_only'])
def test_safe_future_ingress_preserves_original_signed_historical_prefix(runtime, monkeypatch, progress):
    checkpoint_native_ingress(runtime, scope=SCOPE)
    actual = incident(runtime)
    original = actual['incident_coverage']
    raw = connection(runtime).execute(f'SELECT payload_json FROM {OBSERVATIONS} WHERE record_id=?',
                                     (actual['incident_record_id'],)).fetchone()[0]
    if progress != 'checkpoint_only':
        later = RecordEnvelope.create(kind='reflection', source='eimemory.scheduler.step_evidence',
            scope=SCOPE, title='actual later diagnostic', content={'status': 'observed'})
        later.time.occurred_at = datetime.now(timezone.utc).isoformat()
        runtime.store.append(later)
    if progress != 'future_ingress':
        next_value = checkpoint_native_ingress(runtime, scope=SCOPE)
        assert next_value['epoch_id'] == original['epoch_id']
        assert digest(next_value) != original['checkpoint_digest']
    monkeypatch.setattr('eimemory.governance.evolution.code_evolution_incident_coverage.seal',
                        lambda *_a, **_k: pytest.fail('readback must not reseal an observation'))
    result = read_native_ingress_coverage(runtime, scope=SCOPE, record_id=actual['incident_record_id'])
    assert result == original and verify(result, OBSERVATION_SCHEMA)
    high_water = connection(runtime).execute(f'SELECT max(sequence) FROM {JOURNAL}').fetchone()[0]
    assert result['high_water'] == high_water if progress == 'checkpoint_only' else result['high_water'] < high_water
    assert connection(runtime).execute(f'SELECT payload_json FROM {OBSERVATIONS} WHERE record_id=?',
                                     (actual['incident_record_id'],)).fetchone()[0] == raw
    assert result['global_facts'] == dict.fromkeys(('known_before_detection', 'prior_user_reported', 'manual_bootstrap'))
    assert result['qualifies_for_product_completion'] is False


@pytest.mark.parametrize('danger', ['late', 'ambiguous_second', 'malformed_time', 'related', 'human', 'version', 'epoch', 'page_bound', 'prefix_metadata', 'prefix_hole'])
def test_new_ingress_never_hides_unsafe_changes_behind_a_signed_old_prefix(runtime, monkeypatch, danger):
    checkpoint_native_ingress(runtime, scope=SCOPE)
    actual = incident(runtime)
    original = actual['incident_coverage']
    original_raw = connection(runtime).execute(f'SELECT payload_json FROM {OBSERVATIONS} WHERE record_id=?',
                                              (actual['incident_record_id'],)).fetchone()[0]
    record = RecordEnvelope.create(kind='reflection', source='hermes', scope=SCOPE, title='later actual ingress')
    record.time.occurred_at = datetime.now(timezone.utc).isoformat()
    if danger == 'late': record.time.occurred_at = actual['detected_at']
    if danger == 'ambiguous_second':
        record.time.occurred_at = datetime.fromisoformat(actual['detected_at'].replace('Z', '+00:00')).replace(microsecond=0).isoformat()
    if danger == 'malformed_time': record.time.occurred_at = 'invalid-source-time'
    if danger == 'related':
        incident_record = runtime.store.get_by_id(actual['incident_record_id'], scope=SCOPE, exact_scope=True)
        record.content = dict(incident_record.content)
    if danger == 'human': record.provenance = {'origin': 'user_reported'}
    runtime.store.append(record)
    if danger == 'version':
        current = runtime.store.get_by_id(actual['incident_record_id'], scope=SCOPE, exact_scope=True)
        current.content['note'] = 'version changed after detection'
        runtime.store.rewrite(current)
    if danger == 'epoch':
        monkeypatch.setattr('eimemory.storage.code_incident_coverage_store.MAX_EPOCH_CHECKPOINTS', 1)
        assert checkpoint_native_ingress(runtime, scope=SCOPE)['epoch_id'] != original['epoch_id']
    if danger == 'page_bound':
        monkeypatch.setattr('eimemory.storage.code_incident_coverage_store.MAX_JOURNAL_PAGE', 1)
    if danger in {'prefix_metadata', 'prefix_hole'}:
        conn = connection(runtime)
        operation = 'update' if danger == 'prefix_metadata' else 'delete'
        guard = f'trg_{JOURNAL}_no_{operation}'
        conn.execute('DROP TRIGGER ' + guard)
        if operation == 'update':
            conn.execute(f"UPDATE {JOURNAL} SET source='changed-prefix' WHERE record_id=?", (actual['incident_record_id'],))
        else:
            conn.execute(f'DELETE FROM {JOURNAL} WHERE record_id=?', (actual['incident_record_id'],))
        conn.execute(trigger_definitions()[guard]); conn.commit()
    result = read_native_ingress_coverage(runtime, scope=SCOPE, record_id=actual['incident_record_id'])
    assert result['status'] == 'unproven' and result['covered_known_before_detection'] is None
    assert result['covered_prior_user_reported'] is None and result['qualifies_for_product_completion'] is False
    assert original['covered_known_before_detection'] is False
    assert connection(runtime).execute(f'SELECT payload_json FROM {OBSERVATIONS} WHERE record_id=?',
                                     (actual['incident_record_id'],)).fetchone()[0] == original_raw


def test_real_maintenance_observation_then_complete_report_archive_remains_readable_by_native_l5(runtime, monkeypatch):
    from eimemory.contracts.release_identity import ReleaseIdentity
    from eimemory.governance.learning.nightly_step_evidence import read_nightly_step_evidence
    from eimemory.ops.runtime_identity_drift import inspect_live_runtime_identity
    from eimemory.ops.system_code_repair_failure import detect_system_code_repair_failure
    from eimemory.scheduler.jobs import _run_archived_code_maintenance
    from eimemory.governance.l5 import l5_reader

    # Real native producer + SQLite persistence + maintenance collector + full
    # evidence writer and native envelope hook. Systemd/provider/repair effects
    # are isolated deterministic fixtures; no deployment or model is invoked.
    release = ReleaseIdentity(COMMIT, '1.14.155', 'fixture-receipt', 'fixture-session')
    monkeypatch.setattr('eimemory.governance.release.evidence_contract.current_release_identity', lambda *_a, **_k: release)
    monkeypatch.setenv('EIMEMORY_L5_LOOP_ENABLED', '1')
    monkeypatch.setenv('EIMEMORY_L5_LOOP_APPLY', '1')
    def inspect(value, **kwargs):
        return inspect_live_runtime_identity(value, **kwargs,
            runner=lambda unit: 'EIMEMORY_RUNTIME_COMMIT=' + 'b' * 40 if unit == 'eimemory-rpc.service' else None)
    monkeypatch.setattr('eimemory.ops.runtime_identity_drift.inspect_live_runtime_identity', inspect)
    monkeypatch.setattr('eimemory.governance.evolution.code_automation_policy_owner.ensure_native_incident_policy',
                        lambda *_a, **_k: {'ok': True, 'status': 'idle', 'processed': []})
    route = {'ok': True, 'status': 'idle', 'processed': []}
    monkeypatch.setattr('eimemory.governance.evolution.system_code_repair.process_system_code_incidents',
                        lambda *_a, **_k: dict(route))
    monkeypatch.setattr('eimemory.ops.system_code_repair_failure.record_system_code_repair_failure',
        lambda _runtime, **kwargs: {**detect_system_code_repair_failure(kwargs['repair_report'], policy={},
            release_commit=COMMIT, detected_at=kwargs['detected_at']), 'incident_record_id': ''})
    runtime.resume_code_evolution_transactions = lambda **_k: {'ok': True, 'reports': []}
    maintenance = _run_archived_code_maintenance(runtime, scope=asdict(SCOPE), run_id='fixture-native-maintenance')
    assert maintenance['ok'] is True and maintenance['status'] == 'observed'
    assert datetime.fromisoformat(maintenance['detected_at'].replace('Z', '+00:00')) >= datetime.fromisoformat(
        maintenance['incident_coverage']['checkpoint']['observed_at'])
    actual_id = maintenance['runtime_identity']['incident_record_id']
    observation = maintenance['incident_coverage']['observations'][0]
    assert 'record_id' in observation, observation
    assert observation['record_id'] == actual_id and verify(observation, OBSERVATION_SCHEMA)
    archived = read_nightly_step_evidence(runtime, scope=SCOPE, run_id='fixture-native-maintenance',
        step='code_evolution', reference=maintenance['evidence_archive'])
    assert archived.content['step_report']['incident_coverage']['observations'][0] == observation
    assert connection(runtime).execute(f'SELECT max(sequence) FROM {JOURNAL}').fetchone()[0] > observation['high_water']
    monkeypatch.setattr(l5_reader, '_code_evolution_evidence', lambda *_a, **_k: (
        {'ready': False}, {'incident_id': actual_id, 'known_before_detection': None,
                         'prior_user_reported': None, 'manual_bootstrap': None}, {'ok': False, 'compatible': False}))
    envelope = l5_reader._v3_readiness_envelope({'ok': False, 'deployment_assurance': {'required': False, 'blocking': False}},
        scope=SCOPE, profile_key='l5.default', capability_scope='global', runtime=runtime)
    assert envelope['incident_coverage'] == observation and verify(envelope['incident_coverage'], OBSERVATION_SCHEMA)
    assert envelope['product_l5_complete'] is False
    assert all(envelope['transaction_evidence'][field] is None
               for field in ('known_before_detection', 'prior_user_reported', 'manual_bootstrap'))
    assert connection(runtime).execute('SELECT count(*) FROM code_evolution_terminal_receipts').fetchone()[0] == 0


def test_different_connection_update_delete_and_scope_moves_are_journaled(runtime):
    record = runtime.store.append(RecordEnvelope.create(kind='memory', source='hermes', scope=SCOPE, title='version one'))
    database = connection(runtime).execute('PRAGMA database_list').fetchone()[2]
    other = sqlite3.connect(database)
    try:
        other.execute('UPDATE records SET user_id=? WHERE record_id=?', ('other-owner', record.record_id))
        other.execute('DELETE FROM records WHERE record_id=?', (record.record_id,))
        other.commit()
    finally:
        other.close()
    rows = connection(runtime).execute(f'SELECT operation,user_id FROM {JOURNAL} WHERE record_id=? ORDER BY sequence', (record.record_id,)).fetchall()
    assert [tuple(row) for row in rows] == [('INSERT', SCOPE.user_id), ('UPDATE', SCOPE.user_id), ('UPDATED_VERSION', 'other-owner'), ('DELETE', 'other-owner')]


@pytest.mark.parametrize('table', [JOURNAL, CHECKPOINTS, OBSERVATIONS])
@pytest.mark.parametrize('operation', ['UPDATE', 'DELETE'])
def test_evidence_journals_are_physically_append_only(runtime, table, operation):
    checkpoint_native_ingress(runtime, scope=SCOPE)
    incident(runtime)
    with pytest.raises(sqlite3.IntegrityError, match='append-only'):
        connection(runtime).execute(f'DELETE FROM {table}' if operation == 'DELETE' else f'UPDATE {table} SET sequence=sequence')
    connection(runtime).rollback()


def test_ordinary_hash_forgery_is_not_accepted_as_coverage(runtime):
    checkpoint_native_ingress(runtime, scope=SCOPE)
    actual = incident(runtime)
    forged = {**actual['incident_coverage'], 'covered_known_before_detection': True}
    # Attacker can recompute ordinary digests but cannot produce the native HMAC.
    connection(runtime).execute(f'''INSERT INTO {OBSERVATIONS}(tenant_id,agent_id,workspace_id,user_id,record_id,payload_json,payload_digest,created_at)
        VALUES(?,?,?,?,?,?,?,?)''', (*asdict(SCOPE).values(), actual['incident_record_id'], canonical(forged), digest(forged), forged['observed_at']))
    connection(runtime).commit()
    result = read_native_ingress_coverage(runtime, scope=SCOPE, record_id=actual['incident_record_id'])
    assert result['reason'] == 'readback_unavailable:ValueError'


def test_existing_schema_missing_index_is_repaired_without_history_reset(runtime):
    first = checkpoint_native_ingress(runtime, scope=SCOPE)
    connection(runtime).execute('DROP INDEX idx_incident_coverage_scope_sequence')
    connection(runtime).commit()
    assert schema_ready(connection(runtime)) is False
    ensure_schema(connection(runtime))
    assert schema_ready(connection(runtime)) is True
    next_value = checkpoint_native_ingress(runtime, scope=SCOPE)
    assert next_value['epoch_id'] == first['epoch_id'] and next_value['continuity'] is True


def test_missing_capture_trigger_repairs_with_visible_gap_not_false_continuity(runtime):
    first = checkpoint_native_ingress(runtime, scope=SCOPE)
    connection(runtime).execute('DROP TRIGGER trg_code_incident_coverage_records_insert')
    connection(runtime).commit()
    assert checkpoint_native_ingress(runtime, scope=SCOPE)['reason'] == 'capture_schema_or_index_not_ready'
    ensure_schema(connection(runtime))
    assert schema_ready(connection(runtime)) is True
    next_value = checkpoint_native_ingress(runtime, scope=SCOPE)
    assert next_value['epoch_id'] != first['epoch_id'] and next_value['reason'] == 'capture_gap'


def test_checkpoint_is_incremental_and_does_not_scan_record_payloads(runtime):
    first = checkpoint_native_ingress(runtime, scope=SCOPE)
    runtime.store.append(RecordEnvelope.create(kind='memory', source='hermes', scope=SCOPE, title='new input'))
    statements = []
    connection(runtime).set_trace_callback(statements.append)
    try:
        second = checkpoint_native_ingress(runtime, scope=SCOPE)
    finally:
        connection(runtime).set_trace_callback(None)
    assert second['prior_high_water'] == first['high_water'] and second['incremental_count'] == 1
    assert second['continuity'] is True
    assert not any('FROM records' in sql or 'FROM events' in sql for sql in statements)


def test_producer_change_expiry_and_cross_scope_never_reuse_coverage(runtime, monkeypatch):
    checkpoint_native_ingress(runtime, scope=SCOPE)
    actual = incident(runtime)
    store = CodeIncidentCoverageStore(runtime.store)
    assert store.read_observation(asdict(SCOPE), actual['incident_record_id'], producer_digest='changed', verify=verify)['reason'] == 'producer_changed'
    later = datetime.now(timezone.utc) + timedelta(hours=2)
    monkeypatch.setattr('eimemory.storage.code_incident_coverage_store._now', lambda: later)
    assert store.read_observation(asdict(SCOPE), actual['incident_record_id'], producer_digest=producer_identity(), verify=verify)['reason'] == 'checkpoint_expired'
    other = {**asdict(SCOPE), 'user_id': 'other-owner'}
    assert store.read_observation(other, actual['incident_record_id'], producer_digest=producer_identity(), verify=verify)['reason'] == 'persisted_observation_missing'


def test_real_existing_event_ingress_and_different_writer_updates_remain_unclassified(runtime):
    checkpoint_native_ingress(runtime, scope=SCOPE)
    runtime.store.record_event({'source': 'hermes', 'user_phrase': 'a real free text input',
        'event_type': 'communication', 'timestamp': datetime.now(timezone.utc).isoformat()}, scope=SCOPE)
    rows = connection(runtime).execute(f"SELECT * FROM {JOURNAL} WHERE table_name='events'").fetchall()
    assert len(rows) == 1 and rows[0]['human_input'] == 1 and rows[0]['opaque_payload'] == 1
    # No user_phrase or record body is copied to the new chronology table.
    assert 'user_phrase' not in rows[0].keys() and 'payload_json' not in rows[0].keys()
    actual = incident(runtime)
    assert 'unclassified_input_present' in actual['incident_coverage']['reasons']
    assert actual['incident_coverage']['covered_prior_user_reported'] is None


def test_legacy_database_upgrade_has_no_fabricated_history(runtime, monkeypatch):
    import eimemory.storage.migrations as registry
    old_specs = tuple(spec for spec in registry.REGISTERED_STORAGE_MIGRATIONS if spec.migration_id != 'code.incident.coverage.v1')
    with monkeypatch.context() as patch:
        patch.setattr(registry, 'REGISTERED_STORAGE_MIGRATIONS', old_specs)
        legacy = RuntimeStore(runtime.store.root.parent / 'legacy')
    try:
        before = legacy.append(RecordEnvelope.create(kind='memory', source='hermes', scope=SCOPE, title='pre-upgrade'))
        original = legacy.sqlite.conn.execute('SELECT payload_digest FROM records WHERE record_id=?', (before.record_id,)).fetchone()[0]
        root = legacy.root
        legacy.close()
        legacy = RuntimeStore(root)
        assert schema_ready(legacy.sqlite.conn)
        assert legacy.sqlite.conn.execute(f'SELECT count(*) FROM {JOURNAL}').fetchone()[0] == 0
        assert legacy.sqlite.conn.execute('SELECT payload_digest FROM records WHERE record_id=?', (before.record_id,)).fetchone()[0] == original
    finally:
        legacy.close()


def test_prefix_hole_after_illicit_trigger_removal_is_not_accepted(runtime):
    checkpoint_native_ingress(runtime, scope=SCOPE)
    prior = runtime.store.append(RecordEnvelope.create(kind='memory', source='hermes', scope=SCOPE, title='prior input'))
    checkpoint_native_ingress(runtime, scope=SCOPE)
    actual = incident(runtime)
    conn = connection(runtime)
    guard = f'trg_{JOURNAL}_no_delete'
    # Fault injection bypasses the physical guard. Recreating it does not make
    # the already signed prefix's missing row acceptable to the verifier.
    conn.execute('DROP TRIGGER ' + guard)
    conn.execute(f'DELETE FROM {JOURNAL} WHERE record_id=?', (prior.record_id,))
    conn.execute(trigger_definitions()[guard])
    conn.commit()
    result = read_native_ingress_coverage(runtime, scope=SCOPE, record_id=actual['incident_record_id'])
    assert result['status'] == 'unproven'
    assert result['covered_known_before_detection'] is None


def test_checkpoint_chain_parent_must_physically_exist(runtime):
    checkpoint_native_ingress(runtime, scope=SCOPE)
    checkpoint_native_ingress(runtime, scope=SCOPE)
    conn = connection(runtime)
    guard = f'trg_{CHECKPOINTS}_no_delete'
    conn.execute('DROP TRIGGER ' + guard)
    conn.execute(f'DELETE FROM {CHECKPOINTS} WHERE sequence=(SELECT min(sequence) FROM {CHECKPOINTS})')
    conn.execute(trigger_definitions()[guard])
    conn.commit()
    result = checkpoint_native_ingress(runtime, scope=SCOPE)
    assert result['reason'] == 'checkpoint_unavailable:ValueError'


def test_gap_and_capacity_limit_are_explicit_never_false_continuity(runtime, monkeypatch):
    import eimemory.storage.code_incident_coverage_store as implementation
    monkeypatch.setattr(implementation, 'MAX_JOURNAL_PAGE', 2)
    checkpoint_native_ingress(runtime, scope=SCOPE)
    for index in range(3):
        runtime.store.append(RecordEnvelope.create(kind='memory', source='hermes', scope=SCOPE, title='new ' + str(index)))
    checkpoint = checkpoint_native_ingress(runtime, scope=SCOPE)
    assert checkpoint['reason'] == 'incremental_span_exceeded' and checkpoint['incremental_truncated'] is True
    assert checkpoint['continuity'] is False and checkpoint['historical_before_epoch_covered'] is False


def test_no_hmac_key_keeps_real_business_writes_available(runtime, monkeypatch):
    monkeypatch.delenv('EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY')
    monkeypatch.setenv('EIMEMORY_EVIDENCE_RECEIPT_ENV_FILE', str(runtime.store.root / 'absent-private-key.env'))
    report = checkpoint_native_ingress(runtime, scope=SCOPE)
    assert report['status'] == 'unproven'
    record = runtime.store.append(RecordEnvelope.create(kind='memory', source='hermes', scope=SCOPE, title='ordinary business write'))
    assert connection(runtime).execute(f'SELECT count(*) FROM {JOURNAL} WHERE record_id=?', (record.record_id,)).fetchone()[0] == 1
    assert connection(runtime).execute(f'SELECT count(*) FROM {CHECKPOINTS}').fetchone()[0] == 0


def test_real_parallel_connection_is_serialized_at_checkpoint_high_water(runtime):
    import threading
    record = runtime.store.append(RecordEnvelope.create(kind='memory', source='hermes', scope=SCOPE, title='before checkpoint'))
    database = connection(runtime).execute('PRAGMA database_list').fetchone()[2]
    entered, release = threading.Event(), threading.Event()
    results = []
    def paused_seal(body, domain):
        entered.set()
        assert release.wait(3)
        return seal(body, domain)
    def checkpoint():
        try:
            results.append(CodeIncidentCoverageStore(runtime.store).checkpoint(asdict(SCOPE),
                producer_digest=producer_identity(), seal=paused_seal, verify=verify))
        except BaseException as error:
            results.append(error)
    thread = threading.Thread(target=checkpoint)
    thread.start()
    assert entered.wait(3)
    other = sqlite3.connect(database, timeout=0.02)
    try:
        with pytest.raises(sqlite3.OperationalError, match='locked'):
            other.execute('UPDATE records SET title=title WHERE record_id=?', (record.record_id,))
        other.rollback()
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive() and len(results) == 1 and isinstance(results[0], dict)
    try:
        other.execute('UPDATE records SET title=title WHERE record_id=?', (record.record_id,))
        other.commit()
    finally:
        other.close()
    assert connection(runtime).execute(f'SELECT max(sequence) FROM {JOURNAL}').fetchone()[0] > results[0]['high_water']


def test_idle_checkpoint_chain_rolls_to_real_bounded_epoch(runtime, monkeypatch):
    import eimemory.storage.code_incident_coverage_store as implementation
    monkeypatch.setattr(implementation, 'MAX_EPOCH_CHECKPOINTS', 2)
    first = checkpoint_native_ingress(runtime, scope=SCOPE)
    second = checkpoint_native_ingress(runtime, scope=SCOPE)
    assert second['epoch_checkpoint_count'] == 2 and second['epoch_id'] == first['epoch_id']
    third = checkpoint_native_ingress(runtime, scope=SCOPE)
    assert third['reason'] == 'bounded_checkpoint_capacity_reached'
    assert third['epoch_checkpoint_count'] == 1 and third['epoch_id'] != first['epoch_id']
    actual = incident(runtime)
    assert verify(actual['incident_coverage'], OBSERVATION_SCHEMA)
    assert read_native_ingress_coverage(runtime, scope=SCOPE, record_id=actual['incident_record_id']) == actual['incident_coverage']


@pytest.mark.parametrize('enabled', [False, True])
def test_postgres_configuration_never_upgrades_sqlite_coverage(runtime, monkeypatch, enabled):
    from eimemory.retrieval.postgres_vector import PostgresVectorCandidateSource, PostgresVectorConfig
    monkeypatch.setenv('EIMEMORY_POSTGRES_VECTOR_ENABLED', '1' if enabled else '0')
    def forbidden_connection(**_kwargs):
        pytest.fail('a SQLite chronology diagnostic must never connect to PostgreSQL')
    source = PostgresVectorCandidateSource(sqlite_source=SimpleNamespace(),
        config=PostgresVectorConfig(enabled=enabled, connection_factory=forbidden_connection))
    runtime.memory = SimpleNamespace(recall_engine=SimpleNamespace(candidate_source=source))
    assert source.config.enabled is enabled and source.config.configured is True
    checkpoint = checkpoint_native_ingress(runtime, scope=SCOPE)
    observation = incident(runtime)['incident_coverage']
    for value in (checkpoint, observation):
        assert value['backend_coverage']['postgres'] is None
        assert value['backend_coverage']['external_inputs'] is None
        assert value['all_backend_knowledge_covered'] is False
        assert value['qualifies_for_product_completion'] is False


def test_changed_signed_prefix_metadata_and_wrong_index_fail_closed(runtime):
    checkpoint_native_ingress(runtime, scope=SCOPE)
    record = runtime.store.append(RecordEnvelope.create(kind='memory', source='hermes', scope=SCOPE, title='prior'))
    checkpoint_native_ingress(runtime, scope=SCOPE)
    actual = incident(runtime)
    conn = connection(runtime)
    guard = f'trg_{JOURNAL}_no_update'
    conn.execute('DROP TRIGGER ' + guard)
    conn.execute(f"UPDATE {JOURNAL} SET source='different-writer' WHERE record_id=?", (record.record_id,))
    conn.execute(trigger_definitions()[guard])
    conn.commit()
    assert read_native_ingress_coverage(runtime, scope=SCOPE, record_id=actual['incident_record_id'])['status'] == 'unproven'
    conn.execute('DROP INDEX idx_incident_coverage_scope_sequence')
    conn.execute(f'CREATE INDEX idx_incident_coverage_scope_sequence ON {JOURNAL}(record_id)')
    conn.commit()
    assert schema_ready(conn) is False
    ensure_schema(conn)
    assert schema_ready(conn) is True
    # Fixing only an index cannot repair the already changed signed history.
    assert checkpoint_native_ingress(runtime, scope=SCOPE)['status'] == 'unproven'


def test_incident_version_lookup_uses_its_bounded_index(runtime):
    checkpoint_native_ingress(runtime, scope=SCOPE)
    actual = incident(runtime)
    observation = actual['incident_coverage']
    sql = f'''SELECT * FROM {JOURNAL} WHERE table_name='records' AND operation='INSERT'
        AND tenant_id=? AND agent_id=? AND workspace_id=? AND user_id=? AND record_id=? AND payload_digest=?
        ORDER BY sequence LIMIT 2'''
    plan = connection(runtime).execute('EXPLAIN QUERY PLAN ' + sql,
        (*asdict(SCOPE).values(), actual['incident_record_id'], observation['payload_digest'])).fetchall()
    assert any('idx_incident_coverage_record_version' in row[3] for row in plan), [tuple(row) for row in plan]
    assert not any('TEMP B-TREE' in row[3] for row in plan)
    record_plan = connection(runtime).execute('EXPLAIN QUERY PLAN SELECT * FROM records INDEXED BY idx_records_record_id '
        'WHERE tenant_id=? AND agent_id=? AND workspace_id=? AND user_id=? AND record_id=? LIMIT 2',
        (*asdict(SCOPE).values(), actual['incident_record_id'])).fetchall()
    assert any('idx_records_record_id' in row[3] for row in record_plan)


def test_trigger_literal_case_change_is_a_visible_capture_gap(runtime):
    first = checkpoint_native_ingress(runtime, scope=SCOPE)
    conn = connection(runtime)
    name = 'trg_code_incident_coverage_records_insert'
    conn.execute('DROP TRIGGER ' + name)
    conn.execute(trigger_definitions()[name].replace("'user_reported'", "'USER_REPORTED'"))
    conn.commit()
    assert schema_ready(conn) is False
    ensure_schema(conn)
    next_value = checkpoint_native_ingress(runtime, scope=SCOPE)
    assert next_value['reason'] == 'capture_gap' and next_value['epoch_id'] != first['epoch_id']


def test_different_connection_event_update_delete_preserves_both_row_scopes(runtime):
    runtime.store.record_event({'source': 'hermes', 'user_phrase': 'original private text',
        'event_type': 'communication', 'timestamp': datetime.now(timezone.utc).isoformat()}, scope=SCOPE)
    database = connection(runtime).execute('PRAGMA database_list').fetchone()[2]
    other = sqlite3.connect(database)
    try:
        other.execute('UPDATE events SET user_id=?,user_phrase=?', ('other-owner', 'changed private text'))
        other.execute('DELETE FROM events')
        other.commit()
    finally:
        other.close()
    rows = connection(runtime).execute(f"SELECT operation,user_id FROM {JOURNAL} WHERE table_name='events' ORDER BY sequence").fetchall()
    assert [tuple(row) for row in rows] == [('INSERT', SCOPE.user_id), ('UPDATE', SCOPE.user_id),
        ('UPDATED_VERSION', 'other-owner'), ('DELETE', 'other-owner')]
    stored = canonical([dict(row) for row in connection(runtime).execute(f'SELECT * FROM {JOURNAL}')])
    assert 'original private text' not in stored and 'changed private text' not in stored


def test_journal_write_failure_rolls_back_its_business_row(runtime):
    conn = connection(runtime)
    conn.execute(f"CREATE TRIGGER isolated_journal_failure BEFORE INSERT ON {JOURNAL} BEGIN SELECT RAISE(ABORT,'isolated capture failure'); END")
    conn.commit()
    record = RecordEnvelope.create(kind='memory', source='hermes', scope=SCOPE, title='must be atomic')
    with pytest.raises(sqlite3.IntegrityError, match='isolated capture failure'):
        runtime.store.append(record)
    assert conn.execute('SELECT count(*) FROM records WHERE record_id=?', (record.record_id,)).fetchone()[0] == 0
    assert conn.execute(f'SELECT count(*) FROM {JOURNAL} WHERE record_id=?', (record.record_id,)).fetchone()[0] == 0


def test_coverage_authority_paths_cannot_be_changed_by_evolution():
    from eimemory.governance.evolution.code_evolution_path_policy import path_denied_by_self
    for path in ('eimemory/contracts/code_incident_coverage.py',
                 'eimemory/contracts/nightly_step_evidence.py',
                 'eimemory/governance/learning/nightly_step_evidence.py',
                 'eimemory/storage/code_incident_coverage_store.py',
                 'eimemory/storage/migrations/code_incident_coverage.py',
                 'eimemory/storage/migrations/__init__.py',
                 'eimemory/governance/evolution/code_evolution_incident_coverage.py'):
        assert path_denied_by_self(path)
