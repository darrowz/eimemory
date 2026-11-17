"""Correlate a host answer callback with its original private recall request."""
from hashlib import sha256
import json
import logging
import os
import re

from eimemory.adapters.hermes.profile_context import verify_owner
from eimemory.adapters.hermes.session_observation import _identity
from eimemory.adapters.runtime.channel import resolve_channel_scope
from eimemory.evaluation.query_input_vault import configured_capture_scopes

logger = logging.getLogger(__name__)
PREFIX = 'eimemory_hermes_answer_observation '
MAX_ANSWER_CHARS = 1024 * 1024


def observe_answer(provider, *, session_id, host_turn_id, observed_host_turn_id, pending,
                   started_query_digest, started_host_query_digest, host_query_chars,
                   assistant_message):
    """Log digest metadata for later review of the exact observed callback body."""
    try:
        if os.environ.get('EIMEMORY_CAPTURE_ANSWER_OBSERVATIONS', '0') != '1':
            return
        if (not isinstance(assistant_message, str) or not assistant_message.strip()
                or len(assistant_message) > MAX_ANSWER_CHARS or not pending
                or pending.get('acceptance_generated') is True or not pending.get('decision_id')
                or not observed_host_turn_id or observed_host_turn_id != host_turn_id
                or not host_turn_id or str(host_turn_id).startswith('local-')):
            return
        verify_owner(provider._host_binding)
        query = pending.get('query')
        if not isinstance(query, str) or not query:
            return
        pre_query_digest = sha256(query.encode('utf-8', errors='replace')).hexdigest()
        expected_turn = pending.get('host_turn_id')
        if ((expected_turn and expected_turn != host_turn_id)
                or started_query_digest != pre_query_digest
                or not isinstance(started_host_query_digest, str)
                or re.fullmatch('[0-9a-f]{64}', started_host_query_digest) is None
                or type(host_query_chars) is not int or not 0 < host_query_chars <= 1024 * 1024):
            return
        from eimemory.retrieval.proactive import _bounded_text, _MAX_QUERY_CHARS
        query_digest = sha256(_bounded_text(query, _MAX_QUERY_CHARS).encode('utf-8', errors='replace')).hexdigest()
        scope = resolve_channel_scope('hermes', provider._scope)
        sources = sorted(provider._source_ids())
        if (pending.get('session_id') != session_id or session_id != provider._session_id
                or resolve_channel_scope('hermes', pending.get('scope')) != scope
                or sorted(pending.get('source_ids', [])) != sources):
            return
        allowed = configured_capture_scopes()
        if not allowed or not any(
                {key: entry[key] for key in scope} == scope
                and entry['channel'] == 'hermes' and sources == [entry['source_id']]
                for entry in allowed):
            return
        metadata = dict(schema='eimemory.hermes_answer_observation.v2', event='answer_observed',
            observation_only=True, verified_task_outcome=False, **_identity(provider, session_id, None),
            decision_digest=sha256(str(pending['decision_id']).encode()).hexdigest(),
            query_digest=query_digest, answer_digest=sha256(assistant_message.encode('utf-8', errors='replace')).hexdigest(),
            host_query_digest=started_host_query_digest, host_query_chars=host_query_chars,
            pre_query_digest=pre_query_digest, query_normalization='proactive-whitespace-collapse.v1',
            answer_chars=len(assistant_message),
            host_turn_digest=sha256(str(host_turn_id).encode()).hexdigest())
        logger.info(PREFIX + '%s', json.dumps(metadata, sort_keys=True, separators=(',', ':')))
    except Exception:
        return
