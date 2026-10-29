"""Bounded, read-only operational observations; never natural-query evidence.

Only the native collector is a measurement authority. ``assess`` validates its
shape and integrity, not a signature from an arbitrary external JSON document.
An enclosing release acceptance receipt must authenticate this report.
"""
from __future__ import annotations

from datetime import datetime, timezone
from contextvars import ContextVar
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import time
from typing import Any
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from eimemory.core.strict_json import loads as strict_json_loads

SCHEMA = "controlled_operational_stability.v1"
SAMPLE_SCHEMA = "controlled_operational_stability_sample.v1"
TOPOLOGY_SCHEMA = "controlled_operational_stability.v2"
TOPOLOGY_SAMPLE_SCHEMA = "controlled_operational_stability_sample.v2"
RUNTIME_UNITS = ("eimemory-rpc.service", "hermes-gateway.service")
_TOPOLOGY_UNSET = object()
_OBSERVATION_TOPOLOGY = ContextVar("eimemory_stability_topology", default=_TOPOLOGY_UNSET)
TIMER_UNITS = (
    "eimemory-nightly.timer", "eimemory-backup.timer", "eimemory-audit-verify.timer",
    "eimemory-timer-monitor.timer", "eimemory-vector-sync.timer",
    "eimemory-real-effect.timer", "eimemory-code-implementation-refresh.timer",
    "eimemory-l5-effect-review.timer", "eimemory-l1-extract.timer",
)
JOB_UNITS = tuple(name.removesuffix(".timer") + ".service" for name in TIMER_UNITS)
UNITS = RUNTIME_UNITS + TIMER_UNITS + JOB_UNITS


def _topology():
    value = _OBSERVATION_TOPOLOGY.get()
    if value is _TOPOLOGY_UNSET:
        from eimemory.contracts.hermes_topology import load_topology
        return load_topology()
    return value


def _runtime_units() -> tuple[str, ...]:
    topology = _topology()
    if topology is not None:
        # Expected owners come from the operator declaration, never from the
        # subset that happens to be active after a gateway crash.
        return tuple(topology['runtime_units'])
    from eimemory.ops.runtime_service_observation import registered_runtime_context
    if not registered_runtime_context():
        return _user_runtime_units()
    from eimemory.core.runtime_manager import read_registry
    from eimemory.contracts.runtime_manager import compatibility_units
    registry = read_registry()
    profiles = sorted(name for name in compatibility_units(registry['units'])
                      if name.startswith('hermes-gateway-') and registry['units'][name]['installed'])
    units = (*RUNTIME_UNITS, *profiles)
    # Preserve the original complete-team coverage floor. A smaller declared
    # inventory cannot silently certify a partial deployment.
    if len(units) < 5:
        raise ValueError('stability_team_runtime_inventory_incomplete')
    return units


def _user_runtime_units() -> tuple[str, ...]:
    """Observe the user manager directly, without consulting a root registry."""
    result = subprocess.run(
        ['systemctl', '--user', 'list-unit-files', '--type=service',
         '--no-legend', '--no-pager'], capture_output=True, text=True,
        timeout=30, check=False)
    if result.returncode:
        raise RuntimeError('stability_user_runtime_inventory_unavailable')
    if len(result.stdout.encode()) > 1024 * 1024:
        raise ValueError('stability_user_runtime_inventory_too_large')
    profiles = set()
    for row in result.stdout.splitlines():
        fields = row.split()
        if not fields:
            continue
        name = fields[0]
        if re.fullmatch(r'hermes-gateway-[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\.service', name):
            if name in profiles:
                raise ValueError('stability_user_runtime_inventory_duplicate')
            profiles.add(name)
    units = (*RUNTIME_UNITS, *sorted(profiles))
    if not 5 <= len(units) <= 66:
        raise ValueError('stability_team_runtime_inventory_incomplete')
    return units


def _units() -> tuple[str, ...]:
    return _runtime_units() + TIMER_UNITS + JOB_UNITS


def _required_agents() -> frozenset[str]:
    from eimemory.evaluation.controlled_production_acceptance import (
        DEFAULT_POLICY, _read_policy, _validate_policy,
    )
    # Fixed protected policy path; caller-supplied scope and environment never
    # choose which colleagues count as the complete business team.
    raw = _read_policy(Path(DEFAULT_POLICY))
    policy = _validate_policy(raw, raw['owner_scope'])
    agents = frozenset(entry['scope']['agent_id'] for entry in policy['agents'])
    if len(agents) < 4:
        raise ValueError('stability_team_business_inventory_incomplete')
    return agents

