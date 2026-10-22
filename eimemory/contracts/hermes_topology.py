"""User-owned declaration of one native Hermes gateway and logical profiles.

This stdlib-only contract does not configure, constrain or launch Hermes. The
operator declaration binds memory identities; it is not recall or L5 evidence.
The same bytes are installed beside the independent user controller.
"""
from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import PurePosixPath
import re

SCHEMA = 'eimemory.hermes_topology.v1'
MAIN_GATEWAY = 'hermes-gateway.service'
RUNTIME_UNITS = ('eimemory-rpc.service', MAIN_GATEWAY)
TOPOLOGY_NAME = 'hermes-topology.v1.json'
INTENT_NAME = 'hermes-topology-intent.v1.json'
_SCOPE_KEYS = {'tenant_id', 'agent_id', 'workspace_id', 'user_id'}
_PROFILE = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}')


def _text(value, reason='hermes_topology_identity_invalid'):
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > 256 or '*' in value or any(ord(char) < 32 for char in value)):
        raise ValueError(reason)
    return value


def _home(value):
    _text(value, 'hermes_topology_home_invalid')
    path = PurePosixPath(value)
    if (not path.is_absolute() or value.startswith('//') or str(path) != value
            or '..' in path.parts or path == PurePosixPath('/')):
        raise ValueError('hermes_topology_home_invalid')
    return value


def canonical_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode('utf-8')


def _strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('hermes_topology_json_duplicate')
            result[key] = value
        return result
    def constant(value):
        raise ValueError('hermes_topology_json_constant')
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def validate_operator_topology_intent(value, *, main, retired, uid, gid):
    """One shared typed operator intent; never grants autonomous repair credit."""
    fields = {'schema', 'intent_id', 'runtime_uid', 'runtime_gid', 'mode', 'main_gateway',
              'retired_gateways', 'source', 'topology_sha256', 'native_snapshot_sha256',
              'authorization', 'autonomous_repair_credit'}
    source_fields = {'registry_sha256', 'transition_sha256', 'job_key', 'job_sha256',
                     'invocation_id', 'base_commit', 'capture_sha256'}
    if (not isinstance(value, dict) or set(value) != fields
            or value.get('schema') != 'eimemory.hermes_topology_intent.v1'
            or value.get('mode') != 'single_native_gateway' or value.get('main_gateway') != main
            or value.get('retired_gateways') != list(retired)
            or type(value.get('runtime_uid')) is not int or value['runtime_uid'] != uid or uid <= 0
            or type(value.get('runtime_gid')) is not int or value['runtime_gid'] != gid or gid <= 0
            or value.get('authorization') != 'explicit_user_operator'
            or value.get('autonomous_repair_credit') is not False
            or not isinstance(value.get('source'), dict) or set(value['source']) != source_fields):
        raise ValueError('manager_topology_intent_invalid')
    def hexadecimal(item, size):
        return isinstance(item, str) and re.fullmatch('[0-9a-f]{' + str(size) + '}', item) is not None
    if (not hexadecimal(value.get('intent_id'), 32)
            or not hexadecimal(value.get('native_snapshot_sha256'), 64)
            or not hexadecimal(value.get('topology_sha256'), 64)
            or any(not hexadecimal(value['source'][key], 32 if key == 'invocation_id' else
                                   40 if key == 'base_commit' else 64) for key in source_fields)):
        raise ValueError('manager_topology_intent_binding_invalid')
    return value


