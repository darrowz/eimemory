"""Optional native origin collector with an independent, rollback-safe outbox."""
from collections import OrderedDict
from copy import deepcopy
import importlib
from hashlib import sha256
import hmac
import json
import os
from pathlib import Path
import threading
import time
from uuid import uuid4

from eimemory.adapters.hermes.profile_context import verify_owner, spawn_owned_thread
from eimemory.adapters.hermes.host_context import hermes_producer_token
from eimemory.adapters.runtime.channel import resolve_channel_scope
from eimemory.adapters.runtime.host_auth import _read_private_file, is_strong_producer_token
from eimemory.retrieval.input_origin import SCHEMA, UNKNOWN, EXTERNAL, validate_claim, digest, text_digest
from eimemory.storage.atomic_file import interprocess_lock
from eimemory.core.strict_json import loads as strict_json_loads

OUTBOX_SCHEMA = 'hermes.input_origin_outbox.v1'
MAX_ITEMS = 16
MAX_BYTES = 64 * 1024
MAX_TURN_SECONDS = 2 * 60 * 60
PARAMS = {'channel', 'scope', 'source_ids', 'session_id', 'turn_id', 'decision_id', 'origin'}


def _seal_outbox(value):
    key = hermes_producer_token()
    if not is_strong_producer_token(key):
        raise ValueError('input_origin_queue_key_unavailable')
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    return hmac.new(key.encode(), OUTBOX_SCHEMA.encode()+b'\0'+raw, sha256).hexdigest()


def enabled():
    return os.environ.get('EIMEMORY_CAPTURE_INPUT_ORIGIN', '0') == '1'


def _snapshot(provider, session, message):
    verify_owner(provider._host_binding)
    native = importlib.import_module('gateway.eimemory_input_origin')
    value = native.snapshot(session_id=session, message=message)
    binding = provider._host_binding or {}
    if (not isinstance(value, dict) or not binding.get('native') or session != provider._session_id
            or value.get('profile_name') != binding.get('profile_name')
            or value.get('home_digest') != text_digest(str(Path(provider._hermes_home).expanduser().resolve()))
            or value.get('actor_digest') != text_digest(provider._scope['user_id'])):
        return None
    return value


