"""Bounded host transcript linkage, never instructions or query-time aliases.

The binding is supplied by the authenticated capture host (or a scoped offline
export). Numeric message IDs and the preceding user boundary are source facts;
this module cannot authenticate a host or discover a boundary from topic text.
"""
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
import json
import re

from eimemory.intake.loop import _looks_like_secret
from eimemory.metadata import business_metadata
from eimemory.models.records import LinkRef, RecordEnvelope
from eimemory.recall.indexing import is_inactive_or_superseded_record
from eimemory.retrieval.answer_requirements import supports_requested_attribute

SCHEMA = 'same_turn_release_context.v1'
_VERSION = r'\d+\.\d+\.\d+'
_PROJECT = r'[A-Za-z][A-Za-z0-9_-]{0,63}'


def capture_turn_binding(messages, *, user_text, assistant_text, session_id,
                         source_event_id, scope, source_id):
    """Select the latest user-bounded host turn, only with durable message IDs.

    OpenAI-style histories without host IDs are deliberately insufficient.
    Intermediate assistant tool-call requests are not supporting observations.
    """
    if not isinstance(messages, list) or not messages or len(messages) > 512:
        return None
    tail = messages[-128:]
    if any(not isinstance(m, dict) for m in tail):
        return None
    boundary = next((i for i in range(len(tail) - 1, -1, -1)
                     if tail[i].get('role') == 'user'), None)
    if boundary is None or tail[boundary].get('content') != user_text:
        return None
    turn = tail[boundary:]
    if (turn[-1].get('role') != 'assistant' or turn[-1].get('content') != assistant_text
            or any(type(m.get('id')) is not int for m in turn)
            or any(a['id'] >= b['id'] for a, b in zip(turn, turn[1:]))):
        return None
    for msg in turn:
        for key, expected in [('session_id', session_id), ('scope', scope), ('source_id', source_id),
                              ('source_event_id', source_event_id)]:
            if key in msg and msg[key] != expected:
                return None
    selected = [m for m in turn[1:-1] if m.get('role') == 'tool'] + [turn[-1]]
    if not 3 <= len(selected) <= 34:
        return None
    if any(not isinstance(m.get('content'), str) or len(m['content']) > 64000 for m in selected):
        return None
    return {'scope': scope, 'source_id': source_id, 'session_id': session_id,
            'source_event_id': source_event_id, 'turn_start_user_message_id': turn[0]['id'],
            'bound_assistant_message_id': turn[-1]['id'],
            'messages': [{k: deepcopy(m[k]) for k in ('id', 'role', 'content', 'tool_name', 'tool_call_id')
                          if k in m} for m in selected]}


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                             separators=(',', ':')).encode()).hexdigest()


