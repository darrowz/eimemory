"""Prepare reviewable real standing authority; install only as native root.

Preparation resolves the existing Profile and release using the runtime user.
Installation never constructs Runtime/SQLite, invents an incident, or issues a
child policy. Its required reviewed digest is a compare-and-review boundary.
"""
from __future__ import annotations
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
import argparse
import json
import os
import stat
import subprocess
import tempfile
import uuid

from eimemory.models.records import ScopeRef
from eimemory.governance.evolution import code_automation_policy_owner as owner
from eimemory.governance.evolution.code_automation_policy_issue import _policy_lock, _git, _EFFECTS_BY_MODE
from eimemory.governance.evolution.code_automation_policy_incidents import _repository_identity
from eimemory.governance.evolution.code_evolution_repository import remote_url_digest
from eimemory.governance.evolution.code_evolution_test_plans import protected_test_plan_digest
from eimemory.governance.release.closure_verdict import report_digest

from eimemory.contracts.user_control_plane import user_mode, account as user_account

SUPPORTED_CLASSES = ('deployment.runtime_commit_drift', 'release.closure_internal_failure', 'release.closure_report_failure')


def _runtime_git(root: Path, account: Any, *arguments: str) -> str:
    """Root never executes repository-controlled Git configuration itself."""
    allowed = arguments in {('status', '--porcelain', '--untracked-files=no'),
                           ('rev-parse', 'HEAD'), ('symbolic-ref', '--short', 'HEAD')}
    if (not allowed and not (len(arguments) == 3 and arguments[:2] == ('remote', 'get-url')
            and arguments[2] and not arguments[2].startswith('-')
            and not any(ord(char) < 32 for char in arguments[2]))):
        raise ValueError('standing_grant_git_read_operation_forbidden')
    if os.name != 'posix' or os.geteuid() != (account.pw_uid if user_mode() else 0) or account.pw_uid == 0:
        raise ValueError('standing_grant_git_runtime_account_required')
    argv = [* ([] if user_mode() else ['/usr/sbin/runuser', '-u', account.pw_name, '--']), '/usr/bin/env', '-i',
            'PATH=/usr/bin:/bin', 'HOME='+account.pw_dir, 'GIT_CONFIG_GLOBAL=/dev/null',
            'GIT_CONFIG_NOSYSTEM=1', 'GIT_TERMINAL_PROMPT=0', 'GIT_OPTIONAL_LOCKS=0',
            '/usr/bin/git', '--no-optional-locks', '-c', 'core.fsmonitor=false', '-C', str(root), *arguments]
    try:
        result = subprocess.run(argv, cwd='/', env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin'},
                                capture_output=True, text=True, timeout=10, check=True)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ValueError('standing_grant_runtime_git_read_failed') from exc
    return result.stdout.strip()


