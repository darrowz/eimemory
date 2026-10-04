from threading import RLock
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from eimemory.contracts.capability_models import CapabilityBinding
from eimemory.contracts.capability_validators import CapabilityContractError
from eimemory.governance.learning.candidate_search import _max_risk_level
from eimemory.governance.release.closure_verdict import failure_signals, summarize_release_closure
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.runtime_store import RuntimeStore


@pytest.mark.parametrize('allowlist', ['global', {'global': True}, {'global'}, [True], ['']])
def test_binding_validates_raw_allowlist_before_normalization(allowlist):
    with pytest.raises(CapabilityContractError):
        CapabilityBinding(
            binding_id='fake-binding', capability_id='inventory.check', capability_revision_id='fake-revision',
            provider_kind='module', provider_instance_id='fake-provider', implementation_digest='a'*64,
            operations=('inspect',), limits={'max_items': 1}, environment_fingerprint={'os': 'fake'},
            applicability={'allowed_scopes': allowlist}, advertisement_evidence_refs=('fake-evidence',),
            created_at='2026-10-03T00:00:00Z')


def test_valid_allowlist_binding_positive_control():
    binding = CapabilityBinding(
        binding_id='fake-binding', capability_id='inventory.check', capability_revision_id='fake-revision',
        provider_kind='module', provider_instance_id='fake-provider', implementation_digest='a'*64,
        operations=('inspect',), limits={'max_items': 1}, environment_fingerprint={'os': 'fake'},
        applicability={'allowed_scopes': ['global']}, advertisement_evidence_refs=('fake-evidence',),
        created_at='2026-10-03T00:00:00Z')
    assert binding.applicability['allowed_scopes'] == ('global',)


@pytest.mark.parametrize('operation', ['append', 'append_and_supersede'])
def test_insert_once_cannot_return_other_source_partition(operation):
    scope = ScopeRef(tenant_id='fake')
    incoming = RecordEnvelope.create(kind='memory', title='fake', source_id='partition-a', scope=scope)
    existing = RecordEnvelope.from_dict(incoming.to_dict())
    existing.source_id = 'partition-b'
    store = object.__new__(RuntimeStore)
    store._lock = RLock()
    store.sqlite = SimpleNamespace(in_transaction=False, execute=Mock(),
        get_by_id=Mock(return_value=existing), commit=Mock(), rollback=Mock())
    store._existing_reflection_duplicate = lambda record: None
    with pytest.raises(ValueError, match='source_id conflict'):
        getattr(store, operation)(incoming, existing_match=lambda record: True)
    store.sqlite.commit.assert_not_called()
    store.sqlite.rollback.assert_called_once_with()


@pytest.mark.parametrize('other', ['medium', 'unknown', ''])
def test_low_risk_does_not_hide_review_required_peer(other):
    assert _max_risk_level(['low', other]) != 'low'


@pytest.mark.parametrize('field', ['error', 'contract_error', 'gate_errors', 'blocked_reasons'])
def test_compact_gap_reason_cannot_hide_error_sibling(field):
    report = {'report_type': 'l5_release_closure', 'ok': False, 'closure_complete': False,
              'data_accumulating': False, 'blocked_stage': 'readiness',
              'blocked_reason': 'bootstrap_pending_non_recall_l5_evidence_incomplete',
              'deployment': {'commit': 'a'*40, 'promotion_request_id': 'fake'},
              'deployment_receipt': {'release_session_id': 'fake'},
              'readiness': {'gaps': [{'reason': 'awaiting_evidence', field: 'source_identity_mismatch'}]}}
    signals = failure_signals(report)
    assert any(item['code'] == 'source_identity_mismatch' for item in signals['hard_errors'])
    result = summarize_release_closure(report)
    assert result['ordinary_release_admission'] == 'denied'
    assert result['l5_certification'] != 'certified'
    assert result['production_quality'] != 'certified'
