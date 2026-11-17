"""Private namespace correlation for provider lifecycle and recall observations."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re

from eimemory.adapters.hermes.outbox import digest
from eimemory.adapters.hermes.profile_context import resolve_host_binding
from eimemory.adapters.runtime.channel import resolve_channel_scope

logger = logging.getLogger(__name__)
PREFIX = 'eimemory_hermes_session_observation '


def _identity(provider, session_id, context):
    if context is None:
        home, scope = provider._hermes_home, provider._scope
        sources = provider._source_ids()
    else:
        binding = resolve_host_binding(context)
        home = str(binding.get('hermes_home') or context.get('hermes_home')
                   or os.getenv('HERMES_HOME', '')).strip()
        scope = binding.get('scope') or provider._scope_from_context(context)
        sources = binding.get('source_ids') or provider._source_ids()
    home = str(Path(home).expanduser().resolve()) if home else ''
    scope = resolve_channel_scope('hermes', dict(scope))
    sources = sorted(sources)
    session = str(session_id or provider._session_id)
    return dict(home_digest=digest(home), scope_digest=digest(scope),
        source_ids_digest=digest(sources), session_digest=digest(session),
        namespace_digest=digest(dict(channel='hermes', home=home, scope=scope,
                                     source_ids=sources, session_id=session)))


def observe_session(provider, event, *, session_id='', context=None, retained_owner=None,
                    operation='', outcome='', capture_ref=''):
    """Emit fixed metadata. Signed captures establish their own evidence authority."""
    try:
        if event not in {'initialized', 'initialization_conflict', 'initialization_failed',
                         'session_rotated', 'closed', 'recall_result'}:
            return
        metadata = dict(schema='eimemory.hermes_session_observation.v1', event=event,
                        observation_only=True, **_identity(provider, session_id, context))
        if retained_owner is not None:
            metadata['retained_namespace_digest'] = _identity(retained_owner, session_id, None)['namespace_digest']
        if event == 'recall_result':
            if operation not in {'prefetch', 'explicit_recall'} or outcome not in {'returned', 'empty', 'unavailable'}:
                return
            metadata.update(operation=operation, outcome=outcome)
            if isinstance(capture_ref, str) and re.fullmatch(r'prec_[0-9a-f]{32}', capture_ref):
                metadata['capture_ref'] = capture_ref
        level = logging.WARNING if event in {'initialization_conflict', 'initialization_failed'} else logging.INFO
        logger.log(level, PREFIX + '%s', json.dumps(metadata, sort_keys=True, separators=(',', ':')))
    except Exception:
        # Diagnostics must leave the original binding, error and RPC result intact.
        return


def observe_recall(provider, response, *, operation, session_id='', delivered_context=None):
    outcome, capture_ref = 'unavailable', ''
    try:
        payload = response.get('result') if isinstance(response, dict) else None
        if (isinstance(response, dict) and response.get('ok') is True
                and isinstance(payload, dict) and payload.get('ok') is True
                and response.get('bypassed') is not True and payload.get('bypassed') is not True
                and payload.get('channel') == 'hermes'
                and payload.get('scope') == resolve_channel_scope('hermes', provider._scope)):
            context = payload.get('context') if delivered_context is None else delivered_context
            bundle = payload.get('bundle')
            rows = [payload.get('items')]
            if isinstance(bundle, dict):
                rows.extend(bundle.get(key) for key in ('items', 'persona', 'rules', 'reflections'))
            has_rows = any(isinstance(value, list) and value for value in rows)
            raw = bundle.get('raw_citation') if isinstance(bundle, dict) else None
            raw_delivered = (isinstance(raw, dict) and bool(raw.get('evidence'))
                             and (raw.get('presentation') or {}).get('status') == 'delivered')
            if operation == 'prefetch':
                raw_delivered = raw_delivered and delivered_context != payload.get('context')
            if isinstance(bundle, dict) or isinstance(payload.get('items'), list):
                outcome = ('returned' if isinstance(context, str) and context.strip()
                           and (has_rows or raw_delivered) else 'empty')
            capture = payload.get('capture')
            if isinstance(capture, dict):
                capture_ref = capture.get('record_id', '')
    except Exception:
        # Invalid diagnostic metadata cannot alter the already produced response.
        outcome, capture_ref = 'unavailable', ''
    observe_session(provider, 'recall_result', session_id=session_id, operation=operation,
                    outcome=outcome, capture_ref=capture_ref)