_SHA = re.compile(r"[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_PROPERTIES = "Id,LoadState,ActiveState,SubState,Result,ExecMainStatus,NRestarts,MainPID,UnitFileState"
_JOURNAL_ERROR_PATTERNS = (
    ("crash", r"segfault|core-dump|dumped core"),
    ("oom", r"oom-kill|out of memory|killed process"),
    ("database_read_write_failure", r"database is locked|database is read.only|attempt to write a readonly|disk i/o error|"
     r"database or disk is full|sqlite.*(?:permission denied|operationalerror|databaseerror)"),
)

# Run the installed native configuration and repository in an isolated child
# with the live RPC environment. This avoids mutating this caller's environment
# and avoids constructors that install SQLite schema or projection triggers.
_PG_READ_SCRIPT = r'''
import sys, json, sqlite3, math
from pathlib import Path
sys.path.insert(0, '/opt/eimemory/releases/' + sys.argv[2])
try:
    from eimemory.retrieval.postgres_cli import config_from_env
    from eimemory.retrieval.postgres_vector import (PostgresCandidateRepository,
        PROJECTION_DIGEST_SCHEMA, projection_fingerprint, embedding_provider_fingerprint,
        candidate_index_lag_seconds, _canonical_timestamp)
    from dataclasses import replace
    import psycopg
    from psycopg.rows import dict_row
    config = config_from_env()
    if not config.enabled or not config.configured or config.embedding_provider is None:
        raise ValueError('postgres_runtime_not_configured')
    connections = []
    def connect(*, dsn, connect_timeout_seconds):
        conn = psycopg.connect(dsn, connect_timeout=max(1, int(connect_timeout_seconds)),
            row_factory=dict_row, options='-c default_transaction_read_only=on')
        connections.append(conn)
        return conn
    config = replace(config, connection_factory=connect, connect_timeout_seconds=2,
                     statement_timeout_ms=1500, pool_size=1)
    path = Path(sys.argv[1])
    conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)
    conn.execute('PRAGMA query_only=ON')
    def authority():
        conn.execute('BEGIN')
        try:
            table = 'memory_vector_revision' if config.projection_memory_only else 'vector_sync_revision'
            revision = str(conn.execute('SELECT revision FROM ' + table + ' WHERE singleton=1').fetchone()[0])
            where = "WHERE kind='memory' AND status='active' " if config.projection_memory_only else ''
            head = conn.execute('SELECT updated_at, storage_key FROM records ' + where +
                'ORDER BY updated_at DESC, storage_key DESC LIMIT 1').fetchone()
            return revision, (_canonical_timestamp(head[0]), str(head[1])) if head else ('', '')
        finally:
            conn.rollback()
    try:
        before = authority()
        try:
            state = PostgresCandidateRepository(config).read_index_state()
        finally:
            for connection in connections:
                connection.close()
        after = authority()
        fingerprints = (state.embedding_fingerprint == embedding_provider_fingerprint(config.embedding_provider, config)
            and state.projection_digest_schema == PROJECTION_DIGEST_SCHEMA
            and state.projection_fingerprint == projection_fingerprint(config))
        lag = candidate_index_lag_seconds(state, authority_cursor=after[1], authority_revision=after[0])
        fresh = math.isfinite(lag) and 0 <= lag <= config.max_index_lag_seconds
        ok = config.enabled and config.configured and state.ready and bool(state.watermark)
        ok = bool(ok and before == after and fingerprints and fresh)
        result = dict(ok=ok, measurement_origin='native_readonly_repository_snapshot',
            ready=state.ready, authority_unchanged=before==after, fingerprints_match=fingerprints,
            revision_matches=state.authority_revision==after[0],
            lag_seconds=lag if math.isfinite(lag) else None,
            maximum_lag_seconds=config.max_index_lag_seconds,
            watermark=state.watermark, authority_revision=state.authority_revision,
            error='' if ok else 'projection_not_current')
    finally:
        conn.close()
except Exception as exc:
    result = dict(ok=False, error='native_pg_observation_unavailable', error_type=type(exc).__name__)
print(json.dumps(result, allow_nan=False))
'''


def _digest(value: Any) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _commit(value: str) -> str:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise ValueError("full_expected_commit_required")
    return value


def _run(args: list[str]) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=12, check=False,
                            env={**os.environ, "LC_ALL": "C", "TZ": "UTC"})
    if result.returncode != 0:
        raise RuntimeError("readonly_command_unavailable")
    if len(result.stdout.encode()) > 2 * 1024 * 1024:
        raise ValueError("readonly_command_output_limit")
    return result.stdout


def _unit_states() -> list[dict[str, str]]:
    from eimemory.ops.runtime_service_observation import service_state
    properties = tuple(_PROPERTIES.split(","))
    units = _units()
    states = [service_state(unit, tuple(name for name in properties
        if not unit.endswith('.timer') or name not in {'ExecMainStatus', 'NRestarts', 'MainPID'})) for unit in units]
    if len(states) != len(units) or {s["Id"] for s in states} != set(units):
        raise ValueError("unit_inventory_incomplete")
    return states


def _host_process_observation(state: dict[str,str]) -> dict[str,Any]:
    from eimemory.core.runtime_manager import request
    value=request('runtime_process_environment',unit=state['Id'])['observation']
    fields={'schema','unit','pid','start_ticks','invocation_id','environment'}
    if (not isinstance(value,dict) or set(value) not in (fields, fields | {'namespace_pids'})
            or value['schema']!='eimemory.runtime_process_environment.v1' or value['unit']!=state['Id']
            or type(value['pid']) is not int or value['pid']!=int(state['MainPID']) or value['pid']<=0
            or type(value['start_ticks']) is not int or value['start_ticks']<=0
            or not isinstance(value['invocation_id'],str) or re.fullmatch('[0-9a-f]{32}',value['invocation_id']) is None
            or not isinstance(value['environment'],dict)
            or any(not isinstance(k,str) or not isinstance(v,str) or '\0' in k+v for k,v in value['environment'].items())):
        raise ValueError('runtime_host_process_observation_invalid')
    if 'namespace_pids' in value and (not isinstance(value['namespace_pids'], list)
            or not 0 <= len(value['namespace_pids']) <= 64
            or any(type(pid) is not int or pid <= 0 for pid in value['namespace_pids'])
            or (value['namespace_pids'] and value['namespace_pids'][0] != value['pid'])
            or len(set(value['namespace_pids'])) != len(value['namespace_pids'])):
        raise ValueError('runtime_host_process_observation_invalid')
    return value


def _process_environment(state: dict[str, str]) -> tuple[int, Path, dict[str, str]]:
    from eimemory.ops.runtime_service_observation import registered_runtime_context
    if registered_runtime_context():
        value=_host_process_observation(state)
        return value['pid'],Path('/proc')/str(value['pid']),value['environment']
    pid = int(state["MainPID"])
    if pid <= 0:
        raise ValueError("runtime_pid_unavailable")
    process = Path("/proc") / str(pid)
    if process.stat().st_uid != os.geteuid():
        raise ValueError("runtime_process_owner_mismatch")
    raw = (process / "environ").read_bytes()
    if len(raw) > 1024 * 1024:
        raise ValueError("runtime_environment_limit")
    entries = [item.split("=", 1) for item in raw.decode().split("\0") if "=" in item]
    if len({key for key, _ in entries}) != len(entries):
        raise ValueError("runtime_environment_duplicate")
    return pid, process, dict(entries)


def _postgres_snapshot(states: list[dict[str, str]], database_path: Path, expected_commit: str) -> dict[str, Any]:
    if database_path.is_symlink() or database_path.stat().st_uid != os.geteuid():
        raise ValueError("database_owner_required")
    _pid, _process, env = _process_environment(next(s for s in states if s["Id"] == _runtime_units()[0]))
    binary = Path("/opt/eimemory/releases") / expected_commit / ".venv/bin/python"
    result = subprocess.run([str(binary), "-I", "-B", "-c", _PG_READ_SCRIPT, str(database_path), expected_commit],
        env=env, capture_output=True, text=True, timeout=12, check=False)
    if result.returncode != 0:
        raise RuntimeError("native_pg_child_unavailable")
    value = strict_json_loads(result.stdout, max_bytes=4096)
    if not isinstance(value, dict):
        raise ValueError("native_pg_child_contract_invalid")
    return value


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _authenticated_health_payload(*, timeout_seconds: float = 8.0) -> dict[str, Any]:
    from eimemory.core.rpc_probe_auth import rpc_probe_headers

    if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 8):
        raise ValueError("invalid_health_probe_timeout")
    headers = rpc_probe_headers()
    if not headers.get("Authorization"):
        raise ValueError("authenticated_health_required")
    request = Request("http://127.0.0.1:8091/health", headers=headers)
    with build_opener(ProxyHandler({}), _NoRedirect).open(request, timeout=timeout_seconds) as response:
        raw = response.read(65537)
    value = strict_json_loads(raw, max_bytes=65536, max_depth=32)
    if not isinstance(value, dict):
        raise ValueError("health_object_required")
    return value


