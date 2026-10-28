from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
import shutil
from types import SimpleNamespace

import pytest

from eimemory.ops import controlled_stability as probe
from eimemory.version import __version__


FIXTURE_RUNTIME_UNITS = ('eimemory-rpc.service','hermes-gateway.service',
    'hermes-gateway-hongtai.service','hermes-gateway-hongxin.service',
    'hermes-gateway-xiaomage.service')


@contextmanager
def stability_fixture_contract():
    # Reusable factories can be called outside this module's pytest fixture
    # scope. Freeze the synthetic host contract only for that local operation.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(probe, 'RUNTIME_UNITS', FIXTURE_RUNTIME_UNITS)
        patch.setattr(probe, 'UNITS', FIXTURE_RUNTIME_UNITS + probe.TIMER_UNITS + probe.JOB_UNITS)
        patch.setattr(probe, '_runtime_units', lambda: FIXTURE_RUNTIME_UNITS)
        patch.setattr(probe, '_required_agents', lambda: frozenset(
            {'hongrui','hongtai','hongxin','xiaomage'}))
        yield


@pytest.fixture(autouse=True)
def configured_team_observation_contract(monkeypatch):
    # Share pytest's patch stack with the test. Separate nested patch stacks
    # restore in the wrong order when a test overrides this synthetic host.
    monkeypatch.setattr(probe, 'RUNTIME_UNITS', FIXTURE_RUNTIME_UNITS)
    monkeypatch.setattr(probe, 'UNITS', FIXTURE_RUNTIME_UNITS + probe.TIMER_UNITS + probe.JOB_UNITS)
    monkeypatch.setattr(probe, '_runtime_units', lambda: FIXTURE_RUNTIME_UNITS)
    monkeypatch.setattr(probe, '_required_agents', lambda: frozenset(
        {'hongrui','hongtai','hongxin','xiaomage'}))


COMMIT = "b" * 40


def seal(sample):
    sample["sample_digest"] = probe._digest({k: v for k, v in sample.items() if k != "sample_digest"})
    return sample


def readiness(stamp=None, **overrides):
    from eimemory.retrieval.readiness_contract import FLAGS, SCHEMA, ORIGIN
    return {**dict.fromkeys(FLAGS, True), "schema": SCHEMA, "measurement_origin": ORIGIN,
        "circuit": "closed", "last_error": "", "error": "", "lag_seconds": 0.0,
        "watermark": "watermark-current", "authority_revision": "123",
        "observed_at": stamp or datetime.now(timezone.utc).isoformat(), **overrides}


def samples():
    now = datetime.now(timezone.utc)
    result = []
    for offset in (-61, 0):
        result.append(seal({"schema": probe.SAMPLE_SCHEMA, "expected_commit": COMMIT,
            "observed_at": (now + timedelta(seconds=offset)).isoformat(),
            "monotonic_seconds": 1000 + offset, "boot_id": "b" * 36,
            "collector_origin": "native_readonly_host_probe", "read_only": True,
            "natural_certified": False, "ok": True, "blocked_reasons": [],
            "health": {"ok": True, "identity_ok": True, "postgres_fresh": True, "migration_complete": True,
                "package_tree_digest": "f" * 64, "version": __version__,
                "postgres_health_model": "native_readonly_vector_query.v1",
                "postgres_readiness": readiness((now + timedelta(seconds=offset)).isoformat())},
            "postgres_snapshot": {"ok": True, "measurement_origin": "native_readonly_repository_snapshot",
                "ready": True, "authority_unchanged": True, "fingerprints_match": True, "revision_matches": True,
                "watermark": "watermark-current", "authority_revision": "123"},
            "database": {"ok": True, "read_smoke_ok": True, "owner_read_write_access": True,
                "same_database_inode": True, "write_probe_performed": False, "ownership": [{"ok": True}],
                "integrity_basis": "verified_backup_snapshot", "current_full_integrity_scan_performed": False,
                "backup_integrity": {"ok": True, "schema_matches_current_database": True,
                    "native_sqlite_integrity_check_ok": True, "native_logical_export_verified": True, "published_set": True}},
            "journal": {"ok": True, "window_seconds": 600,
                "observation_scope": "allowlisted_user_unit_journal",
                "observed_error_counts": {"crash": 0, "oom": 0, "database_read_write_failure": 0}},
            "capture": {"ok": True, "credential_binding_valid": True, "processes": [{"unit": unit, "ok": True,
                "capture_enabled": True, "capture_policy_valid": True, "capture_scope_count": 6,
                "capture_policy_digest": "c" * 64, "recall_budget_seconds": 8,
                "hermes_attestation_ready": True, "runtime_commit_matches": True,
                "pid": 100 + n, "start_ticks": 2345} for n, unit in enumerate(FIXTURE_RUNTIME_UNITS)]},
            "units": [{"unit": unit, "ok": True, "restart_count": 0} for unit in (FIXTURE_RUNTIME_UNITS + probe.TIMER_UNITS + probe.JOB_UNITS)]}))
    return result


def assess(value):
    return probe.assess_controlled_stability(value, expected_commit=COMMIT)


def test_two_native_samples_certify_only_observed_short_window():
    report = assess(samples())
    assert report["ok"] and report["sample_count"] == 2
    assert report["interval_seconds"] == 61
    assert report["short_window_only"] is True
    assert report["long_term_stability_certified"] is False
    assert report["natural_certified"] is False


@pytest.mark.parametrize("count", [0, 1, 11])
def test_bounded_sample_count_required(count):
    assert not assess([deepcopy(samples()[0]) for _ in range(count)])["ok"]


@pytest.mark.parametrize("field,value", [("schema", "other"), ("expected_commit", "a" * 40),
    ("read_only", False), ("natural_certified", True), ("ok", "true"), ("collector_origin", "uploaded_json")])
