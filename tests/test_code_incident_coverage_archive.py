from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
import sqlite3

import pytest

from test_code_incident_coverage import runtime, incident, connection, SCOPE
from eimemory.governance.evolution.code_evolution_incident_coverage import (
    checkpoint_native_ingress, read_native_ingress_coverage,
)
from eimemory.governance.learning.effect_policy import seal, verify
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.code_incident_coverage_store import CodeIncidentCoverageStore
from eimemory.storage.code_incident_coverage_archive import CoverageArchive, TABLES
from eimemory.storage.migrations.code_incident_coverage import (
    JOURNAL, CHECKPOINTS, OBSERVATIONS, trigger_definitions,
)
from eimemory.storage.migrations.code_incident_coverage_archive import ARCHIVES, KEYS
from eimemory.storage.runtime_store import RuntimeStore


def prepare(runtime):
    checkpoint_native_ingress(runtime, scope=SCOPE)
    original = incident(runtime)
    for index in range(3):
        later = RecordEnvelope.create(kind='reflection', source='eimemory.scheduler.step_evidence',
            scope=SCOPE, title=f'later actual diagnostic {index}')
        later.time.occurred_at = datetime.now(timezone.utc).isoformat()
        runtime.store.append(later)
        checkpoint_native_ingress(runtime, scope=SCOPE)
    # A different actual incident produces another immutable observation.
    other_scope = ScopeRef.from_dict({**asdict(SCOPE), 'user_id': 'other-owner'})
    checkpoint_native_ingress(runtime, scope=other_scope)
    other = incident(runtime, scope=other_scope, reason='different-actual-failure')
    assert other['incident_record_id'] != original['incident_record_id']
    return original


def compact(runtime, **kwargs):
    return CodeIncidentCoverageStore(runtime.store).maintain_archive(
        seal=seal, verify=verify, keep={table: 1 for table in TABLES}, **kwargs)


def test_real_signed_history_survives_compression_pruning_and_restart(runtime):
    original = prepare(runtime)
    conn = connection(runtime)
    before = {table: [dict(row) for row in conn.execute(f'SELECT * FROM {table} ORDER BY sequence')]
              for table in TABLES}
    result = compact(runtime)
    assert result['ok'] and result['raw_evidence_deleted'] is False
    assert all(result['hot_rows'][table] == 1 and result['archived'][table] > 0 for table in TABLES)
    cold = CoverageArchive(runtime.store, verify)
    for table in TABLES:
        maximum = before[table][-1]['sequence']
        rows = cold.range_rows(table, 0, maximum)
        rows += [dict(row) for row in conn.execute(f'SELECT * FROM {table} ORDER BY sequence')]
        assert sorted(rows, key=lambda row: row['sequence']) == before[table]
    assert read_native_ingress_coverage(runtime, scope=SCOPE,
        record_id=original['incident_record_id']) == original['incident_coverage']
    root = runtime.store.root
    runtime.store.close()
    runtime.store = RuntimeStore(root)
    assert read_native_ingress_coverage(runtime, scope=SCOPE,
        record_id=original['incident_record_id']) == original['incident_coverage']
    assert original['incident_coverage']['global_facts'] == dict.fromkeys(
        ('known_before_detection', 'prior_user_reported', 'manual_bootstrap'))
    # This is a bounded diagnostic, never a new product-completion certificate.
    assert not original['incident_coverage']['qualifies_for_product_completion']


@pytest.mark.parametrize('table', TABLES)
@pytest.mark.parametrize('operation', ['UPDATE', 'DELETE'])
def test_original_guards_are_restored_and_archive_indices_are_append_only(runtime, table, operation):
    prepare(runtime)
    compact(runtime)
    conn = connection(runtime)
    name = f'trg_{table}_no_{operation.lower()}'
    assert conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()[0] == trigger_definitions()[name]
    sql = f'DELETE FROM {table}' if operation == 'DELETE' else f'UPDATE {table} SET sequence=sequence'
    with pytest.raises(sqlite3.IntegrityError): conn.execute(sql)
    conn.rollback()
    for index in (ARCHIVES, KEYS):
        sql = f'DELETE FROM {index}' if operation == 'DELETE' else f'UPDATE {index} SET sequence=sequence'
        with pytest.raises(sqlite3.IntegrityError): conn.execute(sql)
        conn.rollback()


