"""Isolated native provenance regression fixtures, never production evidence."""
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict
import json
from types import SimpleNamespace
import pytest

from eimemory.contracts.code_incident_provenance import (
    FIELDS, SOURCE_KEY, provenance_snapshot, qualification_provenance,
)
from eimemory.models.records import ScopeRef
from eimemory.storage.runtime_store import RuntimeStore
from eimemory.storage.code_evolution_store import CodeEvolutionStore, CodeEvolutionStoreError, CodeEvolutionConflict, digest_json
from eimemory.governance.evolution.code_evolution_transaction import CodeEvolutionTransactionManager, qualification_report
from eimemory.governance.evolution.code_evolution_effects import _l5_observation_semantics
from eimemory.governance.l5.l5_product_completion import build_product_completion
from test_code_evolution_effects import _observation_transaction, _pending_observation_report
from test_l5_product_completion import _assessment


def _unknown(store, **updates):
    return CodeEvolutionStore(store).create_transaction({
        'transaction_id': 'fixture-unknown', 'idempotency_key': 'fixture-unknown',
        'scope': {'tenant_id': 'fixture', 'agent_id': 'fixture', 'workspace_id': 'fixture', 'user_id': 'fixture'},
        'repository': {'root': str(store.root), 'ref': 'master'},
        'incident': {'incident_id': 'fixture-native-incident'}, 'origin': 'system_detector',
        'detector': 'eimemory.release_closure_failure.v1', **{key: None for key in FIELDS},
        'current_state': 'DETECTED', **updates})


