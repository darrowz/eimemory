"""Native event/input capabilities for the optional eimemory origin companion.

Copied verbatim to gateway/eimemory_input_origin.py by its separate host patch.
No origin comes from prompt text, request kwargs, or a plugin-supplied mapping.
This proves platform/native provenance, never that an actor is biologically human.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import threading
from uuid import uuid4

EXTERNAL = 'host_attested_external_input'
INTERNAL = 'host_attested_internal_input'
UNKNOWN = 'unknown'
_ISSUER = object()
_seed = ContextVar('eimemory_native_event_seed', default=None)
_bound = ContextVar('eimemory_native_input_origin', default=None)
_PLATFORMS = frozenset({'feishu', 'telegram', 'discord', 'slack', 'signal', 'whatsapp', 'matrix'})


def _hash(value):
    return sha256(value.encode('utf-8', errors='replace')).hexdigest()


def _text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [part if isinstance(part, str) else part.get('text', '')
                 if isinstance(part, dict) else '' for part in value[:64]]
        return '\n'.join(part for part in parts if isinstance(part, str) and part)
    return ''


@dataclass(frozen=True, slots=True)
class _Receipt:
    issuer: object
    nonce: str
    session_id: str
    origin_kind: str
    profile_name: str
    home: str
    platform: str
    actor: str
    chat: str
    message: str
    query_digest: str = ''
    lease: object = None
    owner_thread: int = 0


@dataclass(slots=True)
class _Lease:
    active: bool = True
    parent: object = None


def _live(lease):
    return (type(lease) is _Lease and lease.active is True
            and (lease.parent is None or type(lease.parent) is _Lease and lease.parent.active is True))


@contextmanager
def event_origin(event, *, session_id):
    """Only the gateway's admitted event boundary calls this native producer."""
    receipt = None
    try:
        from gateway.platforms.event import MessageEvent
        from gateway.response_filters import display_kind_for_event, is_machinery_display_kind
        from gateway.session_display import admission_submission
        source = event.source
        # The gateway may enter the receiving profile only inside _run_agent.
        # Freeze the routed source here, then verify the active native home on
        # the actual agent worker rather than sampling the launch-profile env.
        profile = str(getattr(source, 'profile', None) or 'default')
        platform = str(getattr(source.platform, 'value', source.platform) or '')
        message = str(event.message_id or admission_submission(event).get('submission_id') or '')
        chat, actor = str(source.chat_id or ''), str(source.user_id or '')
        admitted = isinstance(event, MessageEvent) and event._gateway_accepted is True
        kind = UNKNOWN
        if admitted and session_id and message and chat:
            if (event.internal is True or getattr(event, '_heartbeat_session_id', None)
                    or getattr(event, 'inherit_channel_pin', False)
                    or is_machinery_display_kind(display_kind_for_event(event))):
                kind = INTERNAL
            elif (platform in _PLATFORMS and actor and getattr(source, 'is_bot', None) is False):
                kind = EXTERNAL
            receipt = _Receipt(_ISSUER, uuid4().hex, str(session_id), kind, profile,
                '', platform, actor, chat,
                json.dumps([message, str(getattr(event, 'edit_revision', None) or '')], separators=(',', ':')),
                lease=_Lease())
    except Exception:
        receipt = None
    token = _seed.set(receipt)
    try:
        yield
    finally:
        if receipt is not None:
            receipt.lease.active = False
        _seed.reset(token)


@contextmanager
def input_origin(message):
    """Bind the actual agent input after gateway framing, on its worker thread."""
    seed = _seed.get()
    receipt = None
    text = _text(message)
    try:
        from hermes_constants import get_hermes_home
        from hermes_cli.profiles import get_active_profile_name
        if (type(seed) is _Receipt and seed.issuer is _ISSUER and _live(seed.lease)
                and text and len(text) <= 1024 * 1024
                and seed.profile_name == str(get_active_profile_name() or 'default')):
            receipt = _Receipt(seed.issuer, seed.nonce, seed.session_id, seed.origin_kind,
                seed.profile_name, str(Path(get_hermes_home()).expanduser().resolve()),
                seed.platform, seed.actor, seed.chat, seed.message, _hash(text),
                _Lease(parent=seed.lease), threading.get_ident())
    except Exception:
        receipt = None
    token = _bound.set(receipt)
    try:
        yield
    finally:
        if receipt is not None:
            receipt.lease.active = False
        _bound.reset(token)


def snapshot(*, session_id, message):
    """Read the current capability, never accept a caller's purported receipt."""
    value = _bound.get()
    if (type(value) is not _Receipt or value.issuer is not _ISSUER
            or not _live(value.lease) or value.owner_thread != threading.get_ident()
            or value.session_id != session_id or value.query_digest != _hash(_text(message))):
        return None
    return dict(origin_kind=value.origin_kind, profile_name=value.profile_name, platform=value.platform,
        home_digest=_hash(value.home), actor_digest=_hash(value.actor), chat_digest=_hash(value.chat),
        message_digest=_hash(value.message), host_query_digest=value.query_digest,
        native_receipt_digest=_hash(value.nonce))