@pytest.mark.parametrize('failure', ['seal', 'after_prune', 'directory_fsync'])
def test_failure_rolls_back_sql_prefix_and_guards_without_accepting_orphan_frames(runtime, monkeypatch, failure):
    original = prepare(runtime)
    conn = connection(runtime)
    before = {table: [tuple(row) for row in conn.execute(f'SELECT * FROM {table}')] for table in TABLES}
    if failure == 'seal':
        def failing_seal(*_args): raise ValueError('fixture_seal_failure')
        with pytest.raises(ValueError, match='fixture_seal_failure'):
            CodeIncidentCoverageStore(runtime.store).maintain_archive(seal=failing_seal, verify=verify,
                keep={table: 1 for table in TABLES})
    elif failure == 'after_prune':
        with monkeypatch.context() as patch:
            patch.setattr(CoverageArchive, '_rows', lambda *_args:
                (_ for _ in ()).throw(ValueError('fixture_readback_failure')))
            with pytest.raises(ValueError, match='fixture_readback_failure'): compact(runtime)
    else:
        with monkeypatch.context() as patch:
            patch.setattr('eimemory.storage.code_incident_coverage_archive._fsync_directory',
                lambda *_args: (_ for _ in ()).throw(OSError('fixture_directory_fsync_failure')))
            with pytest.raises(OSError, match='fixture_directory_fsync_failure'): compact(runtime)
    for table in TABLES:
        assert [tuple(row) for row in conn.execute(f'SELECT * FROM {table}')] == before[table]
        assert conn.execute('SELECT sql FROM sqlite_master WHERE name=?',
            (f'trg_{table}_no_delete',)).fetchone()[0] == trigger_definitions()[f'trg_{table}_no_delete']
    assert conn.execute(f'SELECT count(*) FROM {ARCHIVES}').fetchone()[0] == 0
    assert read_native_ingress_coverage(runtime, scope=SCOPE,
        record_id=original['incident_record_id']) == original['incident_coverage']


@pytest.mark.parametrize('damage', ['frame', 'missing_frame', 'manifest', 'key_binding'])
def test_corrupt_cold_evidence_becomes_unknown_without_resealing(runtime, damage):
    original = prepare(runtime)
    compact(runtime)
    conn = connection(runtime)
    row = conn.execute(f'SELECT * FROM {ARCHIVES} ORDER BY sequence LIMIT 1').fetchone()
    body = json.loads(row['payload_json'])
    cold = CoverageArchive(runtime.store, verify)
    if damage in {'frame', 'missing_frame'}:
        files = list(cold.root.glob('payload-*.seg'))
        assert len(files) == 1
        if damage == 'missing_frame': files[0].unlink()
        else:
            with files[0].open('r+b') as handle:
                handle.seek(0); handle.write(b'BROKEN')
    elif damage == 'manifest':
        conn.execute(f'DROP TRIGGER trg_{ARCHIVES}_no_update')
        body['signature'] = '0' * 64
        conn.execute(f'UPDATE {ARCHIVES} SET payload_json=? WHERE sequence=?', (json.dumps(body), row['sequence']))
        from eimemory.storage.migrations.code_incident_coverage_archive import triggers
        conn.execute(triggers()[f'trg_{ARCHIVES}_no_update'])
        conn.commit()
    else:
        conn.execute(f'DROP TRIGGER trg_{KEYS}_no_update')
        conn.execute(f'UPDATE {KEYS} SET original_sequence=999999 WHERE table_name=?', (OBSERVATIONS,))
        from eimemory.storage.migrations.code_incident_coverage_archive import triggers
        conn.execute(triggers()[f'trg_{KEYS}_no_update'])
        conn.commit()
    result = read_native_ingress_coverage(runtime, scope=SCOPE, record_id=original['incident_record_id'])
    assert result['status'] == 'unproven'
    assert result['covered_known_before_detection'] is None
    assert not result['qualifies_for_product_completion']


def test_cold_source_lookup_is_exact_scope_and_late_input_still_invalidates(runtime):
    original = prepare(runtime)
    compact(runtime)
    cold = CoverageArchive(runtime.store, verify)
    assert cold.key_rows(OBSERVATIONS, scope={**asdict(SCOPE), 'tenant_id': 'other'},
        record_id=original['incident_record_id']) == []
    record = RecordEnvelope.create(kind='memory', source='hermes', scope=SCOPE, title='actual late report')
    record.time.occurred_at = original['detected_at']
    runtime.store.append(record)
    result = read_native_ingress_coverage(runtime, scope=SCOPE, record_id=original['incident_record_id'])
    assert result['status'] == 'unproven' and result['covered_prior_user_reported'] is None