def prepare_standing_grant(runtime: Any, *, scope: ScopeRef | Mapping[str, Any], profile_key: str,
                           valid_hours: int | None = None, incident_classes: tuple[str, ...] = SUPPORTED_CLASSES) -> dict:
    """Derive actual coordinates without requiring or fabricating an incident."""
    from eimemory.config.trusted import trusted_repository_root, trusted_remote
    from eimemory.capabilities.profiles import CapabilityProfiles
    from eimemory.governance.release.evidence_contract import current_release_identity
    exact = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(dict(scope))
    if not isinstance(scope, ScopeRef) and asdict(exact) != dict(scope):
        raise ValueError('standing_grant_exact_scope_required')
    if (valid_hours is not None and (type(valid_hours) is not int or valid_hours < 1)
            or not incident_classes or len(set(incident_classes)) != len(incident_classes)
            or any(kind not in SUPPORTED_CLASSES for kind in incident_classes)):
        raise ValueError('standing_grant_preparation_bounds_invalid')
    root = Path(trusted_repository_root()).resolve()
    identity = _repository_identity(root)
    release = current_release_identity(runtime, exact)
    if identity.get('ok') is not True or release is None or not release.complete or release.commit != identity['base_commit']:
        raise ValueError('standing_grant_current_repository_release_unverified')
    from eimemory.governance.evolution.code_evolution_provider_authority import select_provider_authority
    authority = select_provider_authority(runtime, owner_scope=exact, profile_key=profile_key)
    resolved = CapabilityProfiles(runtime.store).resolve(profile_key, runtime_scope=authority['provider_scope'], capability_scope='global')
    if not any(item.get('capability_id') == 'code.implementation' for item in resolved.get('requirements', ())):
        raise ValueError('standing_grant_code_implementation_profile_required')
    profile = resolved['profile']
    now = datetime.now(timezone.utc)
    remote = trusted_remote()
    grant = {'schema': 'code_automation_standing_grant.v1', 'grant_id': 'native-standing-'+uuid.uuid4().hex,
        'enabled': True, 'not_before': now.isoformat(),
        'expires_at': (now+timedelta(hours=valid_hours)).isoformat() if valid_hours is not None else None,
        'scope': asdict(exact), 'repository': {'root': str(root), 'remote': remote,
            'branch': _git(root, 'symbolic-ref', '--short', 'HEAD'),
            'remote_url_digest': remote_url_digest(_git(root, 'remote', 'get-url', remote))},
        'profile_key': profile['profile_key'], 'profile_digest': profile['profile_digest'], 'capability_authority': authority,
        'routes': {kind: {'source': owner._ROUTES[kind][0], 'detector_id': owner._ROUTES[kind][0]+'.v1',
            'test_plan_id': owner._ROUTES[kind][1], 'test_plan_digest': protected_test_plan_digest(owner._ROUTES[kind][1])}
            for kind in incident_classes}, 'effects': dict(_EFFECTS_BY_MODE['full']), 'max_transactions': 1}
    owner._validate_grant(grant, exact, root, now)
    return {'grant': grant, 'grant_digest': report_digest(grant), 'prepared_at': now.isoformat(),
            'repository_commit': release.commit, 'receipt_id': release.receipt_id,
            'release_session_id': release.session_id, 'profile_id': profile['profile_id'],
            'child_policy_issued': False, 'incident_created': False, 'l5_origin_verified': False}


