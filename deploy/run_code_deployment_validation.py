#!/usr/bin/env python3
"""Fixed low-privilege controller for read-only-source target validation."""
from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import re
import socket
import stat
import struct
import sys
import importlib.util

_WORKER_RELEASES_ROOT = Path('/opt/eimemory/releases')
_WORKER_CURRENT_LINK = Path('/opt/eimemory/current')
_WORKER_CONFIG_DIR = Path('/etc/eimemory')
_WORKER_STATE_DIR = Path('/var/lib/eimemory/state')

USER_MODE = os.environ.get('EIMEMORY_CONTROL_PLANE', '') == 'user'
CONTROL_UID = os.geteuid() if USER_MODE else 0
if USER_MODE:
    import pwd
    home=Path(pwd.getpwuid(os.geteuid()).pw_dir)
    helper=home/'.local/lib/eimemory/control-plane/user_control_plane.py'
    info=helper.lstat()
    if helper.is_symlink() or info.st_uid!=CONTROL_UID or info.st_mode&0o022:
        raise ValueError('manager_user_helper_untrusted')
    specification=importlib.util.spec_from_file_location('eimemory_user_control_plane',helper)
    user_control=importlib.util.module_from_spec(specification)
    exec(compile(helper.read_bytes(),str(helper),'exec'),user_control.__dict__)
    USER_COORDINATES=user_control.coordinates()



def _strict_json(raw):
    def pairs(items):
        result = {}
        for name, value in items:
            if name in result:
                raise ValueError("validation_duplicate_json_key")
            result[name] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("validation_nonfinite_json")))