class OriginObserver:
    """Metadata-only. Never writes EffectObserver's legacy retry ledger."""
    def __init__(self, provider):
        self.provider = provider
        self.lock = threading.RLock()
        self.flush_lock = threading.Lock()
        self.turns = OrderedDict()
        self.pending = OrderedDict()
        self.worker = None
        self.error = ''
        self.loaded = False
        self.sent = 0
        self.namespace = dict(channel='hermes', scope=resolve_channel_scope('hermes', provider._scope),
                              source_ids=sorted(provider._source_ids()))
        self.path = Path(provider._hermes_home) / 'logs' / ('eimemory-input-origin-v1-' + digest(self.namespace)[:24] + '.json')

    def pre(self, session, turn, message):
        self.request_flush()
        if not turn or str(turn).startswith('local-'):
            return
        value = _snapshot(self.provider, session, message)
        if not value or value.get('origin_kind') == UNKNOWN:
            return
        with self.lock:
            key = session, turn
            previous = self.turns.get(key)
            # A reused host turn with different input/capability is ambiguous.
            if previous and previous.get('snapshot') != value:
                self.turns[key] = {'conflicted': True}
                return
            if previous is None:
                self.turns[key] = dict(snapshot=deepcopy(value), started=time.monotonic())
            while len(self.turns) > 128:
                self.turns.popitem(last=False)

    def post(self, session, turn, message, pending):
        with self.lock:
            state = self.turns.pop((session, turn), None)
        if (not state or state.get('conflicted') or not pending or not turn
                or not 0 <= time.monotonic() - state['started'] <= MAX_TURN_SECONDS):
            return
        snapshot = _snapshot(self.provider, session, message)
        if snapshot != state['snapshot']:
            return
        exact = self.namespace
        if (pending.get('session_id') != session or session != self.provider._session_id
                or pending.get('host_turn_id') not in (None, '', turn)
                or resolve_channel_scope('hermes', pending.get('scope')) != exact['scope']
                or sorted(pending.get('source_ids', [])) != exact['source_ids']
                or not pending.get('decision_id') or not pending.get('decision_turn_id')):
            return
        from eimemory.adapters.hermes.provider_core import host_message_text
        raw = host_message_text(message)
        pre_query = raw.strip()[:8000]
        if pending.get('query') != pre_query:
            return
        if snapshot['origin_kind'] == EXTERNAL and pending.get('acceptance_generated') is not False:
            return
        from eimemory.retrieval.proactive import _bounded_text, _MAX_QUERY_CHARS
        query = _bounded_text(pre_query, _MAX_QUERY_CHARS)
        claim = validate_claim(dict(schema=SCHEMA, **snapshot,
            host_turn_digest=text_digest(turn), query_digest=text_digest(query)))
        params = dict(**exact, session_id=session, turn_id=pending['decision_turn_id'],
                      decision_id=pending['decision_id'], origin=claim)
        self.enqueue(params)

    def _validate_params(self, params):
        if (not isinstance(params, dict) or set(params) != PARAMS
                or any(params.get(key) != value for key,value in self.namespace.items())
                or not all(isinstance(params[key], str) and params[key].strip() and len(params[key]) <= 500
                           for key in ('session_id', 'turn_id', 'decision_id'))):
            raise ValueError('input_origin_queue_invalid')
        validate_claim(params['origin'])

    def _load(self):
        self.loaded = True
        self.pending = OrderedDict()
        if not self.path.exists():
            if self.error == 'input_origin_queue_unreadable':
                self.error = ''
            return
        try:
            raw = _read_private_file(self.path, max_bytes=MAX_BYTES)
            if not raw:
                raise ValueError('input_origin_queue_invalid')
            value = strict_json_loads(raw, max_bytes=MAX_BYTES, max_depth=8)
            if (not isinstance(value, dict) or set(value) != {'schema', 'namespace_digest', 'items', 'signature'}
                    or value['schema'] != OUTBOX_SCHEMA or value['namespace_digest'] != digest(self.namespace)
                    or not isinstance(value['items'], list) or len(value['items']) > MAX_ITEMS):
                raise ValueError('input_origin_queue_invalid')
            signature = value.pop('signature')
            if not isinstance(signature, str) or not hmac.compare_digest(signature, _seal_outbox(value)):
                raise ValueError('input_origin_queue_invalid')
            parsed = OrderedDict()
            for params in value['items']:
                self._validate_params(params)
                key = params['decision_id']
                if key in parsed and parsed[key] != params:
                    raise ValueError('input_origin_queue_invalid')
                parsed[key] = params
            self.pending = parsed
            if self.error == 'input_origin_queue_unreadable':
                self.error = ''
        except (OSError, TypeError, ValueError, KeyError):
            self.error = 'input_origin_queue_unreadable'

    def _persist(self):
        value = dict(schema=OUTBOX_SCHEMA, namespace_digest=digest(self.namespace), items=list(self.pending.values()))
        value['signature'] = _seal_outbox(value)
        raw = (json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False) + '\n').encode()
        if len(raw) > MAX_BYTES:
            raise ValueError('input_origin_queue_capacity')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError('input_origin_queue_untrusted')
        temporary = self.path.with_name(self.path.name + '.' + uuid4().hex + '.tmp')
        try:
            descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def enqueue(self, params):
        self._validate_params(params)
        try:
            with self.lock, interprocess_lock(self.path.with_suffix('.lock'), timeout=0.25):
                # Cache snapshots never authorize a disk mutation. Re-read the
                # signed state after acquiring the cross-instance/process lock.
                self._load()
                if self.error == 'input_origin_queue_unreadable':
                    return False
                prior = self.pending.get(params['decision_id'])
                if prior is not None and prior != params:
                    self.error = 'input_origin_queue_identity_conflict'
                    return False
                if len(self.pending) >= MAX_ITEMS and prior is None:
                    self.error = 'input_origin_queue_capacity'
                    return False
                before = deepcopy(self.pending)
                self.pending.setdefault(params['decision_id'], deepcopy(params))
                try:
                    self._persist()
                except (OSError, ValueError):
                    self.pending = before
                    raise
        except (OSError, ValueError):
            self.error = 'input_origin_queue_persist_failed'
            return False
        self.request_flush()
        return True

    def _ack(self, key, sent_params):
        try:
            with self.lock, interprocess_lock(self.path.with_suffix('.lock'), timeout=0.25):
                self._load()
                if self.error == 'input_origin_queue_unreadable':
                    return False
                if self.pending.get(key) != sent_params:
                    # A late ACK may see no entry or a later admission with the
                    # same decision key. It has no authority over either state.
                    return False
                before = deepcopy(self.pending)
                self.pending.pop(key)
                try:
                    self._persist()
                except (OSError, ValueError):
                    self.pending = before
                    raise
                return True
        except (OSError, ValueError):
            self.error = 'input_origin_queue_persist_failed'
            return False

    def request_flush(self):
        with self.lock:
            self._load()
            if not self.pending or self.error == 'input_origin_queue_unreadable' or self.worker and self.worker.is_alive():
                return
            self.worker = spawn_owned_thread(self.flush, owner_context=self.provider._owner_context,
                name='eimemory-input-origin', daemon=True)
            self.worker.start()

    def flush(self):
        try:
            self._flush()
        except Exception:
            self.error = 'input_origin_delivery_unavailable'

    def _flush(self):
        with self.flush_lock, interprocess_lock(self.path.with_suffix('.consumer.lock'), timeout=0):
            from eimemory.adapters.hermes.provider_core import hermes_attestation_client_from_env
            client = self.provider._attestation_client or hermes_attestation_client_from_env(
                hermes_home=self.provider._hermes_home, timeout_seconds=1.0)
            if client is None:
                self.error = 'input_origin_producer_unavailable'
                return
            with self.lock, interprocess_lock(self.path.with_suffix('.lock'), timeout=0.25):
                self._load()
                if self.error == 'input_origin_queue_unreadable':
                    return
                pending = list(self.pending.items())[:4]
            for key, params in pending:
                response = self.provider._safe_call_with(client, 'adapter.attest_input_origin', params)
                success = response.get('ok') is True and isinstance(response.get('result'), dict) and response['result'].get('ok') is True
                diagnostic = response.get('diagnostic') or {}
                permanent = response.get('error') in {'invalid_request', 'unknown_method'} or diagnostic.get('http_status') == 400
                if success or permanent:
                    if self._ack(key, params):
                        self.sent += int(success)
                        self.error = '' if success else 'input_origin_unsupported_or_rejected'
                else:
                    self.error = 'input_origin_delivery_unavailable'
                    break


def observe_pre(provider, session, turn, message):
    if not enabled():
        return
    try:
        if not provider._hermes_home or not (provider._host_binding or {}).get('native'):
            return
        observer = getattr(provider, '_input_origins', None)
        if observer is None:
            observer = provider._input_origins = OriginObserver(provider)
        observer.pre(session, turn, message)
    except Exception:
        return


def observe_post(provider, session, turn, message, pending):
    if not enabled():
        return
    try:
        observer = getattr(provider, '_input_origins', None)
        if observer is not None:
            observer.post(session, turn, message, pending)
    except Exception:
        return