def test_untrusted_or_wrong_release_sample_rejected(field, value):
    value_samples = samples(); value_samples[-1][field] = value; seal(value_samples[-1])
    assert not assess(value_samples)["ok"]


def test_tampered_sample_rejected():
    value = samples(); value[-1]["health"]["ok"] = False
    assert "sample_integrity_mismatch" in assess(value)["blocked_reasons"]


@pytest.mark.parametrize("field", ["health", "database", "journal", "capture"])
def test_unknown_observation_never_passes(field):
    value = samples(); value[-1][field] = {}; seal(value[-1])
    assert not assess(value)["ok"]


@pytest.mark.parametrize("difference", [0, 59.9])
def test_at_least_sixty_seconds_required(difference):
    value = samples()
    value[-1]["monotonic_seconds"] = value[0]["monotonic_seconds"] + difference; seal(value[-1])
    assert not assess(value)["ok"]


def test_reboot_rejected():
    value = samples(); value[-1]["boot_id"] = "a" * 36; seal(value[-1])
    assert "host_boot_changed" in assess(value)["blocked_reasons"]


@pytest.mark.parametrize("field", ["pid", "start_ticks"])
def test_manual_restart_even_with_zero_restart_counter_rejected(field):
    value = samples(); value[-1]["capture"]["processes"][0][field] += 1; seal(value[-1])
    assert "runtime_process_changed" in assess(value)["blocked_reasons"]


def test_restart_counter_increment_rejected():
    value = samples(); value[-1]["units"][0]["restart_count"] = 1; seal(value[-1])
    assert "runtime_restarted_or_counter_unavailable" in assess(value)["blocked_reasons"]


def test_stale_samples_rejected():
    value = samples()
    for sample in value:
        sample["observed_at"] = (datetime.fromisoformat(sample["observed_at"]) - timedelta(hours=1)).isoformat(); seal(sample)
    assert "observations_not_fresh" in assess(value)["blocked_reasons"]


def test_report_structural_recheck_is_deterministic_at_original_assessment_time():
    value = samples()
    assessed = datetime.now(timezone.utc) - timedelta(hours=1)
    for sample in value:
        sample["observed_at"] = (datetime.fromisoformat(sample["observed_at"]) - timedelta(hours=1)).isoformat()
        sample["health"]["postgres_readiness"]["observed_at"] = sample["observed_at"]
        seal(sample)
    first = probe.assess_controlled_stability(value, expected_commit=COMMIT, now=assessed)
    assert first["ok"]
    repeated = probe.assess_controlled_stability(value, expected_commit=COMMIT, now=first["assessed_at"])
    assert first == repeated
    assert not assess(value)["ok"]


def test_naive_assessment_timestamp_rejected():
    with pytest.raises(ValueError, match="timezone"):
        probe.assess_controlled_stability(samples(), expected_commit=COMMIT, now="2026-10-18T10:00:00")


@pytest.mark.parametrize("path,value", [("capture_scope_count", 5), ("capture_scope_count", True),
    ("capture_enabled", False), ("capture_policy_digest", ""), ("recall_budget_seconds", 3),
    ("hermes_attestation_ready", False), ("runtime_commit_matches", False)])
def test_capture_contract_must_fail(path, value):
    sample_values = samples(); sample_values[-1]["capture"]["processes"][0][path] = value; seal(sample_values[-1])
    assert not assess(sample_values)["ok"]


@pytest.mark.parametrize("code", ["crash", "oom", "database_read_write_failure"])
def test_observed_failure_counter_blocks_even_with_ok_claim(code):
    value = samples(); value[-1]["journal"]["observed_error_counts"][code] = 1; seal(value[-1])
    assert "sample_error_observation_unverified" in assess(value)["blocked_reasons"]


def test_missing_critical_timer_blocks():
    value = samples(); value[-1]["units"].pop(); seal(value[-1])
    assert not assess(value)["ok"]


def test_health_cached_pg_unavailable_blocks_even_with_ok_claim():
    value = samples(); value[-1]["health"]["postgres_fresh"] = False; seal(value[-1])
    assert "sample_health_unverified" in assess(value)["blocked_reasons"]


def test_no_configurable_interval_below_minimum():
    with pytest.raises(ValueError):
        probe.assess_controlled_stability(samples(), expected_commit=COMMIT, minimum_interval_seconds=1)


def test_full_commit_required():
    with pytest.raises(ValueError):
        probe.collect_controlled_stability_sample(expected_commit="b" * 7, database_path="unused")


@pytest.mark.skipif(os.name != "posix", reason="DB owner contract is Linux")
def test_native_database_read_smoke_is_readonly_and_keeps_business_private(tmp_path):
    state = tmp_path / "state"; state.mkdir()
    path = state / "eimemory.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE records(value)")
        conn.execute("INSERT INTO records VALUES ('private-dont-export')")
    path.chmod(0o600)
    published_backup(path)
    before = path.read_bytes()
    result = probe._database(path)
    assert result["ok"] and result["write_probe_performed"] is False
    assert path.read_bytes() == before
    assert "private-dont-export" not in json.dumps(result)
    assert result["integrity_basis"] == "verified_backup_snapshot"
    assert result["current_full_integrity_scan_performed"] is False


@pytest.mark.skipif(os.name != "posix", reason="DB owner contract is Linux")
def test_database_owner_mismatch_never_opens_database(tmp_path, monkeypatch):
    path = tmp_path / "eimemory.sqlite"; path.write_bytes(b"secret"); path.chmod(0o600)
    monkeypatch.setattr(probe.os, "geteuid", lambda: path.stat().st_uid + 1)
    monkeypatch.setattr(probe.sqlite3, "connect", lambda *a, **k: pytest.fail("must not open"))
    with pytest.raises(ValueError, match="owner_required"):
        probe._database(path)