def test_typed_unknown_roundtrips_all_native_ledger_reads_and_original_terminal(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        tx = _unknown(store)
        ledger = CodeEvolutionStore(store)
        scope = ScopeRef.from_dict(tx['payload']['scope'])
        physical = ledger.conn.execute('SELECT * FROM code_evolution_transactions').fetchone()
        assert physical['schema_version'] == 'code_evolution_transaction.v2'
        assert [physical[k] for k in FIELDS] == [1, 1, 1]
        readers = [ledger.get_transaction(tx['transaction_id']), ledger.list_transactions()[0],
                   ledger.list_transactions_exact_scope(scope=scope, repository_root=str(store.root))[0],
                   ledger.list_recovery_transactions_exact_scope(scope=scope)[0],
                   ledger.acquire_lease(tx['transaction_id'], owner='fixture-owner')]
        for decoded in readers:
            assert [decoded[k] for k in FIELDS] == [None, None, None]
            assert decoded['payload']['source_evidence'][SOURCE_KEY] == decoded['source_evidence'][SOURCE_KEY]
        manager = CodeEvolutionTransactionManager(store)
        ended = manager.effect_disabled(tx['transaction_id'], step='fixture-no-effects')
        assert [ended[k] for k in FIELDS] == [None, None, None]
        receipt = ledger.get_terminal_receipt(tx['transaction_id'])
        assert receipt['payload'][SOURCE_KEY]['facts'] == tx['source_evidence'][SOURCE_KEY]['facts']
        assert all(receipt['payload'][SOURCE_KEY]['facts'][k] is None for k in FIELDS)
        original = receipt['payload_json']
        assert ledger.get_terminal_receipt(tx['transaction_id'])['payload_json'] == original
        assert qualification_report(ended, terminal_receipt=receipt, current_lineage={'ok': True, 'compatible': True})['qualifies_for_product_completion'] is False


@pytest.mark.parametrize('bad', ['schema', 'digest', 'guard', 'facts', 'source', 'both_missing', 'payload', 'physical_schema', 'schema_downgrade'])
def test_typed_unknown_corruption_never_projects_guard_as_a_fact(tmp_path, bad):
    with closing(RuntimeStore(tmp_path)) as store:
        tx = _unknown(store)
        ledger = CodeEvolutionStore(store)
        source = deepcopy(tx['source_evidence']); payload = deepcopy(tx['payload'])
        if bad == 'schema': source[SOURCE_KEY]['schema'] = 'unknown'
        elif bad == 'digest': source[SOURCE_KEY].pop('digest')
        elif bad == 'guard':
            ledger.conn.execute('UPDATE code_evolution_transactions SET known_before_detection=0'); ledger.conn.commit()
        elif bad == 'facts':
            forged = provenance_snapshot({**source[SOURCE_KEY]['facts'], 'known_before_detection': False})
            source[SOURCE_KEY] = payload['source_evidence'][SOURCE_KEY] = forged
        elif bad == 'source': payload['source_evidence'][SOURCE_KEY]['facts']['detector'] = 'other'
        elif bad in {'both_missing', 'schema_downgrade'}:
            source.pop(SOURCE_KEY); payload['source_evidence'].pop(SOURCE_KEY)
            if bad == 'schema_downgrade':
                payload['schema_version'] = 'code_evolution_transaction.v1'
                ledger.conn.execute("UPDATE code_evolution_transactions SET schema_version='code_evolution_transaction.v1'")
        elif bad == 'payload': payload['known_before_detection'] = False
        elif bad == 'physical_schema': ledger.conn.execute("UPDATE code_evolution_transactions SET schema_version='code_evolution_transaction.v1'")
        ledger.conn.execute('UPDATE code_evolution_transactions SET source_evidence_json=?,payload_json=?',
                            (json.dumps(source), json.dumps(payload))); ledger.conn.commit()
        for read in [lambda: ledger.get_transaction(tx['transaction_id']), lambda: ledger.list_transactions(),
                     lambda: ledger.list_recovery_transactions_exact_scope(scope=tx['payload']['scope'])]:
            with pytest.raises(CodeEvolutionStoreError): read()


@pytest.mark.parametrize('bad', ['flat', 'payload', 'source'])
def test_typed_original_source_facts_cannot_be_replaced_by_metadata_updates(tmp_path, bad):
    with closing(RuntimeStore(tmp_path)) as store:
        tx = _unknown(store)
        ledger = CodeEvolutionStore(store)
        updates = {}
        if bad == 'flat': updates['known_before_detection'] = False
        elif bad == 'payload':
            value = deepcopy(tx['payload']); value['prior_user_reported'] = False; updates['payload_json'] = value
        else: updates['source_evidence_json'] = {}
        with pytest.raises(CodeEvolutionConflict):
            ledger.cas_transition(tx['transaction_id'], expected_state='DETECTED', expected_state_version=tx['state_version'],
                                  target_state='DIAGNOSED', updates=updates)
        assert all(ledger.get_transaction(tx['transaction_id'])[k] is None for k in FIELDS)


@pytest.mark.parametrize('bad', ['missing', 'changed_with_fresh_digest'])
def test_typed_terminal_snapshot_is_reverified_on_read(tmp_path, bad):
    with closing(RuntimeStore(tmp_path)) as store:
        tx = _unknown(store)
        manager = CodeEvolutionTransactionManager(store)
        manager.effect_disabled(tx['transaction_id'], step='fixture-no-effects')
        ledger = manager.store
        receipt = ledger.get_terminal_receipt(tx['transaction_id'])
        payload = deepcopy(receipt['payload'])
        if bad == 'missing': payload.pop(SOURCE_KEY)
        else: payload[SOURCE_KEY] = provenance_snapshot({**payload[SOURCE_KEY]['facts'], 'prior_user_reported': False})
        digest = digest_json(payload)
        # Simulate corrupted persisted bytes only after proving the normal
        # append-only trigger rejects mutation in this disposable fixture DB.
        import sqlite3
        with pytest.raises(sqlite3.IntegrityError, match='append-only'):
            ledger.conn.execute('UPDATE code_evolution_terminal_receipts SET payload_json=?,receipt_digest=?',
                                (json.dumps(payload), digest))
        ledger.conn.rollback()
        names = [row[0] for row in ledger.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='code_evolution_terminal_receipts'")]
        for name in names:
            assert name.startswith('trg_code_evolution_') and name.replace('_','').isalnum()
            ledger.conn.execute('DROP TRIGGER '+name)
        ledger.conn.execute('UPDATE code_evolution_terminal_receipts SET payload_json=?,receipt_digest=?',
                            (json.dumps(payload), digest)); ledger.conn.commit()
        with pytest.raises(CodeEvolutionStoreError, match='terminal incident provenance mismatch'):
            ledger.get_terminal_receipt(tx['transaction_id'])


def test_legacy_signed_bool_receipt_bytes_are_retained_but_native_negatives_are_unproven(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        tx = _unknown(store, **{key: False for key in FIELDS})
        ledger = CodeEvolutionStore(store)
        legacy = deepcopy(tx['payload']); legacy['schema_version'] = 'code_evolution_transaction.v1'
        legacy['source_evidence'].pop(SOURCE_KEY)
        ledger.conn.execute('UPDATE code_evolution_transactions SET schema_version=?,source_evidence_json=?,payload_json=?',
                            (legacy['schema_version'], '{}', json.dumps(legacy))); ledger.conn.commit()
        ledger.add_terminal_receipt(tx['transaction_id'], {'outcome': 'aborted_no_external_effect', 'evidence_digest': 'f'*64},
                                    terminal_state='ABORTED_NO_EXTERNAL_EFFECT')
        original = ledger.get_terminal_receipt(tx['transaction_id'])['payload_json']
        declared = ledger.get_transaction(tx['transaction_id'])
        assert all(declared[k] is False for k in FIELDS)
        normalized = qualification_provenance(declared)
        assert all(normalized[k] is None for k in FIELDS)
        assert all(normalized['declared_provenance'][k] is False for k in FIELDS)
        assert ledger.get_terminal_receipt(tx['transaction_id'])['payload_json'] == original


@pytest.mark.parametrize('field', FIELDS)
def test_each_unknown_source_fact_blocks_l5_even_if_other_shapes_are_positive(field):
    transaction = {'transaction_id': 'fixture-terminal', 'terminal_receipt_digest': 'a'*64,
        'qualifying_terminal_outcome': 'succeeded_sedimented', 'origin': 'system_detector',
        'known_before_detection': False, 'prior_user_reported': False, 'manual_bootstrap': False,
        'observation_valid': True, 'evidence_verified': True, 'quarantined': False}
    transaction[field] = None
    product = build_product_completion(_assessment(), provider={'ready': True, 'catalog_ready': True, 'advertisement_fresh': True},
                                       transaction=transaction, current_lineage={'ok': True, 'compatible': True})
    assert product['product_l5_complete'] is False
    assert not qualification_report(transaction, current_lineage={'ok': True, 'compatible': True})['qualifies_for_product_completion']


def test_legacy_create_replay_retains_original_encoding_and_terminal_bytes(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        tx = _unknown(store, **{key: False for key in FIELDS})
        ledger = CodeEvolutionStore(store)
        legacy = deepcopy(tx['payload']); legacy['schema_version'] = 'code_evolution_transaction.v1'
        legacy['source_evidence'].pop(SOURCE_KEY)
        ledger.conn.execute('UPDATE code_evolution_transactions SET schema_version=?,source_evidence_json=?,payload_json=?',
                            (legacy['schema_version'], '{}', json.dumps(legacy))); ledger.conn.commit()
        ledger.add_terminal_receipt(tx['transaction_id'], {'outcome': 'aborted_no_external_effect', 'evidence_digest': 'f'*64},
                                    terminal_state='ABORTED_NO_EXTERNAL_EFFECT')
        original = tuple(ledger.conn.execute('SELECT * FROM code_evolution_transactions').fetchone())
        receipt = ledger.get_terminal_receipt(tx['transaction_id'])['payload_json']
        replay = ledger.create_transaction(legacy)
        assert replay['idempotent'] is True and replay['schema_version'] == legacy['schema_version']
        assert tuple(ledger.conn.execute('SELECT * FROM code_evolution_transactions').fetchone()) == original
        assert ledger.get_terminal_receipt(tx['transaction_id'])['payload_json'] == receipt
        for field in FIELDS:
            changed = deepcopy(legacy); changed[field] = True
            removed = deepcopy(legacy); removed.pop(field)
            for bad in [changed, removed]:
                with pytest.raises(CodeEvolutionConflict, match='legacy transaction identity conflict'):
                    ledger.create_transaction(bad)
        downgraded = deepcopy(legacy); downgraded['schema_version'] = 'code_evolution_transaction.v2'
        with pytest.raises(CodeEvolutionConflict): ledger.create_transaction(downgraded)


def test_native_unknown_can_be_healthy_observation_without_l5_qualification():
    transaction = _observation_transaction(**{key: None for key in FIELDS})
    report = _pending_observation_report()
    report['transaction_evidence'].update(transaction)
    report['gaps'].extend(['incident_prior_knowledge_unproven', 'incident_not_user_reported_unproven',
                           'incident_not_manual_bootstrap_unproven'])
    accepted, measure = _l5_observation_semantics(report, transaction)
    assert accepted is True and all(measure['checks'].values())
    assert report['product_l5_complete'] is False
    for key in ['incident_prior_knowledge_unproven', 'incident_not_user_reported_unproven', 'incident_not_manual_bootstrap_unproven']:
        bad = deepcopy(report); bad['gaps'].remove(key)
        assert _l5_observation_semantics(bad, transaction)[0] is False


@pytest.mark.parametrize('adverse', [None, 'known_before_detection', 'prior_user_reported', 'manual_bootstrap'])
def test_native_unknown_archive_and_nightly_keep_unproven_separate_from_known_failure(tmp_path, monkeypatch, adverse):
    from eimemory.governance.l5 import native_assessment as native
    from eimemory.contracts.release_identity import ReleaseIdentity
    from eimemory.scheduler.result_contract import _nightly_step, _aggregate_nightly_ok
    from test_native_l5_assessment_maintenance import envelope
    release = ReleaseIdentity('a'*40, 'isolated-fixture', 'fixture-receipt', 'fixture-session')
    monkeypatch.setattr(native, 'current_release_identity', lambda *_a: release)
    monkeypatch.setattr('eimemory.governance.release.evidence_contract.current_release_identity', lambda *_a: release)
    with closing(RuntimeStore(tmp_path)) as store:
        tx = _unknown(store, **({adverse: True} if adverse else {}))
        scope = ScopeRef.from_dict(tx['payload']['scope'])
        value = envelope()
        value['scope'] = asdict(scope)
        value['transaction_evidence'] = qualification_provenance(tx)
        completion = build_product_completion(_assessment(), provider={'ready': True, 'catalog_ready': True, 'advertisement_fresh': True},
            transaction={**value['transaction_evidence'], 'nonterminal': True}, current_lineage={'ok': True, 'compatible': True})
        value.update(completion)
        runtime = SimpleNamespace(store=store, build_l5_readiness_report=lambda **_k: deepcopy(value))
        result = native.run_native_l5_assessment(runtime, scope=asdict(scope))
        assert result['archive_verified'] is True and result['product_l5_complete'] is False, result
        assert result['awaiting_evidence'] is (adverse is None), result
        archived = store.get_by_id(result['record_id']).content['envelope']['transaction_evidence']
        assert all(archived[k] is (True if k == adverse else None) for k in FIELDS)
        steps = []
        _nightly_step(steps, 'native_l5_assessment', lambda: result)
        assert _aggregate_nightly_ok({'native_l5_assessment': result}, steps) is (adverse is None)


def test_native_detector_old_false_is_declared_but_never_qualification_proof():
    from eimemory.contracts.code_incident_provenance import NATIVE_UNPROVEN_DETECTORS
    for detector in NATIVE_UNPROVEN_DETECTORS:
        raw = {'origin': 'system_detector', 'detector': detector, **{key: False for key in FIELDS}}
        normal = qualification_provenance(raw)
        assert all(normal[key] is None for key in FIELDS)
        assert all(normal['declared_provenance'][key] is False for key in FIELDS)
        assert all(raw[key] is False for key in FIELDS)


__all__ = [name for name in globals() if name.startswith('test_')]
