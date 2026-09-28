"""Explicit exact-scope selection for acceptance and replay CLI commands."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_SCOPE_KEYS = ('tenant_id', 'agent_id', 'workspace_id', 'user_id')


def add_selection_arguments(parser: Any) -> None:
    for name in ('tenant', 'agent', 'workspace', 'user'):
        parser.add_argument('--scope-' + name, default=None)
    parser.add_argument('--at-time', default='')
    parser.add_argument('--selection-only', action='store_true',
                        help='Resolve catalog targets only; does not execute acceptance or persist probes.')


def selection_scope(parsed: Any, defaults: Mapping[str, Any]) -> dict[str, str]:
    result = {}
    for key in _SCOPE_KEYS:
        override = getattr(parsed, 'scope_' + key[:-3], None)
        value = defaults.get(key, 'default' if key == 'tenant_id' else '') if override is None else override
        if not isinstance(value, str) or '\x1f' in value or '\x00' in value:
            raise ValueError('capability_scope_fields_invalid')
        result[key] = value
    if not result['tenant_id']:
        raise ValueError('capability_tenant_required')
    return result


def run_selected_capability(runtime: Any, parsed: Any, defaults: Mapping[str, Any],
                            *, profile_key: str, operation: str) -> dict:
    """Keep selector failures inspectable; never try another scope or legacy set."""
    if operation not in {'acceptance', 'replay'}:
        raise ValueError('unknown_capability_cli_operation')
    scope = selection_scope(parsed, defaults)
    legacy = bool(getattr(parsed, 'legacy_compatibility', False))
    only = bool(getattr(parsed, 'selection_only', False))
    capability_scope = str(getattr(parsed, 'capability_scope', 'global'))
    at_time = str(getattr(parsed, 'at_time', '') or '')
    request = {'runtime_scope': scope, 'profile_key': profile_key,
               'profile_argument': str(getattr(parsed, 'profile', '') or ''),
               'capability_scope': capability_scope, 'at_time': at_time}
    if legacy and (profile_key or only or at_time):
        return {'ok': False, 'reason': 'legacy_profile_selection_conflict', 'selection_request': request}
    kwargs = dict(scope=scope, runtime_scope=scope, profile_key=profile_key,
                  capability_scope=capability_scope, at_time=at_time, legacy_compatibility=legacy)
    if only:
        from eimemory.evaluation.capability_catalog import resolve_application_capability_catalog
        try:
            catalog = resolve_application_capability_catalog()
        except Exception as exc:
            return {'ok': False, 'reason': 'evaluation_catalog_unavailable',
                    'exception_type': type(exc).__name__, 'selection_request': request,
                    'acceptance_executed': False, 'admission_ok': False, 'persisted': False}
        if profile_key:
            selected = catalog.resolve_profile_cases(runtime, profile_key=profile_key,
                runtime_scope=scope, capability_scope=capability_scope, at_time=at_time)
        else:
            selected = catalog.resolve_active_cases(runtime, runtime_scope=scope,
                capability_scope=capability_scope, at_time=at_time)
        return {'ok': selected.get('ok') is True, 'report_type': 'capability_selection_preflight',
                'selection': selected, 'selection_request': request, 'acceptance_executed': False,
                'admission_ok': False, 'persisted': False}
    if operation == 'acceptance':
        report = runtime.run_capability_acceptance(**kwargs, persist=True)
    elif operation == 'replay':
        report = runtime.build_capability_replay_packs(**kwargs,
            capabilities=list(getattr(parsed, 'capability', []) or []) or None,
            persist=bool(getattr(parsed, 'persist', False)), loop_id='cli_capability_replay')
    else:
        raise ValueError('unknown_capability_cli_operation')
    if not isinstance(report, dict):
        return {'ok': False, 'reason': 'capability_report_not_object', 'selection_request': request}
    return {**report, 'selection_request': request}