@pytest.mark.skipif(os.name != "posix", reason="DB owner contract is Linux")
def test_unsafe_sidecar_rejected(tmp_path):
    path = tmp_path / "eimemory.sqlite"
    with sqlite3.connect(path): pass
    path.chmod(0o600)
    sidecar = tmp_path / "eimemory.sqlite-shm"; sidecar.write_bytes(b""); sidecar.chmod(0o666)
    with pytest.raises(ValueError, match="sidecar"):
        probe._database(path)


@pytest.fixture(autouse=True)
def _registered_manager_journal_boundary(monkeypatch, configured_team_observation_contract):
    import importlib.util
    from eimemory.contracts import runtime_manager as contract
    source=Path(__file__).resolve().parents[1]/"deploy/runtime_manager_bridge.py"
    spec=importlib.util.spec_from_file_location("tested_root_manager_bridge",source)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    bridge=module.Bridge(contract)
    registry={"runtime_uid":1001,"units":{unit:{"installed":True,"manager":"user" if unit.startswith("hermes-") else "system"}
        for unit in probe.RUNTIME_UNITS+probe.JOB_UNITS}}
    # The existing callback fixtures exercise the same native reader contract.
    # Root's concrete reader is separately exercised by runtime-manager tests.
    bridge.journal_output=lambda args:probe._journal_output(args)
    monkeypatch.setattr(probe,"_journal",lambda:bridge.error_window(registry))


def test_journal_exports_only_counts_not_messages(monkeypatch):
    monkeypatch.setattr(probe, "_journal_output", lambda args: '\n'.join(json.dumps({"MESSAGE": value}) for value in
        ["secretbusiness sqlite3.OperationalError: database is locked", "Failed with result oom-kill", "segfault"]))
    report = probe._journal()
    assert report["observed_error_counts"] == {"crash": 1, "oom": 1, "database_read_write_failure": 1}
    assert not report["ok"]
    assert "secretbusiness" not in json.dumps(report)


def test_truncated_journal_is_unknown_not_zero(monkeypatch):
    monkeypatch.setattr(probe, "_journal_output", lambda args: '\n'.join('{}' for _ in range(2001)))
    with pytest.raises(ValueError, match="truncated"):
        probe._journal()


def test_journal_native_filter_and_classifier_share_patterns_and_keep_old_errors(monkeypatch):
    import re

    entries = [dict(MESSAGE="private OLD DATABASE IS LOCKED", _SYSTEMD_USER_UNIT=probe.RUNTIME_UNITS[0])]
    entries += [dict(MESSAGE="normal private nightly output " + "x" * 1500) for _ in range(3256)]
    commands = []

    def native(args, **kwargs):
        commands.append(args)
        pattern = next((arg.removeprefix("--grep=") for arg in args if arg.startswith("--grep=")), None)
        selected = entries
        if pattern is not None:
            assert pattern == "|".join("(?:" + value + ")" for _name, value in probe._JOURNAL_ERROR_PATTERNS)
            assert "--case-sensitive=no" in args
            selected = [entry for entry in entries if re.search(pattern, entry["MESSAGE"], re.IGNORECASE)]
        count = int(next(arg.removeprefix("--lines=") for arg in args if arg.startswith("--lines=")))
        return SimpleNamespace(returncode=0, stderr="", stdout="\n".join(json.dumps(entry) for entry in selected[-count:]))

    monkeypatch.setattr(probe.subprocess, "run", native)
    report = probe._journal()
    assert not report["ok"] and report["entries_observed"] == 1
    assert report["observed_error_counts"] == {"crash": 0, "oom": 0, "database_read_write_failure": 1}
    assert "private" not in json.dumps(report)
    assert len(commands) == 2
    for args in commands:
        assert all(("_SYSTEMD_USER_UNIT=" if unit.startswith("hermes-") else "_SYSTEMD_UNIT=") + unit in args for unit in probe.RUNTIME_UNITS + probe.JOB_UNITS)
        assert "--user" not in args and "+" in args and "_UID=1001" in args
        assert "--quiet" in args and "--all" in args
    bounds = [[arg for arg in args if arg.startswith(("--since=", "--until="))] for args in commands]
    assert bounds[0] == bounds[1]
    assert float(bounds[0][1].removeprefix("--until=@")) - float(bounds[0][0].removeprefix("--since=@")) == 600
    assert "--lines=1" in commands[0] and "--lines=2001" in commands[1]


def test_journal_exact_native_no_match_requires_successful_readability_preflight(monkeypatch):
    results = iter([SimpleNamespace(returncode=0, stdout=json.dumps({"MESSAGE": "normal"}), stderr=""),
                    SimpleNamespace(returncode=1, stdout="", stderr="")])
    monkeypatch.setattr(probe.subprocess, "run", lambda *args, **kwargs: next(results))
    report = probe._journal()
    assert report["ok"] and report["entries_observed"] == 0
    assert set(report["observed_error_counts"].values()) == {0}


@pytest.mark.parametrize("returncode,stdout,stderr", [(1, "", "permission failure"), (1, "{}", ""),
    (2, "", ""), (-15, "", ""), (0, "", "partial observation")])
def test_journal_command_failure_is_never_an_empty_match(monkeypatch, returncode, stdout, stderr):
    results = iter([SimpleNamespace(returncode=0, stdout="", stderr=""),
                    SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)])
    monkeypatch.setattr(probe.subprocess, "run", lambda *args, **kwargs: next(results))
    with pytest.raises(RuntimeError, match="journal_command_unavailable"):
        probe._journal()