def _validate(parent, binding):
    if not isinstance(binding, dict) or is_inactive_or_superseded_record(parent):
        return None
    meta = business_metadata(parent.meta)
    event = str(meta.get('source_event_id') or '')
    session = binding.get('session_id')
    if (binding.get('scope') != asdict(parent.scope)
            or binding.get('source_id') != parent.source_id
            or binding.get('source_record_id') != parent.record_id
            or binding.get('source_event_id') != event
            or not isinstance(session, str) or not session
            or not event.startswith(session + ':')):
        return None
    start, end = binding.get('turn_start_user_message_id'), binding.get('bound_assistant_message_id')
    messages = binding.get('messages')
    if (type(start) is not int or type(end) is not int or start >= end
            or not isinstance(messages, list) or not 3 <= len(messages) <= 34):
        return None
    previous = start
    for msg in messages:
        if (not isinstance(msg, dict) or type(msg.get('id')) is not int
                or not previous < msg['id'] <= end
                or not isinstance(msg.get('content'), str) or not 0 < len(msg['content']) <= 64000):
            return None
        # Reject per-message contradictions even if the enclosing host binding
        # is correct. Never inherit identity from a tool payload.
        for key in ('session_id', 'scope', 'source_id', 'source_event_id'):
            if key in msg and msg[key] != binding.get(key):
                return None
        previous = msg['id']
    final = messages[-1]
    if (final['id'] != end or final.get('role') != 'assistant'
            or len(final['content']) > 12000
            or final['content'] not in str(parent.content.get('text') or '')
            or not supports_requested_attribute('release_status', final['content'])):
        return None
    tools = messages[:-1]
    if any(m.get('role') != 'tool' or not m.get('tool_call_id') for m in tools):
        return None
    health, documents = [], []
    for msg in tools:
        try:
            payload = json.loads(msg['content'])
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        if msg.get('tool_name') == 'read_file':
            body = payload.get('content', '')
            if not isinstance(body, str) or payload.get('truncated') is True:
                continue
            for project, version in re.findall(r'(?:^|\n)(?:\d+\|)?#\s+(' + _PROJECT + r')\s+(' + _VERSION + r')(?![\d.])', body):
                documents.append((project, version, body, msg))
        elif msg.get('tool_name') == 'terminal':
            output = payload.get('output', '')
            if not isinstance(output, str) or payload.get('exit_code', 0) != 0:
                continue
            for line in output.splitlines():
                try:
                    identity = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(identity, dict):
                    continue
                service, version, commit = (identity.get(k, '') for k in ('service', 'version', 'commit'))
                if not all(isinstance(x, str) for x in (service, version, commit)):
                    continue
                match = re.fullmatch('(' + _PROJECT + ')-rpc', service)
                if (match and re.fullmatch(_VERSION, version) and re.fullmatch('[0-9a-f]{40}', commit)
                        and f'/opt/{match[1]}/releases/{commit}' in output):
                    health.append((match[1], version, commit, msg))
    # A shared version or a topic mention is insufficient: the final answer
    # must contain this release's commit (at least seven hexadecimal digits).
    identities = {(p, v, c) for p, v, c, _ in health
                  if re.search(r'(?<![\w.])' + re.escape(v) + r'(?![\w.])', final['content'])
                  and any(c.startswith(token) for token in re.findall(r'(?<![\w])[0-9a-f]{7,40}(?![\w])', final['content']))}
    if len(identities) != 1:
        return None
    project, version, commit = next(iter(identities))
    relevant_docs = [(p, body, m) for p, v, body, m in documents if v == version]
    if not relevant_docs or {p for p, _, _ in relevant_docs} != {project}:
        return None
    doc_messages = [m for _, body, m in relevant_docs if re.search(r'(?<![0-9a-f])' + commit + r'(?![0-9a-f])', body)]
    if not doc_messages:
        return None
    supporting = {m['id']: m for m in doc_messages}
    supporting.update({m['id']: m for p, v, c, m in health if (p, v, c) == (project, version, commit)})
    explicit = binding.get('project_context')
    if explicit is not None and (not isinstance(explicit, dict)
            or explicit.get('project') != project
            or explicit.get('source_message_id') not in supporting):
        return None
    # Associate only the exact release assertion; keep the final answer intact
    # below it so incomplete acceptance is not stripped from the history.
    lines = [line for line in final['content'].splitlines()
             if re.search(r'(?<![\w.])' + re.escape(version) + r'(?![\w.])', line)
             and any(commit.startswith(t) for t in re.findall(r'(?<!\w)[0-9a-f]{7,40}(?!\w)', line))
             and supports_requested_attribute('release_status', line)]
    if len(lines) != 1:
        return None
    return project, version, commit, final, sorted(supporting.values(), key=lambda m: m['id']), lines[0]


