"""Scope overrides and selection diagnostics do not authorize evidence or fallback."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import sys
from types import ModuleType, SimpleNamespace

import pytest
from eimemory.cli.capability_selection import (add_selection_arguments, selection_scope,
                                             run_selected_capability)
from eimemory.evaluation.selection_diagnostics import profile_resolution_diagnostics

DEFAULT = dict(tenant_id='t', agent_id='legacy', workspace_id='old', user_id='old-user')

def args(*argv):
    parser = argparse.ArgumentParser()
    add_selection_arguments(parser)
    parser.add_argument('--profile', default='l5.default')
    parser.add_argument('--capability-scope',default='global')
    parser.add_argument('--legacy-compatibility',action='store_true')
    parser.add_argument('--persist',action='store_true')
    return parser.parse_args(argv)


def test_scope_override_is_exact_including_explicit_empty_user():
    parsed = args('--scope-agent','honrui','--scope-workspace','embodied::channel::hermes',
                  '--scope-user','')
    result = selection_scope(parsed, DEFAULT)
    assert result == dict(tenant_id='t',agent_id='honrui',
                         workspace_id='embodied::channel::hermes',user_id='')
    assert DEFAULT['user_id'] == 'old-user'


@pytest.mark.parametrize('value', ['x\x1fy', 'x\0y', 3, None])
def test_invalid_default_scope_is_not_silently_normalized(value):
    default = {**DEFAULT, 'agent_id':value}
    with pytest.raises(ValueError):selection_scope(args(), default)


@pytest.mark.parametrize('operation', ['acceptance','replay'])
def test_dispatch_uses_one_exact_scope_and_time(operation):
    calls=[]
    def call(**kw):calls.append(kw);return {'ok':False,'reason':'profile_resolution_failed'}
    runtime=SimpleNamespace(run_capability_acceptance=call,build_capability_replay_packs=call)
    parsed=args('--scope-tenant','tenant2','--scope-agent','honrui','--scope-user','user2',
                '--at-time','2026-09-28T10:00:00Z')
    result=run_selected_capability(runtime,parsed,DEFAULT,profile_key='l5.default',operation=operation)
    assert len(calls)==1
    assert calls[0]['scope']==calls[0]['runtime_scope']==result['selection_request']['runtime_scope']
    assert calls[0]['scope']['tenant_id']=='tenant2'
    assert calls[0]['at_time']=='2026-09-28T10:00:00Z'
    assert result['ok'] is False
    assert calls[0]['legacy_compatibility'] is False


@pytest.mark.parametrize('selected_ok', [True,False])
def test_selection_only_calls_catalog_without_acceptance(monkeypatch,selected_ok):
    calls=[]
    catalog=SimpleNamespace(resolve_profile_cases=lambda runtime,**kw:
        calls.append(kw) or {'ok':selected_ok,'cases':[]})
    # This test deliberately replaces only the catalog boundary, not the selector implementation.
    mod=ModuleType('eimemory.evaluation.capability_catalog')
    mod.resolve_application_capability_catalog=lambda:catalog
    monkeypatch.setitem(sys.modules,mod.__name__,mod)
    result=run_selected_capability(object(),args('--selection-only'),DEFAULT,
                                  profile_key='l5.default',operation='acceptance')
    assert result['ok'] is selected_ok and len(calls)==1
    assert result['acceptance_executed'] is False
    assert result['persisted'] is False and result['admission_ok'] is False


def test_explicit_profile_never_falls_back_to_legacy():
    result=run_selected_capability(object(),args('--legacy-compatibility'),DEFAULT,
                                  profile_key='l5.default',operation='acceptance')
    assert result['ok'] is False and result['reason']=='legacy_profile_selection_conflict'


@pytest.mark.parametrize(('message','code'),[
    ("active profile 'secret-profile' is not available in this exact scope",'profile_not_available_in_exact_scope'),
    ("stored profile 'x' does not satisfy its typed contract",'stored_descriptor_contract_invalid'),
    ("stored definition 'x' digest does not match its descriptor",'stored_descriptor_digest_mismatch'),
    ("stored profile key does not match its lineage index",'profile_lineage_index_mismatch'),
    ("profile exact requirement 'x' has no active capability definition",'profile_required_capability_unavailable'),
    ("active capabilities exceed budget; refusing truncation",'profile_resolution_budget_exceeded'),
    ("conflicting same-priority selector requirements",'profile_selector_conflict'),
])
def test_profile_diagnostics_are_structured_without_raw_error(message,code):
    error=ValueError(message);error.__cause__=TypeError('secret-token-no-echo')
    result=profile_resolution_diagnostics(error,profile_key='l5.default',
                                        capability_scope='global',runtime_scope=DEFAULT)
    assert result['reason_code']==code
    assert result['runtime_scope']==DEFAULT
    assert len(result['exception_chain'])==2
    assert 'secret-token-no-echo' not in str(result)
    assert 'secret-profile' not in str(result)
    assert not result['fallback_applied'] and not result['profile_mutated']


def test_catalog_exception_path_keeps_safe_cause_detail():
    from eimemory.evaluation.capability_catalog import CapabilityEvaluationCatalog
    from eimemory.models.records import ScopeRef
    calls=[]
    def broken(key, **kw):
        calls.append((key,kw))
        try:
            raise TypeError('secret-value-not-public')
        except TypeError as cause:
            raise ValueError("active profile 'l5.default' is not available in this exact scope") from cause
    runtime=SimpleNamespace(capabilities=SimpleNamespace(resolve_profile=broken))
    selected=CapabilityEvaluationCatalog().resolve_profile_cases(runtime,profile_key='l5.default',
        runtime_scope=ScopeRef.from_dict(DEFAULT),capability_scope='global')
    assert selected['ok'] is False and selected['reason']=='profile_resolution_failed'
    assert selected['detail']=='ValueError'
    assert selected['diagnostics']['reason_code']=='profile_not_available_in_exact_scope'
    assert len(selected['diagnostics']['exception_chain'])==2
    assert 'secret-value-not-public' not in str(selected)
    assert len(calls)==1