@pytest.mark.parametrize("returncode,stdout,stderr", [(1, "", ""), (0, "", "permission failure"),
    (0, "not json", ""), (0, "[]", ""), (0, "{}\n{}", "")])
def test_journal_preflight_failure_blocks_before_grep(monkeypatch, returncode, stdout, stderr):
    calls = []
    def native(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
    monkeypatch.setattr(probe.subprocess, "run", native)
    with pytest.raises((RuntimeError, ValueError)):
        probe._journal()
    assert len(calls) == 1


@pytest.mark.parametrize("phase", ["preflight", "filtered"])
def test_journal_output_limit_is_preserved_in_both_phases(monkeypatch, phase):
    calls = []
    def native(args, **kwargs):
        calls.append(args)
        oversized = phase == "preflight" or len(calls) == 2
        return SimpleNamespace(returncode=0, stdout="x" * (2 * 1024 * 1024 + 1) if oversized else "", stderr="")
    monkeypatch.setattr(probe.subprocess, "run", native)
    with pytest.raises(ValueError, match="readonly_command_output_limit"):
        probe._journal()


def test_journal_two_phases_share_twelve_second_deadline(monkeypatch):
    clock = {"now": 100.0}
    timeouts = []
    monkeypatch.setattr(probe.time, "monotonic", lambda: clock["now"])
    def native(args, **kwargs):
        timeouts.append(kwargs["timeout"])
        clock["now"] += 7 if len(timeouts) == 1 else 3
        return SimpleNamespace(returncode=0 if len(timeouts) == 1 else 1, stdout="", stderr="")
    monkeypatch.setattr(probe.subprocess, "run", native)
    assert probe._journal()["ok"]
    assert timeouts == [12, 5]


def test_journal_preflight_using_all_time_does_not_grant_more_time_to_grep(monkeypatch):
    clock = {"now": 100.0}
    calls = []
    monkeypatch.setattr(probe.time, "monotonic", lambda: clock["now"])
    def native(args, **kwargs):
        calls.append(args)
        clock["now"] += 12.01
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(probe.subprocess, "run", native)
    with pytest.raises(TimeoutError, match="readonly_command_timeout"):
        probe._journal()
    assert len(calls) == 1


def test_journal_timeout_is_not_an_empty_match(monkeypatch):
    def native(args, **kwargs):
        raise probe.subprocess.TimeoutExpired(args, kwargs["timeout"])
    monkeypatch.setattr(probe.subprocess, "run", native)
    with pytest.raises(probe.subprocess.TimeoutExpired):
        probe._journal()


@pytest.mark.parametrize("entry", [None, [], "segfault", {}, {"message": "segfault"},
    {"MESSAGE": None}, {"MESSAGE": 42}, {"MESSAGE": ["segfault"]},
    {"MESSAGE": list(b"database is locked")}, {"MESSAGE": {"value": "segfault"}}, {"MESSAGE": "normal"}])
def test_journal_unknown_or_array_messages_cannot_be_counted_as_zero(monkeypatch, entry):
    monkeypatch.setattr(probe, "_journal_output", lambda args: json.dumps(entry))
    with pytest.raises(ValueError, match="journal_(message|filter)_contract_invalid"):
        probe._journal()


def test_journal_oversized_matched_entry_is_not_accepted(monkeypatch):
    monkeypatch.setattr(probe, "_journal_output", lambda args: json.dumps({"MESSAGE": "segfault " + "x" * 262144}))
    with pytest.raises(ValueError):
        probe._journal()


def test_current_native_pg_snapshot_is_independently_required():
    value = samples(); value[-1]["postgres_snapshot"] = {"ok": True}; seal(value[-1])
    assert "native_postgres_unverified" in assess(value)["blocked_reasons"]


def test_gateway_producer_must_be_authorized_by_current_rpc_registry(monkeypatch):
    from eimemory.adapters.runtime import host_auth
    states = [{"Id": unit} for unit in probe.RUNTIME_UNITS]
    monkeypatch.setattr(probe, "_process_environment", lambda state: (1, None, {"unit": state["Id"]}))
    monkeypatch.setattr(host_auth, "attestation_tokens_from_private_file", lambda **kw: {"authorized-secret": "hermes"})
    monkeypatch.setattr(host_auth, "producer_token_from_private_file", lambda *args, **kw: "different-strong-secret")
    assert probe._credential_binding(states) is False
    monkeypatch.setattr(host_auth, "producer_token_from_private_file", lambda *args, **kw: "authorized-secret")
    assert probe._credential_binding(states) is True


def test_no_credential_binding_does_not_certify_capture():
    value = samples(); value[-1]["capture"]["credential_binding_valid"] = False; seal(value[-1])
    assert not assess(value)["ok"]


def test_capture_policy_changes_between_observations_rejected():
    value = samples()
    for process in value[-1]["capture"]["processes"]:
        process["capture_policy_digest"] = "a" * 64
    seal(value[-1])
    assert "capture_policy_changed" in assess(value)["blocked_reasons"]


@pytest.mark.parametrize("field,value", [("health", "unexpected"), ("capture", []), ("units", [None])])
def test_malformed_uploaded_shapes_fail_closed(field, value):
    observations = samples(); observations[-1][field] = value; seal(observations[-1])
    assert not assess(observations)["ok"]


def capture_env():
    entries = [{"tenant_id": "tenant", "agent_id": agent, "workspace_id": "workspace",
                "user_id": "owner", "channel": "hermes", "source_id": "hermes"}
               for agent in ("hongrui", "hongtai", "hongxin", "xiaomage", "default", "hongtu")]
    return {"EIMEMORY_CAPTURE_QUERY_SCOPES": json.dumps(entries), "EIMEMORY_CAPTURE_ORIGINAL_QUERY": "1",
            "EIMEMORY_RUNTIME_COMMIT": COMMIT, "EIMEMORY_RECALL_BUDGET_SECONDS": "8"}


def test_native_capture_grants_check_returns_no_tokens(monkeypatch):
    from eimemory.adapters.runtime import host_auth
    # The synthetic six-scope policy exercises the legacy inventory contract.
    monkeypatch.setattr(probe, "_topology", lambda: None)
    monkeypatch.setattr(host_auth, "attestation_tokens_from_private_file", lambda **kw: {"secret-token": "hermes"})
    report = probe._capture_environment_status(capture_env(), probe.RUNTIME_UNITS[0], COMMIT)
    assert report["ok"] and report["capture_scope_count"] == 6
    assert "secret-token" not in json.dumps(report)


@pytest.mark.parametrize("change", ["missing_agent", "wildcard", "duplicate", "disabled", "budget", "no_grant"])
def test_real_capture_config_contract_rejects_broken_policy(monkeypatch, change):
    from eimemory.adapters.runtime import host_auth
    monkeypatch.setattr(probe, "_topology", lambda: None)
    monkeypatch.setattr(host_auth, "attestation_tokens_from_private_file", lambda **kw: {} if change == "no_grant" else {"secret": "hermes"})
    env = capture_env(); policy = json.loads(env["EIMEMORY_CAPTURE_QUERY_SCOPES"])
    if change == "missing_agent": policy[0]["agent_id"] = "foreign"
    if change == "wildcard": policy[0]["user_id"] = "*"
    if change == "duplicate": policy[-1] = policy[0]
    if change == "disabled": env["EIMEMORY_CAPTURE_ORIGINAL_QUERY"] = "0"
    if change == "budget": env["EIMEMORY_RECALL_BUDGET_SECONDS"] = "9"
    env["EIMEMORY_CAPTURE_QUERY_SCOPES"] = json.dumps(policy)
    assert not probe._capture_environment_status(env, probe.RUNTIME_UNITS[0], COMMIT)["ok"]


@pytest.mark.parametrize("memory_only", [True, False])
def test_native_pg_child_normalizes_real_sqlite_time_before_lag_comparison(tmp_path, monkeypatch, capsys, memory_only):
    from eimemory.retrieval import postgres_cli, postgres_vector
    path = tmp_path / "eimemory.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE records(updated_at,storage_key,kind,status)")
        conn.execute("INSERT INTO records VALUES ('2026-10-18T11:00:00+00:00','key','memory','active')")
        conn.execute("CREATE TABLE memory_vector_revision(singleton,revision)")
        conn.execute("INSERT INTO memory_vector_revision VALUES (1,12)")
        conn.execute("CREATE TABLE vector_sync_revision(singleton,revision)")
        conn.execute("INSERT INTO vector_sync_revision VALUES (1,12)")
    config = postgres_vector.PostgresVectorConfig(enabled=True, dsn="test-only", projection_memory_only=memory_only,
        embedding_provider=SimpleNamespace())
    state = postgres_vector.IndexState(ready=True, watermark="test", authority_revision="12",
        authoritative_updated_at=postgres_vector._canonical_timestamp("2026-10-18T11:00:00Z"), authoritative_storage_key="key",
        embedding_fingerprint="f" * 64, projection_digest_schema=postgres_vector.PROJECTION_DIGEST_SCHEMA,
        projection_fingerprint=postgres_vector.projection_fingerprint(config))
    monkeypatch.setattr(postgres_cli, "config_from_env", lambda: config)
    monkeypatch.setattr(postgres_vector, "PostgresCandidateRepository", lambda config: SimpleNamespace(read_index_state=lambda: state))
    monkeypatch.setattr(postgres_vector, "embedding_provider_fingerprint", lambda *args: "f" * 64)
    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "psycopg.rows", SimpleNamespace(dict_row=None))
    monkeypatch.setattr(sys, "argv", ["probe", str(path), COMMIT])
    monkeypatch.setattr(sys, "path", list(sys.path))
    exec(compile(probe._PG_READ_SCRIPT, "<native_pg_probe>", "exec"), {"__name__": "test_pg_probe"})
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True and report["lag_seconds"] == 0, report