def validate_topology(value):
    """Validate the declaration body; snapshots are validated separately."""
    keys = {'schema', 'mode', 'runtime_uid', 'runtime_gid', 'owner_unit',
            'runtime_units', 'profiles', 'retired_gateways'}
    if (not isinstance(value, dict) or set(value) != keys or value['schema'] != SCHEMA
            or value['mode'] != 'single_native_gateway'
            or value['owner_unit'] != MAIN_GATEWAY
            or value['runtime_units'] != list(RUNTIME_UNITS)):
        raise ValueError('hermes_topology_invalid')
    for key in ('runtime_uid', 'runtime_gid'):
        if type(value[key]) is not int or value[key] <= 0:
            raise ValueError('hermes_topology_user_identity_invalid')
    profiles = value['profiles']
    retired = value['retired_gateways']
    if (not isinstance(profiles, list) or not 1 <= len(profiles) <= 64
            or not isinstance(retired, list) or len(retired) > 64
            or any(not isinstance(unit, str) or re.fullmatch(
                r'hermes-gateway-[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}\.service', unit) is None
                for unit in retired) or len(set(retired)) != len(retired)):
        raise ValueError('hermes_topology_inventory_invalid')
    names, homes, captures = set(), set(), set()
    for profile in profiles:
        required = {'profile_name', 'hermes_home', 'agent_id', 'identity_status', 'capture_scopes'}
        if (not isinstance(profile, dict) or not required <= set(profile)
                or set(profile) - required - {'operator_scope'}
                or not isinstance(profile['profile_name'], str)
                or _PROFILE.fullmatch(profile['profile_name']) is None):
            raise ValueError('hermes_topology_profile_invalid')
        name = profile['profile_name']
        home = _home(profile['hermes_home'])
        agent = _text(profile['agent_id'])
        if name in names or home in homes:
            raise ValueError('hermes_topology_profile_duplicate')
        names.add(name)
        homes.add(home)
        operator = profile.get('operator_scope')
        if operator is not None:
            if not isinstance(operator, dict) or set(operator) != _SCOPE_KEYS:
                raise ValueError('hermes_topology_operator_scope_invalid')
            for field in operator.values():
                _text(field, 'hermes_topology_operator_scope_invalid')
            if operator['agent_id'] != agent:
                raise ValueError('hermes_topology_operator_scope_profile_mismatch')
        scopes = profile['capture_scopes']
        status = profile['identity_status']
        if (status not in {'bound', 'unbound'} or not isinstance(scopes, list)
                or len(scopes) > 64 or (status == 'bound') != bool(scopes)):
            raise ValueError('hermes_topology_binding_invalid')
        for capture in scopes:
            if (not isinstance(capture, dict) or set(capture) != {'scope', 'channel', 'source_ids'}
                    or not isinstance(capture['scope'], dict)
                    or set(capture['scope']) != _SCOPE_KEYS):
                raise ValueError('hermes_topology_scope_invalid')
            scope = capture['scope']
            for item in scope.values():
                _text(item, 'hermes_topology_scope_invalid')
            if scope['agent_id'] != agent or capture['channel'] != 'hermes':
                raise ValueError('hermes_topology_scope_profile_mismatch')
            sources = capture['source_ids']
            if (not isinstance(sources, list) or not 1 <= len(sources) <= 32
                    or any(not isinstance(source, str) for source in sources)
                    or len(set(sources)) != len(sources)):
                raise ValueError('hermes_topology_sources_invalid')
            for source in sources:
                _text(source, 'hermes_topology_sources_invalid')
                key = (*[scope[field] for field in sorted(_SCOPE_KEYS)], capture['channel'], source)
                if key in captures:
                    raise ValueError('hermes_topology_capture_duplicate')
                captures.add(key)
    if 'default' not in names:
        raise ValueError('hermes_topology_default_profile_missing')
    # Return an independent JSON value: a caller cannot mutate a validated
    # snapshot through an input alias after its digest has been checked.
    return json.loads(canonical_bytes(value))


def topology_digest(value):
    return sha256(canonical_bytes(validate_topology(value))).hexdigest()


def validate_snapshot(value):
    if not isinstance(value, dict) or 'topology_digest' not in value:
        raise ValueError('hermes_topology_snapshot_invalid')
    digest = value['topology_digest']
    body = {key: item for key, item in value.items() if key != 'topology_digest'}
    body = validate_topology(body)
    if not isinstance(digest, str) or digest != topology_digest(body):
        raise ValueError('hermes_topology_snapshot_digest_mismatch')
    return dict(body, topology_digest=digest)


def _authority():
    # Do not derive enrollment from a request's HOME/XDG or profile secret scope.
    from eimemory.contracts.user_control_plane import account, coordinates, read_file, user_mode
    if not user_mode():
        return None
    coords = coordinates()
    coords['runtime_user'] = account().pw_name
    return coords, read_file