def _endpoint_identity(value: dict[str, Any], expected_commit: str) -> dict[str, Any]:
    from eimemory.runtime_identity import package_tree_digest
    from eimemory.version import __version__
    from eimemory.ei_bridge.protocol import EIMEMORY_RPC_CONTRACT_VERSION

    release = Path("/opt/eimemory/releases") / expected_commit
    checks = value.get("checks", {})
    paths = value.get("paths", {})
    digest = value.get("package_tree_digest")
    identity_ok = (isinstance(checks, dict) and isinstance(paths, dict)
        and value.get("commit") == value.get("configured_runtime_commit") == expected_commit
        and value.get("version") == __version__ and value.get("service") == "eimemory-rpc"
        and value.get("contract_version") == EIMEMORY_RPC_CONTRACT_VERSION
        and checks.get("runtime_identity") is True
        and paths.get("release") == str(release) and value.get("import_root") == str(release / "eimemory")
        and isinstance(digest, str) and _HEX64.fullmatch(digest) is not None
        and package_tree_digest(release / "eimemory") == digest)
    return {"schema": "controlled_rpc_release_identity.v1", "ok": bool(identity_ok),
        "expected_commit": expected_commit, "version": __version__ if identity_ok else "",
        "package_tree_digest": digest if identity_ok else "", "identity_only": True,
        "retrieval_health_certified": False}


def collect_controlled_endpoint_identity(*, expected_commit: str) -> dict[str, Any]:
    """Authenticate the actual listener's release; never waive retrieval health.

    A cold listener may serve the controlled probe only after this identity
    check. Its full retrieval health must still pass the separate strict
    stability collection after serving those real requests.
    """
    return _endpoint_identity(_authenticated_health_payload(), _commit(expected_commit))


def _health(expected_commit: str, *, timeout_seconds: float | None = None) -> dict[str, Any]:
    from eimemory.retrieval.readiness_contract import readiness_ok, sanitize_readiness
    value = (_authenticated_health_payload() if timeout_seconds is None
             else _authenticated_health_payload(timeout_seconds=timeout_seconds))
    identity = _endpoint_identity(value, expected_commit)
    store = value.get("store", {})
    checks = value.get("checks", {})
    candidate = value.get("retrieval", {}).get("candidate_source", {})
    current = sanitize_readiness(candidate.get("current_readiness"))
    lag = current.get("lag_seconds")
    pg_ok = (candidate.get("enabled") is True and candidate.get("configured") is True
             and readiness_ok(current))
    return {"ok": value.get("ok") is True and identity["ok"] and pg_ok and checks.get("ready") is True
                    and store.get("migration_complete") is True,
            "identity_ok": identity["ok"], "version": identity["version"],
            "package_tree_digest": identity["package_tree_digest"], "postgres_fresh": pg_ok,
            "postgres_lag_seconds": lag if type(lag) in (int, float) and math.isfinite(lag) else None,
            "postgres_error": current.get("last_error") if current.get("last_error") in
                {"", "authority_revision_changed", "authority_revision_stale", "index_lag_exceeded",
                 "index_not_ready", "candidate_health_unavailable", "postgres_unavailable",
                 "circuit_open"} else "unavailable",
            "migration_complete": store.get("migration_complete") is True,
            "postgres_health_model": "native_readonly_vector_query.v1", "postgres_readiness": current,
            "last_query_valid": candidate.get("query_valid") is True,
            "last_query_error": candidate.get("last_error") if candidate.get("last_error") in
                {"", "authority_revision_changed", "authority_revision_stale", "index_lag_exceeded",
                 "index_not_ready", "candidate_health_unavailable", "postgres_unavailable",
                 "circuit_open"} else "unavailable"}


def _current_pg_agrees(health, snapshot, *, at=None):
    from eimemory.retrieval.readiness_contract import readiness_ok

    current = health.get("postgres_readiness")
    return bool(health.get("postgres_health_model") == "native_readonly_vector_query.v1"
        and readiness_ok(current, at=at) and snapshot.get("ok") is True
        and current.get("watermark") == snapshot.get("watermark")
        and current.get("authority_revision") == snapshot.get("authority_revision"))


def _process_status(state: dict[str, str], expected_commit: str) -> dict[str, Any]:
    from eimemory.ops.runtime_service_observation import registered_runtime_context
    if registered_runtime_context():
        value=_host_process_observation(state)
        result=_capture_environment_status(value['environment'],state['Id'],expected_commit)
        result.update(pid=value['pid'],start_ticks=value['start_ticks'])
        if _topology() is not None:
            result['invocation_id'] = value['invocation_id']
        return result
    pid, process, env = _process_environment(state)
    result = _capture_environment_status(env, state["Id"], expected_commit)
    # A manual restart can reset NRestarts to zero. Bind the actual process too.
    process_stat = (process / "stat").read_text().rsplit(")", 1)[1].split()
    result["pid"] = pid
    result["start_ticks"] = int(process_stat[19])
    return result


def _credential_binding(states: list[dict[str, str]]) -> bool:
    from eimemory.adapters.runtime.host_auth import attestation_tokens_from_private_file, producer_token_from_private_file

    _pid, _process, rpc_env = _process_environment(next(s for s in states if s["Id"] == _runtime_units()[0]))
    authorized = attestation_tokens_from_private_file(environ=rpc_env)
    for state in states:
        if state["Id"] not in _runtime_units()[1:]:
            continue
        _pid, _process, env = _process_environment(state)
        token = producer_token_from_private_file("hermes", environ=env)
        if not token or authorized.get(token) != "hermes":
            return False
    return True