def published_backup(database, *, stamp=None):
    stamp = stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = database.parent.parent
    selected = root / "backups" / stamp
    (selected / "sqlite").mkdir(parents=True)
    selected.chmod(0o700)
    snapshot = selected / "sqlite" / database.name
    shutil.copy2(database, snapshot); snapshot.chmod(0o600)
    value = {"schema": "eimemory.backup_set.v1", "ok": True, "created_at": stamp, "root": str(root),
        "sqlite": [{"source": str(database), "file": "sqlite/" + database.name, "ok": True,
                    "integrity_check": "ok", "records_rows": 1}],
        "records": {"ok": True, "create_exit_code": 0, "verify_exit_code": 0, "record_count": 1, "sha256": "a" * 64},
        "config": {"ok": True}, "payload_archive": {"ok": True},
        "files": [{"path": "sqlite/" + database.name, "bytes": snapshot.stat().st_size, "sha256": "b" * 64}]}
    manifest = selected / "backup-set.json"; manifest.write_text(json.dumps(value)); manifest.chmod(0o600)
    return manifest, value, snapshot


def backup_database(tmp_path):
    state = tmp_path / "state"; state.mkdir()
    database = state / "eimemory.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE records(record_id TEXT)")
        connection.execute("INSERT INTO records VALUES('private-business')")
    database.chmod(0o600)
    return database


@pytest.mark.skipif(os.name != "posix", reason="protected native backup publication is Linux")
@pytest.mark.parametrize("defect", ["unknown_schema", "not_ok", "wrong_root", "failed_export", "empty_export",
    "wrong_database", "bad_integrity", "wrong_snapshot_size", "old", "future", "wrong_schema_version", "partial"])