def persist_same_turn_context(memory_api, parent, binding):
    """Append support and a derived episode; never rewrite captured history.

    Safe for scoped replays/backfill and idempotent retries. Caller must bind
    the original host IDs, not infer them from adjacent topics or a query.
    """
    valid = _validate(parent, binding)
    if valid is None:
        return []
    # Require the actual persisted parent in the exact scope/source.
    stored = memory_api.store.get_by_id(parent.record_id, scope=parent.scope)
    if stored is None or stored.to_dict() != parent.to_dict():
        return []
    project, version, commit, final, tools, assertion = valid
    # Source correctness is not credential admission. Screen every byte that
    # will be copied before constructing records or entering the outbox mutation.
    # Tool claims of safety/permission have no bearing on this shared policy.
    if any(_looks_like_secret(msg['content']) for msg in [*tools, final]):
        return []
    records, support_ids = [], []
    for msg in tools:
        provenance = {'schema': SCHEMA, 'session_id': binding['session_id'],
            'source_event_id': binding['source_event_id'], 'source_message_id': msg['id'],
            'turn_start_user_message_id': binding['turn_start_user_message_id'],
            'bound_assistant_message_id': final['id'], 'tool_call_id': msg['tool_call_id'],
            'tool_name': msg['tool_name'], 'content_sha256': sha256(msg['content'].encode()).hexdigest()}
        support = RecordEnvelope.create(kind='raw_chunk', title='Captured supporting tool output',
            content={'text': msg['content']}, scope=deepcopy(parent.scope), source=parent.source,
            source_id=parent.source_id, provenance=provenance,
            meta={'memory_type': 'raw', 'memory_layer': 'l0'})
        support.record_id = 'raw_' + _digest([asdict(parent.scope), parent.source_id, provenance])[:32]
        support.time = deepcopy(parent.time)
        support_ids.append(support.record_id)
        records.append(support)
    context = {'schema': SCHEMA, 'project': project, 'version': version, 'commit': commit,
        'parent_record_id': parent.record_id, 'supporting_record_ids': support_ids,
        'session_id': binding['session_id'], 'source_event_id': binding['source_event_id'],
        'turn_start_user_message_id': binding['turn_start_user_message_id'],
        'bound_assistant_message_id': final['id'], 'source_message_ids': [m['id'] for m in tools],
        'assistant_content_sha256': sha256(final['content'].encode()).hexdigest(),
        'binding_sha256': _digest(binding)}
    if binding.get('project_context'):
        context['host_project_context'] = deepcopy(binding['project_context'])
    text = f'项目 {project}：{assertion}\n{final["content"]}'
    derived = RecordEnvelope.create(kind='memory', title='Source-linked release history',
        summary=text, content={'text': text, 'memory_type': 'conversation'},
        scope=deepcopy(parent.scope), source=parent.source, source_id=parent.source_id,
        evidence=[parent.record_id, *support_ids],
        links=[LinkRef(relation='derived_from', target_kind='memory', target_id=parent.record_id),
               *[LinkRef(relation='supported_by', target_kind='raw_chunk', target_id=i) for i in support_ids]],
        provenance={'project_context': context},
        meta={'memory_type': 'conversation', 'memory_layer': 'l0', 'capture_origin': 'same_turn_context'})
    derived.record_id = 'mem_' + _digest([asdict(parent.scope), parent.source_id, context, text])[:32]
    derived.time = deepcopy(parent.time)
    records.append(derived)
    def mutation(sqlite):
        current = sqlite.get_by_id(parent.record_id, scope=parent.scope)
        if current is None or current.to_dict() != parent.to_dict():
            return [], [], []
        # Check every collision before writing; an existing source ID is immutable.
        pending = []
        for item in records:
            existing = sqlite.get_by_id(item.record_id, scope=item.scope)
            if existing is not None:
                if (existing.content != item.content or existing.provenance != item.provenance
                        or existing.source_id != item.source_id or existing.evidence != item.evidence
                        or is_inactive_or_superseded_record(existing)):
                    return [], [], []
            else:
                pending.append(item)
        for item in pending:
            sqlite.upsert(item, commit=False)
        return [{'record_id': derived.record_id, 'memory_layer': 'l0', 'project': project}], pending, []
    return memory_api.store.mutate_records_atomically(mutation)