def _capture_environment_status(env: dict[str, str], unit: str, expected_commit: str) -> dict[str, Any]:
    allowed = strict_json_loads(env.get("EIMEMORY_CAPTURE_QUERY_SCOPES", "null"), max_bytes=65536)
    topology = _topology()
    coverage = None
    fields = {"tenant_id", "agent_id", "workspace_id", "user_id", "channel", "source_id"}
    valid = (isinstance(allowed, list) and (1 <= len(allowed) <= 100 if topology is not None else len(allowed) == 6) and all(
        isinstance(entry, dict) and set(entry) == fields and all(
            isinstance(v, str) and bool(v.strip()) and "*" not in v for v in entry.values())
        for entry in allowed))
    if topology is not None:
        from eimemory.evaluation.hermes_profile_coverage import capture_coverage
        coverage = capture_coverage(allowed, topology)
        valid = valid and coverage['ok']
    elif valid:
        valid = len({_digest(entry) for entry in allowed}) == 6 and _required_agents().issubset(
            {entry["agent_id"] for entry in allowed})
    from eimemory.adapters.runtime.host_auth import attestation_tokens_from_private_file, producer_token_from_private_file
    if unit == "eimemory-rpc.service":
        producer_ready = "hermes" in attestation_tokens_from_private_file(environ=env).values()
    else:
        producer_ready = bool(producer_token_from_private_file("hermes", environ=env))
    capture = env.get("EIMEMORY_CAPTURE_ORIGINAL_QUERY") == "1"
    budget = env.get("EIMEMORY_RECALL_BUDGET_SECONDS") == "8"
    result = {"unit": unit, "ok": valid and capture and budget and producer_ready
                and env.get("EIMEMORY_RUNTIME_COMMIT") == expected_commit,
            "capture_enabled": capture, "capture_scope_count": len(allowed) if isinstance(allowed, list) else 0,
            "capture_policy_valid": bool(valid), "capture_policy_digest": _digest(allowed) if valid else "",
            "recall_budget_seconds": 8 if budget else None, "hermes_attestation_ready": producer_ready,
            "runtime_commit_matches": env.get("EIMEMORY_RUNTIME_COMMIT") == expected_commit}
    if coverage is not None:
        result['profile_capture_coverage'] = coverage
    return result


def _database(path: Path) -> dict[str, Any]:
    if not path.is_absolute() or path.is_symlink():
        raise ValueError("database_path_invalid")
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid():
        raise ValueError("database_owner_required")
    owners = []
    for file in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        if not file.exists() and not file.is_symlink():
            continue
        info = file.lstat()
        valid = (stat.S_ISREG(info.st_mode) and info.st_uid == before.st_uid
                 and info.st_gid == before.st_gid and stat.S_IMODE(info.st_mode) == 0o600)
        owners.append({"file": "database" if file == path else file.name[-3:], "uid": info.st_uid,
                       "gid": info.st_gid, "mode": format(stat.S_IMODE(info.st_mode), "04o"), "ok": valid})
    if not all(item["ok"] for item in owners):
        raise ValueError("database_sidecar_owner_or_mode_mismatch")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)
    deadline = time.monotonic() + 2
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
        result = connection.execute("SELECT 1 FROM records LIMIT 1").fetchone() == (1,)
        schema_identity = _schema_identity(connection)
    finally:
        connection.close()
    after = path.stat()
    inode_ok = (before.st_dev, before.st_ino, before.st_uid) == (after.st_dev, after.st_ino, after.st_uid)
    accessible = os.access(path, os.R_OK | os.W_OK)
    integrity = _recent_backup(path, schema_identity)
    return {"ok": result and inode_ok and accessible and integrity["ok"], "read_smoke_ok": result,
            "owner_read_write_access": accessible, "write_probe_performed": False,
            "same_database_inode": inode_ok, "ownership": owners,
            "integrity_basis": "verified_backup_snapshot", "current_full_integrity_scan_performed": False,
            "backup_integrity": integrity,
            "database_identity": _digest([before.st_dev, before.st_ino, before.st_uid, before.st_gid])}


def _schema_identity(connection: sqlite3.Connection) -> str:
    columns = [list(row) for row in connection.execute("PRAGMA table_info(records)")]
    if not columns:
        raise ValueError("database_records_schema_unavailable")
    return _digest({"records_columns": columns,
        "user_version": connection.execute("PRAGMA user_version").fetchone()[0],
        "application_id": connection.execute("PRAGMA application_id").fetchone()[0]})