def _root_json(path):
    if USER_MODE:return _strict_json(user_control.read_file(path))
    for parent in (path.parent, *path.parent.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("validation_root_job_parent_untrusted")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(descriptor)
        identity = lambda item: (item.st_dev, item.st_ino, item.st_mode, item.st_uid,
            item.st_gid, item.st_nlink, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != 0 or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o640 or before.st_size > 65536):
            raise ValueError("validation_root_job_file_untrusted")
        raw = os.read(descriptor, 65537)
        if (len(raw) != before.st_size or identity(before) != identity(os.fstat(descriptor))
            or identity(before) != identity(path.lstat())):
            raise ValueError("validation_root_job_changed")
        record = _strict_json(raw)
    finally:
        os.close(descriptor)
    return record


def _root_job(key):
    record = _root_json(((USER_COORDINATES["state"] if USER_MODE else Path("/var/lib/eimemory-manager"))/"jobs") / (key + ".json"))
    request = record["request"]
    canonical = json.dumps(request, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()
    if record.get("job_key") != key or sha256(canonical).hexdigest() != key:
        raise ValueError("validation_root_job_identity_invalid")
    return record


def _receive(stream, size):
    parts = []
    while size:
        part = stream.recv(size)
        if not part:
            raise ValueError("validation_root_response_truncated")
        parts.append(part)
        size -= len(part)
    return b"".join(parts)


def _root_validation_status(key, operation):
    """Fixed stdlib bootstrap: authenticate the root peer before product import."""
    registry = _root_json((USER_COORDINATES["registry"] if USER_MODE else Path("/etc/eimemory-code-automation-authority/runtime-manager.v1.json")))
    if (registry.get("schema") != "eimemory.runtime_manager_registry.v1"
        or re.fullmatch("[0-9a-f]{64}", str(registry.get("generation"))) is None
        or registry.get("runtime_uid") != (CONTROL_UID if USER_MODE else 1001) or registry.get("runtime_gid") != (USER_COORDINATES["gid"] if USER_MODE else 1001)
        or sha256(json.dumps({name:value for name,value in registry.items() if name!="generation"},
            sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
            != registry["generation"]):
        raise ValueError("validation_root_registry_identity_invalid")
    path = (USER_COORDINATES["socket"] if USER_MODE else Path("/run/eimemory-manager/bridge.sock"))
    if USER_MODE:user_control.trusted_directory(path.parent,private=True)
    for parent in (() if USER_MODE else (path.parent, *path.parent.parents)):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("validation_root_socket_parent_untrusted")
    info = path.lstat()
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != CONTROL_UID or info.st_gid != (USER_COORDINATES["gid"] if USER_MODE else 1001)
        or stat.S_IMODE(info.st_mode) != 0o660 or info.st_nlink != 1):
        raise ValueError("validation_root_socket_untrusted")
    request = {"schema": "eimemory.runtime_manager_request.v1",
        "operation": "deployment_validation_status", "registry_generation": registry["generation"],
        "job_key": key, "validation_operation": operation}
    raw = json.dumps(request, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
        stream.settimeout(30)
        stream.connect(str(path))
        _, uid, _ = struct.unpack("3i", stream.getsockopt(socket.SOL_SOCKET,
            socket.SO_PEERCRED, struct.calcsize("3i")))
        if uid != CONTROL_UID:
            raise ValueError("validation_root_peer_required")
        stream.sendall(struct.pack(">I", len(raw)) + raw)
        size = struct.unpack(">I", _receive(stream, 4))[0]
        if size > 8 * 1024 * 1024:
            raise ValueError("validation_root_response_too_large")
        response = _strict_json(_receive(stream, size))
    if (not isinstance(response, dict) or response.get("schema") != "eimemory.runtime_manager_response.v1"
        or response.get("operation") != "deployment_validation_status" or response.get("ok") is not True
        or response.get("registry_generation") != registry["generation"]
        or not isinstance(response.get("validation"), dict)):
        raise ValueError("validation_root_status_unverified")
    return response["validation"], uid


def _assert_execution_identity(request, key, operation, native, invocation, status, cgroups):
    unit = "eimemory-code-validation-" + operation.replace("_", "-") + "@" + key + ".service"
    state = native.get("state") or {}
    group = state.get("ControlGroup", "")
    fields = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)
    if (native.get("job_key") != key or native.get("operation") != operation
        or native.get("manager") != ("user" if USER_MODE else "system") or native.get("unit") != unit
        or native.get("target_commit") != request["target_commit"]
        or native.get("controller_commit") != request["controller_commit"]
        or native.get("manual_release", False) is not request["manual_release"]
        or native.get("launch_controller_commit", request["launch_controller_commit"]) != request["launch_controller_commit"]
        or native.get("artifact_basename") != "validation-" + operation + "-result.json"
        or re.fullmatch("[0-9a-f]{32}", invocation) is None
        or native.get("original_invocation") != invocation or state.get("InvocationID") != invocation
        or re.fullmatch("[0-9a-f]{32}", str(native.get("parent_original_invocation"))) is None
        or state.get("ActiveState") not in {"active", "activating"}
        or (not group.startswith("/user.slice/user-"+str(CONTROL_UID)+".slice/user@"+str(CONTROL_UID)+".service/") if USER_MODE else not group.startswith("/system.slice/")) or group.rsplit("/", 1)[-1] != unit
        or [line[3:] for line in cgroups.splitlines() if line.startswith("0::")] != [group]
        or fields.get("Uid", "").split() != [str(CONTROL_UID if USER_MODE else 1001)] * 4
        or fields.get("Gid", "").split() != [str(USER_COORDINATES["gid"] if USER_MODE else 1001)] * 4
        or fields.get("NoNewPrivs", "").strip() != "1"
        or re.fullmatch("0+", fields.get("CapEff", "").strip()) is None
        or re.fullmatch("0+", fields.get("CapPrm", "").strip()) is None
        or re.fullmatch("0+", fields.get("CapAmb", "").strip()) is None):
        raise ValueError("validation_actual_execution_identity_unverified")


def _worker_config_bytes(path, *, maximum=262144):
    """Read ordinary user-owned configuration without following replacements."""
    path = Path(path)
    if (not path.is_absolute() or '..' in path.parts
            or any(parent.is_symlink() for parent in (path.parent, *path.parent.parents))):
        raise ValueError('worker_finalization_config_path_untrusted')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        before = os.fstat(descriptor)
        identity = lambda row: (row.st_dev, row.st_ino, row.st_mode, row.st_uid,
            row.st_nlink, row.st_size, row.st_mtime_ns, row.st_ctime_ns)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_uid != os.geteuid() or before.st_mode & 0o022
                or not 0 < before.st_size <= maximum):
            raise ValueError('worker_finalization_config_untrusted')
        raw = os.read(descriptor, maximum + 1)
        if (len(raw) != before.st_size or identity(before) != identity(os.fstat(descriptor))
                or identity(before) != identity(path.lstat())):
            raise ValueError('worker_finalization_config_changed')
        return raw
    finally:
        os.close(descriptor)


def verify_worker_finalization(runtime, *, body, report):
    """Independently measure final manual-release facts inside the sealed child.

    No parent exit status or environment flag proves these facts. The receipt
    is physically re-read, the scope file comes from the authenticated running
    RPC process, and the exact immutable source is compared with trusted Git.
    """
    from eimemory.core.runtime_manager import read_registry
    from eimemory.models.records import ScopeRef
    from eimemory.version import __version__
    from eimemory.governance.release.evidence_contract import (
        current_deployment_receipt_for_owner, deployment_receipt_for_scope,
        same_release_authority, verified_deployment_receipt_identity,
    )
    from eimemory.governance.release.release_binding_refresh import refresh_bindings
    from deploy.refresh_release_scope_bindings import rpc_process_environment
    from deploy.clean_release_bytecode import validate_release_source

    commit = body.get('target_commit')
    if (body.get('manual_release') is not True or commit != body.get('candidate_commit')
            or re.fullmatch('[0-9a-f]{40}', str(commit)) is None
            or report.get('ok') is not True or report.get('business_qualified') is not True
            or report.get('l5_certification') != 'not_certified'):
        raise ValueError('worker_finalization_manual_business_required')
    registry = read_registry()
    repository = registry['repository_root']
    releases = _WORKER_RELEASES_ROOT
    target = releases / commit
    current = _WORKER_CURRENT_LINK
    if (target.is_symlink() or current.resolve(strict=True) != target
            or not os.statvfs(target).f_flag & os.ST_RDONLY
            or not os.statvfs(target / 'eimemory').f_flag & os.ST_RDONLY):
        raise ValueError('worker_finalization_source_not_readonly')
    # Includes untracked source bytecode: a cleanup claim is never sufficient.
    validate_release_source(release_dir=target, releases_root=releases,
        repo_root=repository, commit=commit)
    scope = ScopeRef.from_dict(body['scope'])
    receipt_id = (report.get('record_ids') or {}).get('deployment_receipt')
    if not isinstance(receipt_id, str) or not receipt_id:
        raise ValueError('worker_finalization_receipt_reference_required')
    identity = current_deployment_receipt_for_owner(runtime, receipt_id, scope,
        expected_commit=commit, expected_version=__version__)
    if identity is None or identity.commit != commit or identity.receipt_id != receipt_id:
        raise ValueError('worker_finalization_current_receipt_unverified')
    process = rpc_process_environment(commit)
    config_dir = Path(process.get('EIMEMORY_CONFIG_DIR') or _WORKER_CONFIG_DIR)
    settings = Path(process.get('EIMEMORY_CONFIG_PATH') or config_dir / 'settings.json')
    if config_dir != _WORKER_CONFIG_DIR or settings != config_dir / 'settings.json':
        raise ValueError('worker_finalization_settings_path_unexpected')
    settings_bytes = _worker_config_bytes(settings)
    value = _strict_json(settings_bytes)
    if not isinstance(value, dict) or value.get('trusted_repository_root') != repository:
        raise ValueError('worker_finalization_settings_repository_mismatch')
    binding_value = process.get('EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE')
    if not isinstance(binding_value, str) or not binding_value:
        raise ValueError('worker_finalization_scope_bindings_required')
    binding = Path(binding_value)
    # Pins are refreshed in place; production keeps them under storage state
    # while settings stay under config. Either reviewed parent is accepted.
    if binding.parent not in {config_dir, _WORKER_STATE_DIR}:
        raise ValueError('worker_finalization_binding_path_unexpected')
    # The normal worker refresh precedes this child. Business validation can
    # advance metadata on the same physical receipt, so refresh idempotently
    # through the existing pin-preserving writer, then independently re-read.
    count = refresh_bindings(runtime, binding, receipt_id)
    raw = _worker_config_bytes(binding, maximum=65536)
    entries = _strict_json(raw)
    if (type(count) is not int or count <= 0 or not isinstance(entries, list)
            or len(entries) != count):
        raise ValueError('worker_finalization_binding_inventory_invalid')
    previous = os.environ.get('EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE')
    try:
        os.environ['EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE'] = str(binding)
        for entry in entries:
            if not isinstance(entry, dict) or entry.get('receipt_id') != receipt_id:
                raise ValueError('worker_finalization_binding_receipt_mismatch')
            record = deployment_receipt_for_scope(runtime, receipt_id, ScopeRef.from_dict(entry['scope']))
            actual = verified_deployment_receipt_identity(record)
            physical_digest = sha256(json.dumps(record.to_dict(), ensure_ascii=False,
                sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest() if record else ''
            if (not same_release_authority(actual, identity)
                    or entry.get('receipt_sha256') != physical_digest):
                raise ValueError('worker_finalization_binding_unverified')
    finally:
        if previous is None:
            os.environ.pop('EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE', None)
        else:
            os.environ['EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE'] = previous
    if (_worker_config_bytes(settings) != settings_bytes
            or _worker_config_bytes(binding, maximum=65536) != raw
            or rpc_process_environment(commit) != process
            or current.resolve(strict=True) != target):
        raise ValueError('worker_finalization_changed_during_verification')
    return dict(schema='managed_worker_finalization.v1', ok=True, commit=commit,
        repository_root=repository, receipt_id=receipt_id, binding_configured=True,
        binding_count=count, binding_digest=sha256(raw).hexdigest(),
        settings_digest=sha256(settings_bytes).hexdigest(), source_verified=True,
        source_readonly=True, bytecode_clean=True, l5_credit=False)


def main():
    if (len(sys.argv) != 5 or sys.argv[1] != "--job-key" or sys.argv[3] != "--operation"
        or re.fullmatch("[0-9a-f]{64}", sys.argv[2]) is None
        or sys.argv[4] not in {"receipt", "pre_observation", "post_resume"}
        or os.getuid() != (CONTROL_UID if USER_MODE else 1001) or os.geteuid() != (CONTROL_UID if USER_MODE else 1001)):
        raise SystemExit("validation_fixed_low_privilege_entry_required")
    key, operation = sys.argv[2], sys.argv[4]
    record = _root_job(key)
    original = record["request"]
    manual = record.get("manual_release") is True
    native, peer_uid = _root_validation_status(key, operation)
    controller = original.get("validation_controller_commit") if manual else original.get("controller_commit")
    target = native.get("target_commit")
    if (target not in ({original.get("base_commit"), original.get("candidate_commit")} if manual else {original.get("target_commit")})
        or record.get("original_invocation") != native.get("parent_original_invocation")
        or native.get("manual_release", False) is not manual):
        raise ValueError("validation_root_job_coordinates_invalid")
    request = {**original, "controller_commit": controller, "target_commit": target,
        "launch_controller_commit": original["controller_commit"], "manual_release": manual}
    if any(re.fullmatch("[0-9a-f]{40}", str(request.get(name))) is None
        for name in ("controller_commit", "target_commit")):
        raise ValueError("validation_root_job_commit_invalid")
    release = Path("/opt/eimemory/releases") / request["controller_commit"]
    if (release.is_symlink() or Path(__file__).resolve().parents[1] != release
        or not os.statvfs(release).f_flag & os.ST_RDONLY
        or not os.statvfs(release / "eimemory").f_flag & os.ST_RDONLY
        or (release / ".venv").is_symlink()):
        raise ValueError("validation_target_source_not_readonly")
    # The root-reviewed launcher pins the interpreter's empty cache prefix
    # before Python starts. -B alone does not prevent loading an existing PYC.
    cache = Path(sys.pycache_prefix or "")
    if cache != (USER_COORDINATES["pycache"] if USER_MODE else Path("/var/lib/eimemory-manager/empty-pycache")):
        raise ValueError("validation_empty_pycache_prefix_required")
    if USER_MODE:user_control.trusted_directory(cache)
    for parent in (() if USER_MODE else (cache, *cache.parents)):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("validation_pycache_prefix_untrusted")
    if any(cache.iterdir()):
        raise ValueError("validation_pycache_prefix_not_empty")
    # An environment InvocationID is a claim. The authenticated root response
    # and actual kernel cgroup/identity must agree before any product import.
    _assert_execution_identity(request, key, operation, native,
        os.environ.get("INVOCATION_ID", ""), Path("/proc/self/status").read_text(),
        Path("/proc/self/cgroup").read_text())
    print(json.dumps({"schema":"managed_code_validation_bootstrap.v1", "job_key":key,
        "operation":operation, "invocation":native["original_invocation"], ("controller_peer_uid" if USER_MODE else "host_root_peer_uid"):peer_uid, "control_plane":"user" if USER_MODE else "legacy_system",
        "uid_map":Path("/proc/self/uid_map").read_text(), "gid_map":Path("/proc/self/gid_map").read_text(),
        ("controller_job_uid" if USER_MODE else "root_job_uid"):(((USER_COORDINATES["state"] if USER_MODE else Path("/var/lib/eimemory-manager"))/"jobs")/(key+".json")).lstat().st_uid,
        ("controller_registry_uid" if USER_MODE else "root_registry_uid"):(USER_COORDINATES["registry"] if USER_MODE else Path("/etc/eimemory-code-automation-authority/runtime-manager.v1.json")).lstat().st_uid,
        ("controller_pycache_uid" if USER_MODE else "root_pycache_uid"):cache.lstat().st_uid, ("controller_socket_uid" if USER_MODE else "root_socket_uid"):(USER_COORDINATES["socket"] if USER_MODE else Path("/run/eimemory-manager/bridge.sock")).lstat().st_uid,
        "actual_cgroup":Path("/proc/self/cgroup").read_text()}, sort_keys=True), flush=True)
    sys.path.insert(0, str(release))
    # These are the same narrowly allowlisted configuration readers used by
    # the standard installer. Their target source is inside the RO release.
    from deploy.run_with_governance_env import (load_governance_environment,
        load_runtime_recall_environment, load_runtime_authority_environment)
    config = Path("/etc/eimemory")
    native_invocation = os.environ.get("INVOCATION_ID", "")
    environment = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": __import__("pwd").getpwuid(os.geteuid()).pw_dir, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1", "INVOCATION_ID": native_invocation,
        "EIMEMORY_ROOT": "/var/lib/eimemory", "EIMEMORY_CONFIG_DIR": str(config),
        "EIMEMORY_RUNTIME_COMMIT": request["controller_commit"],
        "EIMEMORY_EVIDENCE_RECEIPT_ENV_FILE": str(config / "evidence-receipt.env")}
    environment = load_governance_environment(config / "governance.env",
        base_environment=environment, optional=True)
    environment = load_runtime_recall_environment(config, Path("/var/lib/eimemory"),
        base_environment=environment)
    environment = load_runtime_authority_environment(config / "runtime-authority.env",
        base_environment=environment, optional=True)
    os.environ.clear()
    os.environ.update(environment)
    import eimemory
    if Path(eimemory.__file__).resolve().parent.parent != release:
        raise ValueError("validation_product_import_mismatch")
    from eimemory.api.runtime import Runtime
    from eimemory.governance.evolution.code_deployment_job import run_validation, validation_context
    validation_context(key, operation)
    runtime = Runtime.create(root="/var/lib/eimemory", bootstrap_schema=False)
    try:
        result = run_validation(runtime, key=key, operation=operation)
        print(json.dumps({"job_key": key, "operation": operation,
            "validation_artifact_written": True, "ok": result.get("ok") is True}, sort_keys=True))
        return 0 if result.get("ok") is True else 1
    finally:
        runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())