def install_prepared_standing_grant(prepared: Mapping[str, Any], *, expected_grant_digest: str,
                                     runtime_user: str, replace_digest: str = '') -> dict:
    if os.name != 'posix' or (not user_mode() and os.geteuid()!=0):
        raise ValueError('standing_grant_install_control_identity_required' if user_mode() else 'standing_grant_install_requires_native_root')
    import pwd
    from eimemory.storage.atomic_file import _fsync_directory
    from eimemory.governance.evolution.code_automation_policy import _v2_no_symlink
    grant = dict(prepared['grant'])
    digest = report_digest(grant)
    if (len(expected_grant_digest) != 64 or digest != expected_grant_digest
            or prepared.get('grant_digest') != digest):
        raise ValueError('standing_grant_review_digest_mismatch')
    account = pwd.getpwnam(runtime_user)
    if user_mode() and account.pw_uid!=user_account().pw_uid:raise ValueError('standing_grant_install_user_mismatch')
    control_uid=account.pw_uid if user_mode() else 0
    # Logical memory owners (for example a channel ou_* ID) are independent
    # from the explicitly selected POSIX service account/group.
    if account.pw_uid == 0:
        raise ValueError('standing_grant_runtime_account_mismatch')
    exact = ScopeRef.from_dict(grant['scope'])
    root = Path(grant['repository']['root'])
    repository_info = root.lstat()
    git_info = (root / '.git').lstat()
    if (_v2_no_symlink(root / '.standing-grant-path-check', missing_ok=True)
            or not stat.S_ISDIR(repository_info.st_mode) or repository_info.st_uid != account.pw_uid
            or not (stat.S_ISDIR(git_info.st_mode) or stat.S_ISREG(git_info.st_mode) and git_info.st_nlink == 1)
            or git_info.st_uid != account.pw_uid):
        raise ValueError('standing_grant_repository_runtime_account_mismatch')
    git_read = lambda repository, *args: _runtime_git(repository, account, *args)
    owner._validate_grant(grant, exact, root, datetime.now(timezone.utc), git_runner=git_read)
    if git_read(root, 'status', '--porcelain', '--untracked-files=no'):
        raise ValueError('standing_grant_repository_worktree_not_clean')
    if git_read(root, 'rev-parse', 'HEAD').lower() != prepared.get('repository_commit'):
        raise ValueError('standing_grant_repository_changed_after_review')
    destination = owner.standing_grant_path(exact)
    if _v2_no_symlink(destination, missing_ok=True):
        raise ValueError('standing_grant_install_path_invalid')
    if not destination.parent.exists():
        destination.parent.mkdir(mode=0o750)
        os.chown(destination.parent, control_uid, account.pw_gid)
        os.chmod(destination.parent, 0o750)
    parent = destination.parent.stat()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != control_uid or parent.st_gid != account.pw_gid
            or stat.S_IMODE(parent.st_mode) != 0o750):
        raise ValueError('standing_grant_install_parent_invalid')
    with _policy_lock(destination.with_name('.standing-grant.install.lock')):
        if destination.exists():
            existing = owner._secure_read_standing_grant(exact)
            if report_digest(existing) == digest:
                _fsync_directory(destination.parent)
                return {'ok': True, 'status': 'existing', 'grant_digest': digest, 'child_policy_issued': False}
            if not replace_digest or report_digest(existing) != replace_digest:
                raise ValueError('standing_grant_existing_authority_preserved')
        elif replace_digest:
            raise ValueError('standing_grant_existing_authority_changed')
        fd, name = tempfile.mkstemp(prefix='.standing-grant-', dir=destination.parent)
        temporary = Path(name)
        try:
            os.fchown(fd, control_uid, account.pw_gid); os.fchmod(fd, 0o640)
            with os.fdopen(fd, 'wb') as stream:
                stream.write((json.dumps(grant, indent=2)+'\n').encode())
                stream.flush(); os.fsync(stream.fileno())
            if _v2_no_symlink(destination, missing_ok=True):
                raise ValueError('standing_grant_install_path_changed')
            os.replace(temporary, destination); _fsync_directory(destination.parent)
        finally:
            temporary.unlink(missing_ok=True)
        if owner._secure_read_standing_grant(exact) != grant:
            raise ValueError('standing_grant_install_readback_mismatch')
    return {'ok': True, 'status': 'installed', 'grant_digest': digest, 'path': str(destination),
            'child_policy_issued': False, 'incident_created': False, 'l5_origin_verified': False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    prepare = commands.add_parser('prepare')
    prepare.add_argument('--runtime-root', type=Path, required=True)
    for field in ('tenant', 'agent', 'workspace', 'user'): prepare.add_argument('--scope-'+field, required=True)
    prepare.add_argument('--profile-key', required=True)
    prepare.add_argument('--valid-hours', type=int, default=None,
                         help='Explicit finite lifetime; omission means expires_at:null until root revocation.')
    prepare.add_argument('--incident-class', action='append', choices=SUPPORTED_CLASSES)
    install = commands.add_parser('install')
    install.add_argument('--prepared', type=Path, required=True)
    install.add_argument('--expected-grant-digest', required=True)
    install.add_argument('--runtime-user', required=True)
    install.add_argument('--replace-digest', default='')
    args = parser.parse_args()
    if args.command == 'prepare':
        if os.name == 'posix' and os.geteuid() == 0:
            raise ValueError('prepare_as_runtime_user_never_root')
        if not args.runtime_root.is_dir(): raise ValueError('existing_runtime_root_required')
        from eimemory.api.runtime import Runtime
        runtime = Runtime.create(root=args.runtime_root)
        try:
            value = prepare_standing_grant(runtime, scope={field+'_id': getattr(args, 'scope_'+field)
                    for field in ('tenant', 'agent', 'workspace', 'user')}, profile_key=args.profile_key,
                    valid_hours=args.valid_hours, incident_classes=tuple(args.incident_class or SUPPORTED_CLASSES))
        finally: runtime.close()
    else:
        from eimemory.ops.closure_capture import read_source
        from eimemory.core.strict_json import loads
        prepared = loads(read_source(args.prepared), max_bytes=65536, max_depth=16)
        value = install_prepared_standing_grant(prepared, expected_grant_digest=args.expected_grant_digest,
                    runtime_user=args.runtime_user, replace_digest=args.replace_digest)
    print(json.dumps(value, indent=2))
    return 0


if __name__ == '__main__': raise SystemExit(main())
