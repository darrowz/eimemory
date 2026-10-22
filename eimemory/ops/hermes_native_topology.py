"""Read-only native profile inventory, tied to the gateway's process lifetime.

The collector reads metadata only. It never changes gateway configuration,
permissions, sandboxing or enablement, and does not read conversation bodies.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat

from eimemory.contracts.hermes_topology import MAIN_GATEWAY, canonical_bytes, validate_snapshot

SCHEMA = 'eimemory.hermes_native_profile_topology.v1'
_SHA = re.compile(r'[0-9a-f]{64}')
_PLUGIN_NAMES = ('eimemory', 'eimemory_hook')


def _release_source_pins(expected_commit):
    """Pin the collector's immutable release, never a gateway-supplied path."""
    if not isinstance(expected_commit, str) or re.fullmatch(r'[0-9a-f]{40}', expected_commit) is None:
        raise ValueError('hermes_native_release_commit_invalid')
    release = Path(__file__).resolve().parents[2]
    expected_root = Path('/opt/eimemory/releases') / expected_commit
    if release != expected_root:
        raise ValueError('hermes_native_collector_release_mismatch')
    plugins = []
    for name in _PLUGIN_NAMES:
        target = release / 'integrations' / 'hermes' / name
        plugins.append(dict(name=name, target=str(target),
            manifest_sha256=sha256(_read(target / 'plugin.yaml')).hexdigest(),
            entry_sha256=sha256(_read(target / '__init__.py')).hexdigest()))
    return dict(source_commit=expected_commit, release_root=str(release), plugins=plugins)