def load_topology():
    """Read the committed user declaration; absent enrollment alone returns None.

    Independent recovery authenticates the intent's complete source inventory
    and private registration receipt. Product consumers verify the published
    operator intent and declaration digest; neither artifact grants L5 credit.
    """
    authority = _authority()
    if authority is None:
        return None
    coords, reader = authority
    declaration = coords['authority'] / TOPOLOGY_NAME
    intent_path = coords['authority'] / INTENT_NAME
    exists = [os.path.lexists(path) for path in (declaration, intent_path)]
    if not any(exists):
        return None
    if not all(exists):
        raise ValueError('hermes_topology_registration_incomplete')
    raw = reader(declaration, maximum=1024 * 1024)
    intent_raw = reader(intent_path, maximum=256 * 1024)
    try:
        body = validate_topology(_strict_json(raw))
        intent = _strict_json(intent_raw)
        validate_operator_topology_intent(intent, main=MAIN_GATEWAY, retired=body['retired_gateways'],
                                          uid=coords['uid'], gid=coords['gid'])
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ValueError('hermes_topology_registration_invalid') from exc
    digest = topology_digest(body)
    if (not isinstance(intent, dict) or intent.get('schema') != 'eimemory.hermes_topology_intent.v1'
            or intent.get('mode') != 'single_native_gateway'
            or intent.get('main_gateway') != MAIN_GATEWAY
            or intent.get('authorization') != 'explicit_user_operator'
            or intent.get('autonomous_repair_credit') is not False
            or intent.get('topology_sha256') != digest
            or intent.get('retired_gateways') != body['retired_gateways']
            or any(profile.get('operator_scope') is not None
                   and profile['operator_scope']['user_id'] != coords.get('runtime_user')
                   for profile in body['profiles'])
            or any(body[key] != coords[key.removeprefix('runtime_')]
                   or intent.get(key) != body[key] for key in ('runtime_uid', 'runtime_gid'))):
        raise ValueError('hermes_topology_operator_binding_invalid')
    # Re-read both named objects after validation to reject a replaced pair.
    if reader(declaration, maximum=1024 * 1024) != raw or reader(intent_path, maximum=256 * 1024) != intent_raw:
        raise ValueError('hermes_topology_registration_changed')
    return dict(body, topology_digest=digest)


def load_verified_topology():
    value = load_topology()
    if value is None:
        raise ValueError('hermes_topology_not_registered')
    return value


def resolve_profile_binding(profile, home, *, user_id, workspace_id=None, topology=None):
    """Resolve an exact native request identity without profile/global fallbacks."""
    value = load_verified_topology() if topology is None else validate_snapshot(topology)
    selected = [item for item in value['profiles']
                if item['profile_name'] == profile and item['hermes_home'] == str(home)]
    if len(selected) != 1:
        raise ValueError('hermes_topology_profile_unregistered')
    item = selected[0]
    operator = item.get('operator_scope')
    if (operator is not None and user_id == operator['user_id']
            and (workspace_id is None or workspace_id == operator['workspace_id'])):
        return resolve_operator_binding(profile, home, topology=value)
    if item['identity_status'] != 'bound':
        raise ValueError('hermes_topology_identity_unbound')
    matches = [capture for capture in item['capture_scopes']
               if capture['scope']['user_id'] == user_id
               and (workspace_id is None or capture['scope']['workspace_id'] == workspace_id)]
    if len(matches) != 1:
        raise ValueError('hermes_topology_request_scope_ambiguous' if matches
                         else 'hermes_topology_request_scope_unbound')
    capture = matches[0]
    return dict(capture, profile_name=profile, hermes_home=str(home),
                topology_digest=value['topology_digest'], binding_kind='business',
                natural_sample_eligible=True)


def resolve_operator_binding(profile, home, *, topology=None):
    """Resolve an explicitly declared local owner, never infer a chat user.

    A native adapter must first authenticate a local CLI/desktop/cron source
    and the OS account. External requests with missing users cannot use this
    function as a fallback. The returned identity is not natural evidence.
    """
    value = load_verified_topology() if topology is None else validate_snapshot(topology)
    matches = [item for item in value['profiles']
               if item['profile_name'] == profile and item['hermes_home'] == str(home)]
    if len(matches) != 1 or not matches[0].get('operator_scope'):
        raise ValueError('hermes_topology_operator_scope_unbound')
    return dict(scope=matches[0]['operator_scope'], channel='hermes', source_ids=['hermes'],
                profile_name=profile, hermes_home=str(home), topology_digest=value['topology_digest'],
                binding_kind='operator', natural_sample_eligible=False)