def _recent_backup(database_path: Path, live_schema_identity: str) -> dict[str, Any]:
    """Read a protected, published native backup receipt, never a partial set.

    Full snapshot integrity and export hashing belong to the standard backup
    worker. This light probe authenticates their protected local publication and
    schema/file bindings; it does not rehash gigabytes or certify live pages.
    A database backup does not attest the version of running application code.
    """
    root = database_path.parent.parent
    backup_root = root / "backups"
    if backup_root.is_symlink() or not backup_root.is_dir():
        raise ValueError("published_verified_backup_unavailable")
    sets = sorted((p for p in backup_root.iterdir() if re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", p.name)),
                  key=lambda p: p.name, reverse=True)
    if not sets:
        raise ValueError("published_verified_backup_unavailable")
    selected = sets[0]
    info = selected.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError("backup_directory_not_private")
    manifest = selected / "backup-set.json"
    descriptor = os.open(manifest, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_size > 1024 * 1024):
            raise ValueError("backup_manifest_not_private_or_bounded")
        raw = os.read(descriptor, 1024 * 1024 + 1)
    finally:
        os.close(descriptor)
    value = strict_json_loads(raw, max_bytes=1024 * 1024)
    if not isinstance(value, dict):
        raise ValueError("backup_manifest_invalid")
    created = datetime.strptime(selected.name, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - created).total_seconds()
    valid = (value.get("schema") == "eimemory.backup_set.v1" and value.get("ok") is True
        and value.get("created_at") == selected.name and value.get("root") == str(root)
        and 0 <= age <= 86400 and value.get("config", {}).get("ok") is True
        and value.get("payload_archive", {}).get("ok") is True)
    records = value.get("records", {})
    valid = valid and (records.get("ok") is True and type(records.get("create_exit_code")) is int
        and records.get("create_exit_code") == 0 and type(records.get("verify_exit_code")) is int
        and records.get("verify_exit_code") == 0 and type(records.get("record_count")) is int
        and records["record_count"] > 0 and isinstance(records.get("sha256"), str)
        and _HEX64.fullmatch(records["sha256"]) is not None)
    relative = "sqlite/" + database_path.name
    items = [item for item in value.get("sqlite", []) if isinstance(item, dict) and item.get("file") == relative]
    files = [item for item in value.get("files", []) if isinstance(item, dict) and item.get("path") == relative]
    valid = valid and len(items) == len(files) == 1
    if not valid:
        raise ValueError("published_backup_contract_invalid_or_stale")
    item, inventory = items[0], files[0]
    valid = (item.get("source") == str(database_path) and item.get("ok") is True
        and item.get("integrity_check") == "ok" and type(item.get("records_rows")) is int
        and item["records_rows"] > 0 and type(inventory.get("bytes")) is int
        and inventory["bytes"] > 0 and isinstance(inventory.get("sha256"), str)
        and _HEX64.fullmatch(inventory["sha256"]) is not None)
    snapshot = selected / "sqlite" / database_path.name
    if (snapshot.parent.is_symlink() or snapshot.is_symlink() or not snapshot.is_file()):
        raise ValueError("published_backup_snapshot_unavailable")
    snapshot_info = snapshot.stat()
    valid = valid and (snapshot_info.st_uid == os.geteuid() and stat.S_IMODE(snapshot_info.st_mode) == 0o600
                      and snapshot_info.st_size == inventory["bytes"])
    if not valid:
        raise ValueError("published_backup_snapshot_binding_invalid")
    sealed = sqlite3.connect(snapshot.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=2)
    try:
        sealed.execute("PRAGMA query_only=ON")
        schema_matches = _schema_identity(sealed) == live_schema_identity
    finally:
        sealed.close()
    if not schema_matches:
        raise ValueError("backup_snapshot_schema_version_mismatch")
    return {"ok": True, "created_at": created.isoformat(), "age_seconds": round(age, 3),
        "maximum_age_seconds": 86400, "manifest_digest": sha256(raw).hexdigest(),
        "schema_matches_current_database": True, "native_sqlite_integrity_check_ok": True,
        "native_logical_export_verified": True, "published_set": True,
        "snapshot_content_hash_recomputed": False, "certifies_application_release": False}


def _journal_output(args: list[str]) -> str:
    """Distinguish native grep's empty match from a failed journal observation."""
    deadline = time.monotonic() + 12

    def run(command):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("readonly_command_timeout")
        result = subprocess.run(command, capture_output=True, text=True, timeout=remaining, check=False,
                                env={**os.environ, "LC_ALL": "C", "TZ": "UTC"})
        if time.monotonic() > deadline:
            raise TimeoutError("readonly_command_timeout")
        if result.stderr:
            raise RuntimeError("journal_command_unavailable")
        if len(result.stdout.encode()) > 2 * 1024 * 1024:
            raise ValueError("readonly_command_output_limit")
        return result

    # A grep with no matches exits 1 without output. First establish that the
    # same user's units and full window are readable without the grep filter.
    preflight = ["--lines=1" if arg.startswith("--lines=") else arg for arg in args
                 if not arg.startswith(("--grep=", "--case-sensitive="))]
    readable = run(preflight)
    if readable.returncode != 0:
        raise RuntimeError("journal_preflight_unavailable")
    preflight_lines = readable.stdout.splitlines()
    if (len(preflight_lines) > 1 or any(not isinstance(strict_json_loads(line, max_bytes=262144), dict)
                                        for line in preflight_lines)):
        raise ValueError("journal_preflight_contract_invalid")
    result = run(args)
    if result.returncode == 0 or (result.returncode == 1 and result.stdout == ""):
        return result.stdout
    raise RuntimeError("journal_command_unavailable")


def _journal() -> dict[str, Any]:
    from eimemory.ops.runtime_service_observation import registered_runtime_context
    if registered_runtime_context():
        from eimemory.core.runtime_manager import request
        return request("error_window")["report"]
    end = time.time()
    counts = {name: 0 for name, _ in _JOURNAL_ERROR_PATTERNS}
    pattern = '|'.join('(?:' + expression + ')' for _, expression in _JOURNAL_ERROR_PATTERNS)
    args = ['journalctl', '--user', '--quiet', '--no-pager', '--all', '--output=json',
            '--lines=2001', '--since=@' + str(end - 600), '--until=@' + str(end),
            '--grep=' + pattern, '--case-sensitive=no']
    for index, unit in enumerate(sorted(set(_runtime_units() + JOB_UNITS))):
        if index:
            args.append('+')
        args += ['_SYSTEMD_USER_UNIT=' + unit, '_UID=' + str(os.geteuid())]
    lines = _journal_output(args).splitlines()
    if len(lines) > 2000:
        raise ValueError('journal_window_truncated')
    for line in lines:
        entry = strict_json_loads(line, max_bytes=262144)
        if not isinstance(entry, dict) or not isinstance(entry.get('MESSAGE'), str):
            raise ValueError('journal_message_contract_invalid')
        matched = {name: bool(re.search(expression, entry['MESSAGE'], re.I))
                   for name, expression in _JOURNAL_ERROR_PATTERNS}
        if not any(matched.values()):
            raise ValueError('journal_filter_contract_invalid')
        for name, hit in matched.items():
            counts[name] += int(hit)
    return {'ok': not any(counts.values()), 'window_seconds': 600,
            'observation_scope': 'allowlisted_user_unit_journal',
            'entries_observed': len(lines), 'entry_selection': 'native_error_pattern_matches',
            'native_database_counters_available': False, 'observed_error_counts': counts}



def reset_failed_allowlisted_job_units() -> dict[str, Any]:
    """Clear sticky oneshot ``Result=failed`` before restored stability observation.

    Companion to the Result-after-reset-failed trust rule in the sample collector.
    Does not start or stop units. Best-effort: per-unit failures are recorded and
    never raised so a single reset miss cannot abort release closure.
    """
    reset: list[str] = []
    errors: dict[str, str] = {}
    for unit in JOB_UNITS:
        try:
            completed = subprocess.run(
                ["systemctl", "--user", "reset-failed", unit],
                check=False, capture_output=True, text=True, timeout=30,
                env={**os.environ, "LC_ALL": "C", "TZ": "UTC"},
            )
            if completed.returncode == 0:
                reset.append(unit)
            else:
                errors[unit] = f"exit:{completed.returncode}"
        except (OSError, subprocess.SubprocessError) as exc:
            errors[unit] = type(exc).__name__
    return {
        "ok": not errors,
        "action": "systemctl_user_reset_failed",
        "units": list(JOB_UNITS),
        "reset": reset,
        "errors": errors,
    }


def allowlisted_job_units_restored_ready() -> dict[str, Any]:
    """Read-only: whether allowlisted oneshot JOB_UNITS are quiet (not failed).

    Used to drop stale ``allowlisted_unit_not_ready`` release-closure incidents
    after operators (or release closure) cleared sticky failed Result bits.
    """
    from eimemory.ops.runtime_service_observation import service_state
    units: list[dict[str, Any]] = []
    ready = True
    for unit in JOB_UNITS:
        try:
            state = service_state(
                unit, ("LoadState", "ActiveState", "SubState", "Result", "ExecMainStatus"))
        except Exception as exc:  # observation miss fails closed for this helper
            units.append({"unit": unit, "ok": False, "error_type": type(exc).__name__})
            ready = False
            continue
        load = str(state.get("LoadState") or "")
        active = str(state.get("ActiveState") or "")
        result = str(state.get("Result") or "")
        exec_status = str(state.get("ExecMainStatus") or "")
        ok = (load == "loaded" and result in ("", "success")
              and active in ("active", "inactive", "activating"))
        if ok and active in ("active", "activating"):
            ok = exec_status == "0"
        units.append({
            "unit": unit, "ok": bool(ok), "active_state": active, "result": result,
            "exec_main_status": exec_status,
        })
        ready = ready and bool(ok)
    return {"ok": ready, "units": units, "read_only": True}


def collect_controlled_stability_sample(*, expected_commit: str, database_path: str | Path,
                                        background_held: bool = False) -> dict[str, Any]:
    """Freeze the registered identity inventory for this complete measurement.

    When ``background_held`` is True, timer/job units are scored against the
    admission hold contract (enabled timers inactive; writer jobs quiet), not
    the restored active-timer inventory. Core runtime units stay required.
    """
    topology = _topology()
    token = _OBSERVATION_TOPOLOGY.set(topology)
    try:
        return _collect_controlled_stability_sample(expected_commit=expected_commit,
                                                     database_path=database_path,
                                                     background_held=background_held is True)
    finally:
        _OBSERVATION_TOPOLOGY.reset(token)


def _collect_controlled_stability_sample(*, expected_commit: str, database_path: str | Path,
                                         background_held: bool = False) -> dict[str, Any]:
    """Collect once as the database owner; no recall, Runtime, DB writes or actions."""
    commit = _commit(expected_commit)
    result: dict[str, Any] = {"schema": SAMPLE_SCHEMA, "expected_commit": commit,
        "observed_at": datetime.now(timezone.utc).isoformat(), "monotonic_seconds": time.monotonic(),
        "boot_id": "", "collector_origin": "native_readonly_host_probe", "read_only": True,
        "natural_certified": False, "blocked_reasons": [],
        "background_held_observation": background_held is True}
    topology = _topology()
    if topology is not None:
        result.update(schema=TOPOLOGY_SAMPLE_SCHEMA, topology=topology)
    if os.name != "posix" or not hasattr(os, "geteuid"):
        result["blocked_reasons"].append("linux_database_owner_probe_required")
    else:
        try:
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            if not re.fullmatch(r"[0-9a-f-]{36}", boot_id):
                raise ValueError("boot_identity_invalid")
            result["boot_id"] = boot_id
        except (OSError, ValueError):
            result["blocked_reasons"].append("boot_identity_unavailable")
        for name, operation in (("health", lambda: _health(commit)), ("database", lambda: _database(Path(database_path))),
                                ("journal", _journal)):
            try:
                result[name] = operation()
                if result[name].get("ok") is not True:
                    result["blocked_reasons"].append(name + "_not_ready")
            except Exception as exc:
                result[name] = {"ok": False, "error_type": type(exc).__name__}
                if isinstance(exc, sqlite3.Error):
                    code = getattr(exc, "sqlite_errorname", "")
                    result[name]["error_code"] = code if re.fullmatch(r"SQLITE_[A-Z_]{1,64}", code) else "SQLITE_UNKNOWN"
                result["blocked_reasons"].append(name + "_unavailable")
        try:
            states = _unit_states()
            if topology is not None:
                from eimemory.ops.hermes_native_topology import collect_native_profile_topology
                native_profiles = collect_native_profile_topology(topology,
                    next(state for state in states if state['Id'] == 'hermes-gateway.service'),
                    expected_commit=commit)
                result['native_profile_topology'] = native_profiles
                if native_profiles.get('complete') is not True:
                    result['blocked_reasons'].append('native_profile_topology_unverified')
            try:
                result["postgres_snapshot"] = _postgres_snapshot(states, Path(database_path), commit)
            except Exception as exc:
                result["postgres_snapshot"] = {"ok": False, "error_type": type(exc).__name__}
            if result["postgres_snapshot"].get("ok") is not True:
                result["blocked_reasons"].append("native_postgres_not_current")
            if not _current_pg_agrees(result.get("health", {}), result["postgres_snapshot"]):
                result["blocked_reasons"].append("postgres_readiness_identity_mismatch")
            units = []
            for state in states:
                unit = state["Id"]
                if unit in _runtime_units():
                    ok = (state["LoadState"] == "loaded" and state["Result"] in ("", "success")
                          and state["ActiveState"] == "active" and state["ExecMainStatus"] == "0")
                elif unit in TIMER_UNITS:
                    enabled = state["UnitFileState"] in ("enabled", "enabled-runtime")
                    if background_held:
                        # Admission holds writers: enabled timers must stay inactive.
                        ok = (state["LoadState"] == "loaded" and enabled
                              and state["ActiveState"] == "inactive"
                              and state["Result"] in ("", "success"))
                    else:
                        ok = (state["LoadState"] == "loaded" and enabled
                              and state["ActiveState"] == "active"
                              and state["Result"] in ("", "success"))
                else:
                    if background_held:
                        # verify_background_quiet allows inactive or failed oneshots.
                        # After `systemctl reset-failed`, Result becomes success while
                        # ExecMainStatus may keep the prior exit code; trust Result when quiet.
                        ok = state["LoadState"] == "loaded" and state["ActiveState"] in ("inactive", "failed")
                        if state["ActiveState"] != "failed":
                            ok = ok and state["Result"] in ("", "success")
                    else:
                        # Restored inventory: oneshots must be quiet (not failed).
                        # Trust Result after reset-failed; stale ExecMainStatus must not
                        # permanently block post-deploy stability / business closure.
                        ok = (state["LoadState"] == "loaded" and state["Result"] in ("", "success")
                              and state["ActiveState"] in ("active", "inactive", "activating"))
                        if ok and state["ActiveState"] in ("active", "activating"):
                            ok = state["ExecMainStatus"] == "0"
                counter = state.get("NRestarts", "")
                restarts = int(counter) if counter.isdigit() else None
                units.append({"unit": unit, "ok": bool(ok), "active_state": state["ActiveState"],
                              "result": state["Result"], "restart_count": restarts})
            processes = [_process_status(state, commit) for state in states if state["Id"] in _runtime_units()]
            result["units"] = units
            result["capture"] = {"ok": all(p["ok"] for p in processes)
                and len({p["capture_policy_digest"] for p in processes}) == 1,
                "processes": processes, "credential_binding_valid": _credential_binding(states)}
            result["capture"]["ok"] = result["capture"]["ok"] and result["capture"]["credential_binding_valid"]
            if not all(unit["ok"] for unit in units):
                result["blocked_reasons"].append("allowlisted_unit_not_ready")
            if result["capture"]["ok"] is not True:
                result["blocked_reasons"].append("capture_policy_not_ready")
        except Exception as exc:
            result["units"] = []
            result["capture"] = {"ok": False, "error_type": type(exc).__name__}
            result["blocked_reasons"].append("unit_or_capture_observation_unavailable")
    result["ok"] = not result["blocked_reasons"]
    result["sample_digest"] = _digest(result)
    return result


def assess_controlled_stability(samples: list[dict[str, Any]], *, expected_commit: str,
                                minimum_interval_seconds: int = 60,
                                expected_version: str | None = None,
                                now: datetime | str | None = None,
                                expected_runtime_units: list[str] | tuple[str, ...] | None = None,
                                expected_topology: dict[str, Any] | None = None) -> dict[str, Any]:
    """Assess a current window, or reproduce a signed report at its assessed_at.

    Supplying ``now`` is for deterministic structural verification, not proof
    that a historical observation describes the current running release.
    """
    # Native assessment freezes the protected inventory. A signed historical
    # report can explicitly replay that inventory without host I/O; this is
    # structural consistency only, never new authority or a current measurement.
    topology = None
    topology_mode = isinstance(samples, list) and any(isinstance(s, dict)
        and s.get('schema') == TOPOLOGY_SAMPLE_SCHEMA for s in samples)
    if topology_mode:
        from eimemory.contracts.hermes_topology import load_verified_topology, validate_snapshot
        topology = validate_snapshot(load_verified_topology() if expected_topology is None else expected_topology)
        runtime_units = tuple(topology['runtime_units'])
        if expected_runtime_units is not None and tuple(expected_runtime_units) != runtime_units:
            raise ValueError('stability_runtime_inventory_invalid')
        from eimemory.evaluation.hermes_profile_coverage import required_capture_scopes
        required_scopes = required_capture_scopes(topology)
        required_scope_digest = _digest(required_scopes)
    else:
        # Archived v1 evidence retains its complete original five-owner and
        # six-capture contract. A new two-owner sample cannot masquerade as v1.
        runtime_units = tuple(_runtime_units()) if expected_runtime_units is None else expected_runtime_units
    if (not isinstance(runtime_units, (list, tuple)) or not (2 if topology_mode else 5) <= len(runtime_units) <= 66
            or any(not isinstance(unit, str) for unit in runtime_units)
            or len(set(runtime_units)) != len(runtime_units)
            or tuple(runtime_units[:2]) != RUNTIME_UNITS[:2]
            or any(re.fullmatch(r'hermes-gateway-[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}\.service', unit) is None
                   for unit in runtime_units[2:])):
        raise ValueError('stability_runtime_inventory_invalid')
    runtime_units = tuple(runtime_units)
    observed_units = runtime_units + TIMER_UNITS + JOB_UNITS
    commit = _commit(expected_commit)
    if expected_version is None:
        from eimemory.version import __version__
        expected_version = __version__
    trusted_version = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", expected_version)
    if trusted_version is None:
        raise ValueError("expected_release_version_required")
    assessed = datetime.now(timezone.utc) if now is None else (
        datetime.fromisoformat(now.replace("Z", "+00:00")) if isinstance(now, str) else now)
    if not isinstance(assessed, datetime) or assessed.tzinfo is None:
        raise ValueError("assessment_time_must_be_timezone_bound")
    assessed = assessed.astimezone(timezone.utc)
    if type(minimum_interval_seconds) is not int or not 60 <= minimum_interval_seconds <= 3600:
        raise ValueError("minimum_interval_must_be_at_least_60_seconds")
    reasons = []
    interval = 0.0
    valid = isinstance(samples, list) and 2 <= len(samples) <= 10 and all(isinstance(s, dict) for s in samples)
    if not valid:
        reasons.append("at_least_two_bounded_native_samples_required")
    else:
        try:
            for sample in samples:
                body = {k: v for k, v in sample.items() if k != "sample_digest"}
                if sample.get("sample_digest") != _digest(body):
                    reasons.append("sample_integrity_mismatch")
                if (sample.get("schema") != (TOPOLOGY_SAMPLE_SCHEMA if topology_mode else SAMPLE_SCHEMA) or sample.get("expected_commit") != commit
                    or sample.get("collector_origin") != "native_readonly_host_probe"
                    or sample.get("read_only") is not True or sample.get("natural_certified") is not False
                    or sample.get("ok") is not True or sample.get("blocked_reasons") != []):
                    reasons.append("sample_not_current_or_healthy")
                if topology_mode and sample.get('topology') != topology:
                    reasons.append('sample_topology_changed_or_unverified')
                if topology_mode:
                    from eimemory.ops.hermes_native_topology import validate_native_profile_topology
                    try:
                        native_profiles = validate_native_profile_topology(sample.get('native_profile_topology'),
                            topology, expected_commit=commit)
                    except (ValueError, TypeError, KeyError):
                        native_profiles = None
                        reasons.append('native_profile_topology_unverified')
                if any(sample.get(k, {}).get("ok") is not True for k in ("health", "database", "journal", "capture")):
                    reasons.append("sample_observation_incomplete")
                native_pg = sample.get("postgres_snapshot", {})
                if (native_pg.get("ok") is not True or native_pg.get("measurement_origin") != "native_readonly_repository_snapshot"
                        or any(native_pg.get(k) is not True for k in ("ready", "authority_unchanged", "fingerprints_match", "revision_matches"))):
                    reasons.append("native_postgres_unverified")
                health, database, journal, capture = (sample.get(k, {}) for k in ("health", "database", "journal", "capture"))
                # Old saved releases retain their historical structural model.
                # Every current collector always emits the new model and proof.
                legacy = (tuple(map(int, trusted_version.groups())) < (1, 14, 146)
                          and health.get("version") == expected_version
                          and "postgres_health_model" not in health and "postgres_readiness" not in health)
                if health.get("version") != expected_version:
                    reasons.append("sample_release_version_mismatch")
                if not legacy and not _current_pg_agrees(health, native_pg, at=sample.get("observed_at")):
                    reasons.append("sample_current_postgres_readiness_unverified")
                if any(health.get(k) is not True for k in ("identity_ok", "postgres_fresh", "migration_complete")):
                    reasons.append("sample_health_unverified")
                if (any(database.get(k) is not True for k in ("read_smoke_ok", "owner_read_write_access", "same_database_inode"))
                        or database.get("write_probe_performed") is not False
                        or database.get("integrity_basis") != "verified_backup_snapshot"
                        or database.get("current_full_integrity_scan_performed") is not False
                        or database.get("backup_integrity", {}).get("ok") is not True
                        or any(database["backup_integrity"].get(k) is not True for k in
                            ("schema_matches_current_database", "native_sqlite_integrity_check_ok", "native_logical_export_verified", "published_set"))
                        or not database.get("ownership") or any(o.get("ok") is not True for o in database["ownership"])):
                    reasons.append("sample_database_unverified")
                errors = journal.get("observed_error_counts", {})
                if (journal.get("observation_scope") not in {
                        "allowlisted_user_unit_journal", "registered_manager_all_invocations_journal"}
                        or journal.get("window_seconds") != 600
                        or set(errors) != {"crash", "oom", "database_read_write_failure"}
                        or any(type(n) is not int or n != 0 for n in errors.values())):
                    reasons.append("sample_error_observation_unverified")
                processes = capture.get("processes", [])
                if (capture.get("credential_binding_valid") is not True
                        or len(processes) != len(runtime_units) or {p.get("unit") for p in processes} != set(runtime_units)
                        or len({p.get("capture_policy_digest") for p in processes}) != 1
                        or any(p.get("ok") is not True or p.get("capture_enabled") is not True
                            or p.get("capture_policy_valid") is not True
                            or (p.get("capture_scope_count", 0) < len(required_scopes) if topology_mode
                                else p.get("capture_scope_count") != 6)
                            or type(p.get("capture_scope_count")) is not int
                            or p.get("recall_budget_seconds") != 8 or type(p.get("recall_budget_seconds")) is not int
                            or p.get("hermes_attestation_ready") is not True or p.get("runtime_commit_matches") is not True
                            or not isinstance(p.get("capture_policy_digest"), str)
                            or _HEX64.fullmatch(p["capture_policy_digest"]) is None for p in processes)):
                    reasons.append("sample_capture_unverified")
                if topology_mode and any(
                        p.get('profile_capture_coverage') != {
                            'ok': True, 'required_scope_count': len(required_scopes),
                            'missing_scope_count': 0, 'required_scope_digest': required_scope_digest,
                            'topology_digest': topology['topology_digest']} for p in processes):
                    reasons.append('sample_profile_capture_unverified')
                if topology_mode and native_profiles is not None:
                    owner = native_profiles['owner']
                    process = next((p for p in processes if p.get('unit') == 'hermes-gateway.service'), {})
                    if any(owner[key] != process.get(key) for key in ('pid', 'start_ticks', 'invocation_id')):
                        reasons.append('native_profile_owner_capture_mismatch')
                units = sample.get("units", [])
                if len(units) != len(observed_units) or {u.get("unit") for u in units} != set(observed_units) or any(u.get("ok") is not True for u in units):
                    reasons.append("sample_unit_inventory_incomplete")
            if len({sample.get("boot_id") for sample in samples}) != 1 or not samples[0].get("boot_id"):
                reasons.append("host_boot_changed")
            if len({s.get("database", {}).get("database_identity") for s in samples}) != 1:
                reasons.append("database_identity_changed")
            if topology_mode and len({s.get('native_profile_topology', {}).get('stable_digest') for s in samples}) != 1:
                reasons.append('native_profile_topology_changed')
            if len({p.get("capture_policy_digest") for s in samples for p in s.get("capture", {}).get("processes", [])}) != 1:
                reasons.append("capture_policy_changed")
            starts = [s["monotonic_seconds"] for s in samples]
            walls = [datetime.fromisoformat(s["observed_at"]) for s in samples]
            if (any(type(v) not in (int, float) or not math.isfinite(v) for v in starts)
                    or any(t.tzinfo is None for t in walls)
                    or any(b <= a for a, b in zip(starts, starts[1:]))):
                raise ValueError("sample_time_invalid")
            interval = min(starts[-1] - starts[0], (walls[-1] - walls[0]).total_seconds())
            if interval < minimum_interval_seconds:
                reasons.append("observation_interval_incomplete")
            latest_age = (assessed - walls[-1]).total_seconds()
            if not -5 <= latest_age <= 120 or interval > 3600:
                reasons.append("observations_not_fresh")
            for unit in runtime_units:
                counts = [next(u["restart_count"] for u in s["units"] if u["unit"] == unit) for s in samples]
                if any(type(n) is not int or n < 0 for n in counts) or len(set(counts)) != 1:
                    reasons.append("runtime_restarted_or_counter_unavailable")
                identities = [next(p for p in s["capture"]["processes"] if p["unit"] == unit) for s in samples]
                if (any(type(p.get("pid")) is not int or p["pid"] <= 0 or
                        type(p.get("start_ticks")) is not int or p["start_ticks"] <= 0 for p in identities)
                        or len({(p["pid"], p["start_ticks"]) for p in identities}) != 1):
                    reasons.append("runtime_process_changed")
        except (AttributeError, IndexError, KeyError, TypeError, ValueError, OverflowError, StopIteration):
            reasons.append("sample_contract_invalid")
    reasons = sorted(set(reasons))
    result = {"schema": TOPOLOGY_SCHEMA if topology_mode else SCHEMA,
            "ok": not reasons, "status": "observed_healthy" if not reasons else "blocked",
            "expected_commit": commit, "assessed_at": assessed.isoformat(),
            "runtime_units": list(runtime_units),
            "sample_count": len(samples) if isinstance(samples, list) else 0,
            "interval_seconds": max(0.0, round(interval, 3)), "minimum_interval_seconds": minimum_interval_seconds,
            "observation_scope": "bounded_runtime_window", "short_window_only": True,
            "long_term_stability_certified": False, "natural_certified": False,
            "blocked_reasons": reasons, "samples": samples if valid else []}
    if topology_mode:
        result['topology'] = topology
    return result


__all__ = ["collect_controlled_endpoint_identity", "collect_controlled_stability_sample", "assess_controlled_stability", "reset_failed_allowlisted_job_units", "allowlisted_job_units_restored_ready"]


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Read-only, two-point operational stability observation")
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--database-path", required=True)
    parser.add_argument("--interval-seconds", type=int, default=60)
    args = parser.parse_args(argv)
    _commit(args.expected_commit)
    if not 60 <= args.interval_seconds <= 3600:
        parser.error("interval must be between 60 and 3600 seconds")
    first = collect_controlled_stability_sample(expected_commit=args.expected_commit, database_path=args.database_path)
    deadline = first["monotonic_seconds"] + args.interval_seconds
    while time.monotonic() < deadline:
        time.sleep(max(0.0, min(1.0, deadline - time.monotonic())))
    second = collect_controlled_stability_sample(expected_commit=args.expected_commit, database_path=args.database_path)
    report = assess_controlled_stability([first, second], expected_commit=args.expected_commit,
                                         minimum_interval_seconds=args.interval_seconds)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