def test_capacity_bound_precedes_shared_writer_inventory_and_preserves_originals(runtime, monkeypatch):
    from eimemory.storage import code_incident_coverage_archive as module
    prepare(runtime)
    conn = connection(runtime)
    before = conn.execute(f'SELECT count(*) FROM {JOURNAL}').fetchone()[0]
    root = CoverageArchive(runtime.store, verify).root
    root.mkdir(parents=True, exist_ok=True)
    # Test a small explicit cap using real files; the production cap is 256.
    monkeypatch.setattr(module, 'MAX_SEGMENT_FILES', 2)
    for number in range(2): (root / f'payload-{number:08d}.seg').write_bytes(b'')
    monkeypatch.setattr(module, 'PayloadSegmentStore', lambda *_a, **_k:
        pytest.fail('capacity must be checked before any unbounded shared-store scan'))
    with pytest.raises(ValueError, match='capacity_requires_export'): compact(runtime)
    assert conn.execute(f'SELECT count(*) FROM {JOURNAL}').fetchone()[0] == before
    assert conn.execute(f'SELECT count(*) FROM {ARCHIVES}').fetchone()[0] == 0


def test_multiple_batches_preserve_global_sequence_and_evidence_with_another_writer(runtime):
    prepare(runtime)
    compact(runtime, batches=1)
    other = sqlite3.connect(runtime.store.sqlite.path)
    try:
        # A physical mutation through another connection is captured by the
        # existing database trigger, not by a Python collector assumption.
        other.execute("UPDATE records SET title='changed' WHERE record_id=(SELECT record_id FROM records LIMIT 1)")
        other.commit()
    finally: other.close()
    compact(runtime, batches=1)
    conn = connection(runtime)
    high = conn.execute('SELECT seq FROM sqlite_sequence WHERE name=?', (JOURNAL,)).fetchone()[0]
    cold = CoverageArchive(runtime.store, verify)
    rows = cold.range_rows(JOURNAL, 0, high) + [dict(row) for row in conn.execute(f'SELECT * FROM {JOURNAL}')]
    assert sorted(row['sequence'] for row in rows) == list(range(1, high + 1))


def test_multiple_cold_frames_reject_parent_gap_and_bounded_read_overflow(runtime, monkeypatch):
    from eimemory.storage import code_incident_coverage_archive as module
    original = prepare(runtime)
    monkeypatch.setattr(module, 'MAX_ROWS', 2)
    compact(runtime)
    conn = connection(runtime)
    cold = CoverageArchive(runtime.store, verify)
    high = conn.execute('SELECT seq FROM sqlite_sequence WHERE name=?', (JOURNAL,)).fetchone()[0]
    assert len(cold.range_rows(JOURNAL, 0, high)) > 2
    with monkeypatch.context() as patch:
        patch.setattr(module, 'MAX_READ_SEGMENTS', 1)
        with pytest.raises(ValueError, match='read_bound_exceeded'): cold.range_rows(JOURNAL, 0, high)
    first = conn.execute(f'SELECT sequence FROM {ARCHIVES} WHERE table_name=? ORDER BY first_sequence LIMIT 1', (JOURNAL,)).fetchone()[0]
    conn.execute(f'DROP TRIGGER trg_{ARCHIVES}_no_delete')
    conn.execute(f'DELETE FROM {ARCHIVES} WHERE sequence=?', (first,))
    from eimemory.storage.migrations.code_incident_coverage_archive import triggers
    conn.execute(triggers()[f'trg_{ARCHIVES}_no_delete'])
    conn.commit()
    with pytest.raises(ValueError, match='prefix_missing'): cold.range_rows(JOURNAL, 0, high)
    assert read_native_ingress_coverage(runtime, scope=SCOPE,
        record_id=original['incident_record_id'])['status'] == 'unproven'


def test_cold_reads_require_complete_archive_schema_and_guards(runtime):
    original = prepare(runtime)
    compact(runtime)
    conn = connection(runtime)
    conn.execute(f'DROP TRIGGER trg_{ARCHIVES}_no_update')
    conn.commit()
    cold = CoverageArchive(runtime.store, verify)
    with pytest.raises(ValueError, match='schema_or_guards_unverified'): cold.high_water(JOURNAL)
    assert read_native_ingress_coverage(runtime, scope=SCOPE,
        record_id=original['incident_record_id'])['status'] == 'unproven'


@pytest.mark.parametrize('path', [
    'eimemory/storage/code_incident_coverage_archive.py',
    'eimemory/storage/migrations/code_incident_coverage_archive.py',
    'eimemory/storage/code_incident_coverage_store.py',
    'eimemory/governance/release/controlled_release_lineage.py',
    'eimemory/governance/release/release_lineage.py',
    'eimemory/governance/release/closure_contracts.py',
    'eimemory/governance/release/evidence_contract.py',
    'eimemory/governance/release/deployment_receipt.py',
    'eimemory/governance/release/release_pre_observation.py',
])
def test_evidence_authority_cannot_patch_itself_even_with_an_exact_allow(path):
    from eimemory.governance.evolution.code_evolution_path_policy import path_allowed_for_evolution
    assert path_allowed_for_evolution(path, exact_allow_files=[path],
        allowed_path_globs=['**'], denied_path_globs=[], deny_self_paths=[]) == (False, 'deny_self')
