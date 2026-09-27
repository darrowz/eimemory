import hashlib
import json
import os
from pathlib import Path
import sqlite3
import pytest

from deploy.offline_state_review import review, open_snapshot, projection_layout, write_new_report
from deploy.explain_acceptance_failure import explain


def database(path):
    connection = sqlite3.connect(path)
    connection.execute('''CREATE TABLE records (
        record_id TEXT, tenant_id TEXT, agent_id TEXT, workspace_id TEXT, user_id TEXT,
        source_id TEXT, kind TEXT, status TEXT, semantic_key TEXT,
        payload_json TEXT DEFAULT '{}', payload_pointer_json TEXT DEFAULT '', storage_key TEXT DEFAULT '')''')
    return connection


def test_offline_review_finds_limits_and_reserved_separator_without_writes(tmp_path):
    path = tmp_path / "backup.sqlite"
    connection = database(path)
    connection.executemany(
        "INSERT INTO records(record_id,tenant_id,agent_id,workspace_id,user_id,source_id,kind,status,semantic_key) "
        "VALUES(?,'t','a','w','u','default','memory','active','key')",
        [(f"r-{i}",) for i in range(10001)],
    )
    connection.execute("UPDATE records SET agent_id='a' || char(31) WHERE record_id='r-0'")
    # Add one extra valid row so the unchanged exact-scope group is >10000.
    connection.execute("INSERT INTO records VALUES('extra','t','a','w','u','default','memory','active','key','{}','','extra')")
    connection.commit(); connection.close()
    before = path.read_bytes()
    report = review(path)
    assert report["reserved_separator_record_count"] == 1
    assert report["overlimit_groups"][0]["active_count"] == 10001
    assert report["requires_data_review"] and not report["repair_performed"]
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_sidecars_require_an_actual_offline_backup(tmp_path, suffix):
    path = tmp_path / "db.sqlite"
    connection = database(path); connection.close()
    Path(str(path) + suffix).touch()
    with pytest.raises(ValueError, match="standalone_offline_backup_required"):
        review(path)


@pytest.mark.parametrize("seconds", [0, -1, float("nan"), float("inf"), 3601])
def test_review_budget_is_bounded(tmp_path, seconds):
    path = tmp_path / "db.sqlite"
    connection = database(path); connection.close()
    with pytest.raises(ValueError):
        review(path, max_seconds=seconds)


def test_report_publication_never_overwrites(tmp_path):
    path = tmp_path / "result.json"
    write_new_report(path, {"sequence": 1})
    with pytest.raises(FileExistsError):
        write_new_report(path, {"sequence": 2})
    assert json.loads(path.read_text()) == {"sequence": 1}
    assert list(tmp_path.iterdir()) == [path]
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600


def test_projection_review_does_not_merge_or_delete(tmp_path):
    (tmp_path / ("a" * 12)).mkdir()
    (tmp_path / ("v2-" + "b" * 64)).mkdir()
    result = projection_layout(tmp_path)
    assert result["mixed_generations"] is True and not result["migration_performed"]
    assert len(list(tmp_path.iterdir())) == 2


def test_original_error_is_private_by_default_and_exact_scope_lookup(tmp_path):
    path = tmp_path / "backup.sqlite"
    connection = database(path)
    for user, error in [("u", "own failure"), ("other", "other-user-secret")]:
        payload = json.dumps({"content": {"error": error, "executor_id": "executor.1"}})
        connection.execute("INSERT INTO records VALUES('probe-1','t','a','w',?,'default','replay_result','active','',?,'','')", (user, payload))
    connection.commit(); connection.close()
    report = {"scope": {"tenant_id": "t", "agent_id": "a", "workspace_id": "w", "user_id": "u"},
              "results": [{"case_id": "case-1", "probe_record_id": "probe-1", "passed": False,
                           "validator_passed": False, "error": "own failure"}]}
    output = explain(report, snapshot=path)
    encoded = json.dumps(output)
    assert "own failure" not in encoded and "other-user-secret" not in encoded
    assert output["records"][0]["probe_error_sha256"] == hashlib.sha256(b"own failure").hexdigest()
    private = explain(report, snapshot=path, include_private_errors=True)
    assert private["records"][0]["probe_error"] == "own failure"
    assert "other-user-secret" not in json.dumps(private)