def test_native_published_backup_contract_must_fail(tmp_path, defect):
    database = backup_database(tmp_path)
    stamp = None
    if defect == "old": stamp = (datetime.now(timezone.utc)-timedelta(days=2)).strftime("%Y%m%dT%H%M%SZ")
    if defect == "future": stamp = (datetime.now(timezone.utc)+timedelta(hours=1)).strftime("%Y%m%dT%H%M%SZ")
    manifest, value, snapshot = published_backup(database, stamp=stamp)
    if defect == "unknown_schema": value["schema"] = "unknown"
    if defect == "not_ok": value["ok"] = False
    if defect == "wrong_root": value["root"] = "/other/runtime"
    if defect == "failed_export": value["records"]["verify_exit_code"] = 1
    if defect == "empty_export": value["records"]["record_count"] = 0
    if defect == "wrong_database": value["sqlite"][0]["source"] = "/other/eimemory.sqlite"
    if defect == "bad_integrity": value["sqlite"][0]["integrity_check"] = "failed"
    if defect == "wrong_snapshot_size": value["files"][0]["bytes"] += 1
    if defect == "wrong_schema_version":
        with sqlite3.connect(snapshot) as conn: conn.execute("PRAGMA user_version=1")
    manifest.write_text(json.dumps(value)); manifest.chmod(0o600)
    if defect == "partial": manifest.parent.rename(manifest.parent.with_name(".tmp-" + manifest.parent.name))
    with pytest.raises(ValueError):
        probe._database(database)


@pytest.mark.skipif(os.name != "posix", reason="protected native backup publication is Linux")
def test_published_backup_without_manifest_is_not_accepted(tmp_path):
    database = backup_database(tmp_path)
    manifest, _value, _snapshot = published_backup(database)
    manifest.unlink()
    with pytest.raises(FileNotFoundError):
        probe._database(database)


def health_payload():
    from eimemory.version import __version__
    from eimemory.ei_bridge.protocol import EIMEMORY_RPC_CONTRACT_VERSION
    release = Path("/opt/eimemory/releases") / COMMIT
    return {"ok": True, "service": "eimemory-rpc", "version": __version__, "commit": COMMIT,
        "configured_runtime_commit": COMMIT, "contract_version": EIMEMORY_RPC_CONTRACT_VERSION,
        "checks": {"runtime_identity": True, "ready": True}, "store": {"migration_complete": True},
        "paths": {"release": str(release)}, "import_root": str(release / "eimemory"), "package_tree_digest": "f" * 64,
        "retrieval": {"candidate_source": {"enabled": True, "configured": True, "available": False,
            "index_verified": True, "query_valid": False, "circuit": "closed", "last_error": "", "lag_seconds": 0}}}


def test_cold_endpoint_can_prove_identity_but_full_health_remains_strict(monkeypatch):
    from eimemory import runtime_identity
    value = health_payload()
    monkeypatch.setattr(runtime_identity, "package_tree_digest", lambda path: "f" * 64)
    monkeypatch.setattr(probe, "_authenticated_health_payload", lambda: value)
    identity = probe.collect_controlled_endpoint_identity(expected_commit=COMMIT)
    assert identity["ok"] is True and identity["identity_only"] is True
    assert identity["retrieval_health_certified"] is False
    assert probe._health(COMMIT)["ok"] is False
    value["retrieval"]["candidate_source"].update(available=True, query_valid=True)
    assert probe._health(COMMIT)["ok"] is False
    value["retrieval"]["candidate_source"]["current_readiness"] = readiness()
    assert probe._health(COMMIT)["ok"] is True


@pytest.mark.parametrize("defect", ["commit", "configured_commit", "version", "service", "contract", "import", "path", "digest", "runtime_identity"])
def test_authenticated_endpoint_identity_is_exact_and_source_bound(monkeypatch, defect):
    from eimemory import runtime_identity
    value = health_payload()
    monkeypatch.setattr(runtime_identity, "package_tree_digest", lambda path: "f" * 64)
    if defect == "commit": value["commit"] = "a" * 40
    if defect == "configured_commit": value["configured_runtime_commit"] = "a" * 40
    if defect == "version": value["version"] = "unrelated-version"
    if defect == "service": value["service"] = "other-service"
    if defect == "contract": value["contract_version"] = "other-contract"
    if defect == "import": value["import_root"] = "/other/package"
    if defect == "path": value["paths"]["release"] = "/other/release"
    if defect == "digest": value["package_tree_digest"] = "e" * 64
    if defect == "runtime_identity": value["checks"]["runtime_identity"] = False
    monkeypatch.setattr(probe, "_authenticated_health_payload", lambda: value)
    assert not probe.collect_controlled_endpoint_identity(expected_commit=COMMIT)["ok"]


def test_identity_only_does_not_claim_readiness(monkeypatch):
    from eimemory import runtime_identity
    value = health_payload(); value["checks"]["ready"] = False; value["ok"] = False
    monkeypatch.setattr(runtime_identity, "package_tree_digest", lambda path: "f" * 64)
    monkeypatch.setattr(probe, "_authenticated_health_payload", lambda: value)
    assert probe.collect_controlled_endpoint_identity(expected_commit=COMMIT)["ok"]
    assert not probe._health(COMMIT)["ok"]


def test_unauthenticated_health_is_never_requested(monkeypatch):
    from eimemory.core import rpc_probe_auth
    monkeypatch.setattr(rpc_probe_auth, "rpc_probe_headers", lambda: {"Accept": "application/json"})
    monkeypatch.setattr(probe, "build_opener", lambda *args: pytest.fail("must not issue anonymous identity request"))
    with pytest.raises(ValueError, match="authenticated_health_required"):
        probe._authenticated_health_payload()