def _read(path, maximum=1024 * 1024):
    """Read native metadata without following a swapped final component."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_uid != os.geteuid() or before.st_size > maximum):
            raise ValueError('hermes_native_metadata_untrusted')
        raw = os.read(fd, maximum + 1)
        identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns,
                                 item.st_ctime_ns, item.st_uid, item.st_mode, item.st_nlink)
        if (len(raw) != before.st_size or identity(before) != identity(os.fstat(fd))
                or identity(before) != identity(Path(path).lstat())):
            raise ValueError('hermes_native_metadata_changed')
        return raw
    finally:
        os.close(fd)


def _process_owner():
    from eimemory.core.runtime_manager import request
    observation = request('runtime_process_environment', unit=MAIN_GATEWAY)['observation']
    return {key: observation[key] for key in ('unit', 'pid', 'start_ticks', 'invocation_id')}, observation


def _configuration(profile, source_pins):
    import yaml
    home = Path(profile['hermes_home'])
    raw = _read(home / 'config.yaml')
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ValueError('hermes_native_config_invalid') from exc
    if not isinstance(parsed, dict):
        raise ValueError('hermes_native_config_invalid')
    provider = (parsed.get('memory') or {}).get('provider', '')
    plugins = parsed.get('plugins') or {}
    enabled = plugins.get('enabled') or []
    if not isinstance(enabled, list):
        raise ValueError('hermes_native_plugins_invalid')
    links = []
    for name in _PLUGIN_NAMES:
        link = home / 'plugins' / name
        target = link.resolve(strict=True)
        # Native plugin links are allowed; the actual file and bytes are pinned.
        manifest = target / 'plugin.yaml'
        entry = target / '__init__.py'
        links.append(dict(name=name, target=str(target),
                          manifest_sha256=sha256(_read(manifest)).hexdigest(),
                          entry_sha256=sha256(_read(entry)).hexdigest()))
    source_matches = links == source_pins['plugins']
    ok = provider == 'eimemory' and 'eimemory-hook' in enabled and source_matches
    multiplex = (parsed.get('gateway') or {}).get('multiplex_profiles')
    if profile['profile_name'] == 'default' and multiplex is False:
        ok = False
    return dict(profile_name=profile['profile_name'], hermes_home=str(home), ok=ok,
                reason='' if ok else 'hermes_native_plugin_source_mismatch' if not source_matches
                    else 'hermes_native_memory_integration_not_enabled',
                config_sha256=sha256(raw).hexdigest(), provider=provider,
                hook_enabled='eimemory-hook' in enabled, plugin_links=links)


def _stable_body(proof):
    return {key: value for key, value in proof.items()
            if key not in {'observed_at', 'gateway_updated_at', 'stable_digest'}}


def collect_native_profile_topology(topology, owner_state=None, *, expected_commit):
    """Use controller observations outside private PID namespaces; fail closed."""
    topology = validate_snapshot(topology)
    reasons = []
    proof = dict(schema=SCHEMA, topology_digest=topology['topology_digest'], owner={},
                 served_profiles=[], profile_configs=[], complete=False, blocking_reasons=[],
                 release_source_pins={},
                 observed_at=datetime.now(timezone.utc).isoformat(), gateway_updated_at='')
    try:
        source_pins = _release_source_pins(expected_commit)
        proof['release_source_pins'] = source_pins
        owner, observation = _process_owner()
        proof['owner'] = owner
        if owner_state is not None and str(owner['pid']) != str(owner_state.get('MainPID')):
            raise ValueError('hermes_native_owner_pid_changed')
        default = next(profile for profile in topology['profiles'] if profile['profile_name'] == 'default')
        home = Path(default['hermes_home'])
        environment = observation.get('environment') or {}
        if environment.get('HERMES_HOME') and environment['HERMES_HOME'] != default['hermes_home']:
            raise ValueError('hermes_native_owner_home_mismatch')
        state = json.loads(_read(home / 'gateway_state.json'))
        namespace_pids = [owner['pid'], *(observation.get('namespace_pids') or [])]
        if (state.get('pid') not in namespace_pids or state.get('start_time') != owner['start_ticks']
                or state.get('hermes_home') != default['hermes_home'] or state.get('gateway_state') != 'running'):
            raise ValueError('hermes_native_gateway_state_owner_mismatch')
        updated = datetime.fromisoformat(state['updated_at'])
        age = (datetime.now(timezone.utc) - updated).total_seconds()
        if not -5 <= age <= 120:
            raise ValueError('hermes_native_gateway_heartbeat_stale')
        proof['gateway_updated_at'] = state['updated_at']
        served = state.get('served_profiles')
        if (not isinstance(served, list) or any(not isinstance(name, str) for name in served)
                or len(served) != len(set(served))):
            raise ValueError('hermes_native_profile_roster_invalid')
        proof['served_profiles'] = sorted(served)
        if set(served) != {profile['profile_name'] for profile in topology['profiles']}:
            reasons.append('hermes_native_profile_roster_mismatch')
        for profile in sorted(topology['profiles'], key=lambda item: item['profile_name']):
            try:
                config = _configuration(profile, source_pins)
            except (OSError, ValueError, TypeError, KeyError):
                config = dict(profile_name=profile['profile_name'], hermes_home=profile['hermes_home'],
                              ok=False, reason='hermes_native_profile_config_unverified')
            proof['profile_configs'].append(config)
            if not config['ok']:
                reasons.append(config['reason'])
        after, _ = _process_owner()
        if after != owner:
            raise ValueError('hermes_native_owner_changed')
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        code = str(exc) if isinstance(exc, ValueError) else ''
        reasons.append(code if re.fullmatch(r'hermes_native_[a-z_]{1,80}', code)
                       else 'hermes_native_profile_observation_unverified')
    proof['blocking_reasons'] = sorted(set(reasons))
    proof['complete'] = not reasons
    proof['stable_digest'] = sha256(canonical_bytes(_stable_body(proof))).hexdigest()
    return proof


def validate_native_profile_topology(proof, topology, *, expected_commit):
    """Pure structural replay; the enclosing sealed sample supplies provenance."""
    topology = validate_snapshot(topology)
    keys = {'schema', 'topology_digest', 'owner', 'served_profiles', 'profile_configs',
            'complete', 'blocking_reasons', 'observed_at', 'gateway_updated_at', 'stable_digest',
            'release_source_pins'}
    if (not isinstance(proof, dict) or set(proof) != keys or proof['schema'] != SCHEMA
            or proof['topology_digest'] != topology['topology_digest']
            or proof['complete'] is not True or proof['blocking_reasons'] != []
            or proof['stable_digest'] != sha256(canonical_bytes(_stable_body(proof))).hexdigest()):
        raise ValueError('hermes_native_profile_proof_invalid')
    pins = proof['release_source_pins']
    if (not isinstance(expected_commit, str) or re.fullmatch(r'[0-9a-f]{40}', expected_commit) is None
            or not isinstance(pins, dict) or set(pins) != {'source_commit', 'release_root', 'plugins'}
            or pins['source_commit'] != expected_commit
            or pins['release_root'] != '/opt/eimemory/releases/' + expected_commit
            or not isinstance(pins['plugins'], list)
            or any(not isinstance(item, dict) for item in pins['plugins'])
            or [item.get('name') for item in pins['plugins']] != list(_PLUGIN_NAMES)):
        raise ValueError('hermes_native_release_source_pins_invalid')
    for pin in pins['plugins']:
        if (set(pin) != {'name', 'target', 'manifest_sha256', 'entry_sha256'}
                or pin['target'] != pins['release_root'] + '/integrations/hermes/' + pin['name']
                or any(not isinstance(pin.get(key), str) or _SHA.fullmatch(pin[key]) is None
                       for key in ('manifest_sha256', 'entry_sha256'))):
            raise ValueError('hermes_native_release_source_pins_invalid')
    owner = proof['owner']
    if (not isinstance(owner, dict) or set(owner) != {'unit', 'pid', 'start_ticks', 'invocation_id'}
            or owner['unit'] != MAIN_GATEWAY or type(owner['pid']) is not int or owner['pid'] <= 0
            or type(owner['start_ticks']) is not int or owner['start_ticks'] <= 0
            or not isinstance(owner['invocation_id'], str)
            or re.fullmatch(r'[0-9a-f]{32}', owner['invocation_id']) is None):
        raise ValueError('hermes_native_profile_owner_invalid')
    names = sorted(profile['profile_name'] for profile in topology['profiles'])
    configs = proof['profile_configs']
    if (proof['served_profiles'] != names or not isinstance(configs, list)
            or any(not isinstance(config, dict) for config in configs)
            or [config.get('profile_name') for config in configs] != names):
        raise ValueError('hermes_native_profile_coverage_invalid')
    for config, profile in zip(configs, sorted(topology['profiles'], key=lambda item: item['profile_name'])):
        links = config.get('plugin_links')
        if (config.get('ok') is not True or config.get('reason') != ''
                or config.get('hermes_home') != profile['hermes_home']
                or config.get('provider') != 'eimemory' or config.get('hook_enabled') is not True
                or not isinstance(config.get('config_sha256'), str)
                or _SHA.fullmatch(config['config_sha256']) is None
                or not isinstance(links, list) or any(not isinstance(item, dict) for item in links)
                or [item.get('name') for item in links] != ['eimemory', 'eimemory_hook']):
            raise ValueError('hermes_native_profile_config_invalid')
        for link in links:
            if (not isinstance(link.get('target'), str) or not PurePosixPath(link['target']).is_absolute()
                    or any(not isinstance(link.get(key), str) or _SHA.fullmatch(link[key]) is None
                           for key in ('manifest_sha256', 'entry_sha256'))):
                raise ValueError('hermes_native_plugin_metadata_invalid')
        if links != pins['plugins']:
            raise ValueError('hermes_native_plugin_source_mismatch')
    for field in ('observed_at', 'gateway_updated_at'):
        if not isinstance(proof[field], str):
            raise ValueError('hermes_native_profile_timestamp_invalid')
        timestamp = datetime.fromisoformat(proof[field])
        if timestamp.utcoffset() is None:
            raise ValueError('hermes_native_profile_timestamp_invalid')
    age = (datetime.fromisoformat(proof['observed_at']) - datetime.fromisoformat(proof['gateway_updated_at'])).total_seconds()
    if not -5 <= age <= 120:
        raise ValueError('hermes_native_profile_heartbeat_invalid')
    return proof