def test_health_identity_fetch_uses_only_fixed_authenticated_loopback(monkeypatch):
    from eimemory.core import rpc_probe_auth
    monkeypatch.setattr(rpc_probe_auth, "rpc_probe_headers", lambda: {"Authorization": "Bearer fixture-secret"})
    value = health_payload()
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, size):
            assert size == 65537
            return json.dumps(value).encode()
    class Opener:
        def open(self, request, timeout):
            assert request.full_url == "http://127.0.0.1:8091/health" and timeout == 8
            assert request.get_header("Authorization") == "Bearer fixture-secret"
            return Response()
    monkeypatch.setattr(probe, "build_opener", lambda *handlers: Opener())
    assert probe._authenticated_health_payload() == value


def test_standalone_cli_deadline_race_never_sleeps_negative(monkeypatch, capsys):
    values = samples(); observations = iter(values)
    monkeypatch.setattr(probe, "collect_controlled_stability_sample", lambda **kw: next(observations))
    times = iter([998.9, 999.1, 1000.0])
    monkeypatch.setattr(probe.time, "monotonic", lambda: next(times))
    sleeps = []
    monkeypatch.setattr(probe.time, "sleep", lambda duration: sleeps.append(duration))
    assert probe.main(["--expected-commit", COMMIT, "--database-path", "/fixture/eimemory.sqlite"]) == 0
    assert sleeps == [0.0]
    assert json.loads(capsys.readouterr().out)["ok"] is True


@pytest.mark.parametrize('message', ['', 'database is locked', 'Out of memory'])
def test_root_journal_collector_output_is_consumable_without_losing_error_gate(monkeypatch, message):
    from test_runtime_manager import standalone
    from eimemory.contracts import runtime_manager as contract
    bridge = standalone('runtime_manager_bridge').Bridge(contract)
    monkeypatch.setattr(bridge, 'journal_output', lambda _: json.dumps({'MESSAGE':message}) if message else '')
    report = bridge.error_window({'runtime_uid':1001,'units':{
        'eimemory-rpc.service':{'installed':True,'manager':'system'}}})
    value = samples()
    for sample in value:
        sample['journal'] = deepcopy(report)
        seal(sample)
    result = assess(value)
    assert result['ok'] is (not message)
    if message: assert 'sample_error_observation_unverified' in result['blocked_reasons']


def test_unknown_journal_scope_cannot_claim_a_verified_window():
    value = samples()
    value[-1]['journal']['observation_scope'] = 'unregistered_global_journal'
    seal(value[-1])
    assert 'sample_error_observation_unverified' in assess(value)['blocked_reasons']


def test_archived_inventory_replay_is_structural_and_performs_no_host_lookup(monkeypatch):
    report=assess(samples())
    monkeypatch.setattr(probe, '_runtime_units', lambda: pytest.fail('historical replay contacted host'))
    replayed=probe.assess_controlled_stability(report['samples'], expected_commit=COMMIT,
        expected_version=__version__, now=report['assessed_at'],
        expected_runtime_units=report['runtime_units'])
    assert replayed==report
    assert replayed['natural_certified'] is False
    assert replayed['long_term_stability_certified'] is False


@pytest.mark.parametrize('units', [[], list(FIXTURE_RUNTIME_UNITS[:4]),
    list(FIXTURE_RUNTIME_UNITS)+['arbitrary.service'],
    list(FIXTURE_RUNTIME_UNITS)+[FIXTURE_RUNTIME_UNITS[-1]]])
def test_archived_inventory_cannot_shrink_team_or_add_system_units(units):
    with pytest.raises(ValueError,match='runtime_inventory_invalid'):
        probe.assess_controlled_stability(samples(), expected_commit=COMMIT,
                                         expected_runtime_units=units)


def _timer_state(unit, *, active, result="success", unit_file="enabled"):
    return {"Id": unit, "LoadState": "loaded", "ActiveState": active, "Result": result,
            "UnitFileState": unit_file, "ExecMainStatus": "0", "NRestarts": "0",
            "MainPID": "0", "ControlPID": "0"}


def _job_state(unit, *, active, result="success", exec_status="0"):
    return {"Id": unit, "LoadState": "loaded", "ActiveState": active, "Result": result,
            "UnitFileState": "static", "ExecMainStatus": exec_status, "NRestarts": "0",
            "MainPID": "0", "ControlPID": "0"}


def _runtime_state(unit):
    return {"Id": unit, "LoadState": "loaded", "ActiveState": "active", "Result": "success",
            "UnitFileState": "enabled", "ExecMainStatus": "0", "NRestarts": "0",
            "MainPID": "1", "ControlPID": "0", "FragmentPath": "/lib/systemd/system/" + unit,
            "DropInPaths": "", "InvocationID": "a" * 32}


def test_background_held_observation_accepts_quiet_timers_and_failed_jobs(monkeypatch, tmp_path):
    """During admission hold, enabled timers stay inactive and oneshots may be failed."""
    path = tmp_path / "eimemory.sqlite"
    path.write_bytes(b"")
    states = [_runtime_state(unit) for unit in FIXTURE_RUNTIME_UNITS]
    states.extend(_timer_state(unit, active="inactive") for unit in probe.TIMER_UNITS)
    states.extend(_job_state(unit, active="failed", result="exit-code", exec_status="1")
                  for unit in probe.JOB_UNITS)
    monkeypatch.setattr(probe, "_unit_states", lambda: states)
    monkeypatch.setattr(probe, "_health", lambda commit, timeout_seconds=8.0: {
        "ok": True, "identity_ok": True, "postgres_fresh": True, "migration_complete": True,
        "package_tree_digest": "f" * 64, "version": __version__,
        "postgres_health_model": "native_readonly_vector_query.v1",
        "postgres_readiness": readiness()})
    monkeypatch.setattr(probe, "_database", lambda database_path: {
        "ok": True, "read_smoke_ok": True, "owner_read_write_access": True,
        "same_database_inode": True, "write_probe_performed": False,
        "integrity_basis": "verified_backup_snapshot",
        "current_full_integrity_scan_performed": False,
        "backup_integrity": {"ok": True, "schema_matches_current_database": True,
            "native_sqlite_integrity_check_ok": True, "native_logical_export_verified": True,
            "published_set": True},
        "ownership": [{"ok": True}], "database_identity": "db-id"})
    monkeypatch.setattr(probe, "_journal", lambda: {
        "ok": True, "observation_scope": "allowlisted_user_unit_journal", "window_seconds": 600,
        "observed_error_counts": {"crash": 0, "oom": 0, "database_read_write_failure": 0}})
    monkeypatch.setattr(probe, "_postgres_snapshot", lambda states, database_path, commit: {
        "ok": True, "measurement_origin": "native_readonly_repository_snapshot",
        "ready": True, "authority_unchanged": True, "fingerprints_match": True,
        "revision_matches": True, "watermark": "watermark-current", "authority_revision": "123"})
    monkeypatch.setattr(probe, "_process_status", lambda state, commit: {
        "ok": True, "unit": state["Id"], "capture_enabled": True, "capture_policy_valid": True,
        "capture_scope_count": 6, "recall_budget_seconds": 8, "hermes_attestation_ready": True,
        "runtime_commit_matches": True, "capture_policy_digest": "c" * 64, "pid": 1,
        "start_ticks": 1, "invocation_id": "a" * 32})
    monkeypatch.setattr(probe, "_credential_binding", lambda states: True)
    monkeypatch.setattr(probe, "_topology", lambda: None)
    held = probe.collect_controlled_stability_sample(
        expected_commit=COMMIT, database_path=path, background_held=True)
    assert held["ok"] is True
    assert held["background_held_observation"] is True
    assert all(row["ok"] for row in held["units"])
    restored = probe.collect_controlled_stability_sample(
        expected_commit=COMMIT, database_path=path, background_held=False)
    assert restored["ok"] is False
    assert "allowlisted_unit_not_ready" in restored["blocked_reasons"]


def test_restored_observation_accepts_reset_failed_oneshot_stale_exec_status(monkeypatch, tmp_path):
    """After reset-failed, inactive oneshots keep ExecMainStatus but Result=success."""
    path = tmp_path / "eimemory.sqlite"
    path.write_bytes(b"")
    states = [_runtime_state(unit) for unit in FIXTURE_RUNTIME_UNITS]
    states.extend(_timer_state(unit, active="active") for unit in probe.TIMER_UNITS)
    states.extend(_job_state(unit, active="inactive", result="success", exec_status="1")
                  for unit in probe.JOB_UNITS)
    monkeypatch.setattr(probe, "_unit_states", lambda: states)
    monkeypatch.setattr(probe, "_health", lambda commit, timeout_seconds=8.0: {
        "ok": True, "identity_ok": True, "postgres_fresh": True, "migration_complete": True,
        "package_tree_digest": "f" * 64, "version": __version__,
        "postgres_health_model": "native_readonly_vector_query.v1",
        "postgres_readiness": readiness()})
    monkeypatch.setattr(probe, "_database", lambda database_path: {
        "ok": True, "read_smoke_ok": True, "owner_read_write_access": True,
        "same_database_inode": True, "write_probe_performed": False,
        "integrity_basis": "verified_backup_snapshot",
        "current_full_integrity_scan_performed": False,
        "backup_integrity": {"ok": True, "schema_matches_current_database": True,
            "native_sqlite_integrity_check_ok": True, "native_logical_export_verified": True,
            "published_set": True},
        "ownership": [{"ok": True}], "database_identity": "db-id"})
    monkeypatch.setattr(probe, "_journal", lambda: {
        "ok": True, "observation_scope": "allowlisted_user_unit_journal", "window_seconds": 600,
        "observed_error_counts": {"crash": 0, "oom": 0, "database_read_write_failure": 0}})
    monkeypatch.setattr(probe, "_postgres_snapshot", lambda states, database_path, commit: {
        "ok": True, "measurement_origin": "native_readonly_repository_snapshot",
        "ready": True, "authority_unchanged": True, "fingerprints_match": True,
        "revision_matches": True, "watermark": "watermark-current", "authority_revision": "123"})
    monkeypatch.setattr(probe, "_process_status", lambda state, commit: {
        "ok": True, "unit": state["Id"], "capture_enabled": True, "capture_policy_valid": True,
        "capture_scope_count": 6, "recall_budget_seconds": 8, "hermes_attestation_ready": True,
        "runtime_commit_matches": True, "capture_policy_digest": "c" * 64, "pid": 1,
        "start_ticks": 1, "invocation_id": "a" * 32})
    monkeypatch.setattr(probe, "_credential_binding", lambda states: True)
    monkeypatch.setattr(probe, "_topology", lambda: None)
    restored = probe.collect_controlled_stability_sample(
        expected_commit=COMMIT, database_path=path, background_held=False)
    assert restored["ok"] is True
    assert all(row["ok"] for row in restored["units"])
    held = probe.collect_controlled_stability_sample(
        expected_commit=COMMIT, database_path=path, background_held=True)
    # Timers are active, so hold mode still blocks; jobs alone must not.
    assert all(row["ok"] for row in held["units"] if row["unit"] in probe.JOB_UNITS)
