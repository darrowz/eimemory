"""The operator CLI defaults to read-only and emits no private manifest bodies."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from test_subject_projection import proposal, protected, native, physical
from eimemory import subject_projection as subject
from eimemory import subject_projection_cli as cli


def arguments(proposal, command):
    runtime, manifest, _, _, _, tmp_path = proposal
    path = tmp_path / "private-manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf8")
    result = [command, "--root", str(runtime.store.root), "--database", str(runtime.store.sqlite.path)]
    if command != "apply":
        result += ["--manifest", str(path)]
    return result


@pytest.mark.parametrize("command", ["preview", "authorize"])
def test_readonly_commands_do_not_construct_runtime_or_schema(proposal, monkeypatch, capsys, command):
    runtime, manifest, _, _, _, _ = proposal
    before = physical(runtime)
    monkeypatch.setattr(cli.SqliteRecordStore, "__init__", lambda *a, **kw: pytest.fail("no schema constructor"))
    monkeypatch.setattr(subject.authority_api, "snapshot", lambda *a, **kw: pytest.fail("no authority writes in dry run"))
    original = subject.plan_subject_projection
    def checked(store, *args, **kwargs):
        if isinstance(store, (str, Path)):
            return original(store, *args, **kwargs)
        assert store.sqlite.conn.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            store.sqlite.conn.execute("UPDATE records SET title='bad'")
        return original(store, *args, **kwargs)
    monkeypatch.setattr(subject, "plan_subject_projection", checked)
    args = arguments(proposal, command)
    if command == "authorize": args += ["--request-text", "Private real operator request fixture."]
    assert cli.main(args) == 0
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["dry_run"] and report["planned_count"] == 3 and report["written_count"] == 0
    assert report["authorization_verified"] is False
    assert manifest["owner_scope"]["user_id"] not in output
    assert "项目" not in output and "Private real operator" not in output
    assert physical(runtime) == before


@pytest.mark.parametrize("damage", ["root", "missing", "manifest", "private_error"])
def test_invalid_paths_and_manifest_fail_before_schema_and_never_echo_private_input(proposal, capsys, damage):
    runtime, manifest, _, _, _, tmp_path = proposal
    args = arguments(proposal, "preview")
    if damage == "root":
        another = tmp_path / "another-root";another.mkdir()
        args[args.index("--root") + 1] = str(another)
    elif damage == "missing": args[args.index("--database") + 1] = str(tmp_path / "absent.sqlite")
    elif damage == "manifest": Path(args[-1]).write_text('{"schema":1,"schema":2}', encoding="utf8")
    elif damage == "private_error":
        changed = deepcopy(manifest);changed["owner_scope"]["user_id"] = "PRIVATE OWNER SECRET*"
        Path(args[-1]).write_text(json.dumps(changed), encoding="utf8")
    assert cli.main(args) == 2
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["ok"] is False and "error" in report
    assert "PRIVATE OWNER SECRET" not in output
    assert "ou_current_owner" not in output and "legacy-fallback" not in output
    assert not (tmp_path / "absent.sqlite").exists()


def test_argument_failure_does_not_echo_private_request(capsys):
    assert cli.main(["authorize", "--request-text", "PRIVATE REQUEST BODY"]) == 2
    output = capsys.readouterr().out
    assert "PRIVATE REQUEST BODY" not in output
    assert json.loads(output)["error"] == "subject_projection_cli_arguments_invalid"


@pytest.mark.parametrize("real,effective,owner,allowed", [(1001,1001,1001,True), (0,0,0,False),
                                                       (1001,0,1001,False), (1001,1001,1002,False)])
def test_service_writer_guard_requires_real_and_effective_database_owner(monkeypatch, real, effective, owner, allowed):
    monkeypatch.setattr(cli.os, "name", "posix")
    monkeypatch.setattr(cli.os, "getuid", lambda: real, raising=False)
    monkeypatch.setattr(cli.os, "geteuid", lambda: effective, raising=False)
    database = SimpleNamespace(stat=lambda: SimpleNamespace(st_uid=owner))
    if allowed:
        cli._require_service_user(database)
    else:
        with pytest.raises(ValueError, match="service_user_required"):
            cli._require_service_user(database)


@native
@pytest.mark.linux_deployment
def test_native_authorize_uses_readonly_source_and_existing_operator_policy(proposal, capsys):
    import grp
    runtime, manifest, _, _, _, tmp_path = proposal
    directory = tmp_path / "subject-cli-authority"
    subject.authority_api.initialize(directory, owner_scope=manifest["owner_scope"], source_ids=["hermes"],
                                     service_group=grp.getgrgid(os.getegid()).gr_name)
    request = tmp_path / "request.private.txt";request.write_text("The authenticated operator requests this exact synthetic repair.", encoding="utf8")
    before = physical(runtime)
    args = arguments(proposal, "authorize") + ["--authority-dir", str(directory), "--request-file", str(request), "--apply"]
    assert cli.main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["grant_id"] and not result["idempotent"]
    with subject.authority_api.snapshot(directory) as (authority, _):
        assert authority.active(result["grant_id"])["manifest_digest"] == subject.digest(manifest)
    assert physical(runtime) == before


@native
@pytest.mark.linux_deployment
def test_apply_default_is_readonly_but_root_cannot_create_service_files(protected, capsys):
    runtime, _, _, _, _, _, directory, grant = protected
    before = physical(runtime)
    args = arguments(protected[:6], "apply") + ["--grant-id", grant["grant_id"], "--authority-dir", str(directory)]
    assert cli.main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["dry_run"] and result["written_count"] == 0 and len(result["record_ids"]) == 3
    assert physical(runtime) == before
    assert cli.main(args + ["--apply"]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "subject_projection_cli_service_user_required"
    assert physical(runtime) == before


@native
@pytest.mark.linux_deployment
def test_managed_database_symlink_is_rejected_before_apply(proposal, capsys):
    runtime, _, _, _, _, tmp_path = proposal
    database = Path(runtime.store.sqlite.path)
    link = tmp_path / "database-link.sqlite";link.symlink_to(database)
    args = arguments(proposal, "preview")
    args[args.index("--database") + 1] = str(link)
    assert cli.main(args) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "subject_projection_cli_database_root_mismatch"


@native
@pytest.mark.linux_deployment
def test_real_unprivileged_service_cli_applies_grant_without_root_owned_data(proposal, monkeypatch, capsys):
    """The positive writer path uses a real dropped UID, not a mocked UID check."""
    import grp
    import pwd
    import shutil
    import subprocess
    import sys
    import tempfile
    from eimemory.storage.runtime_store import RuntimeStore

    runtime, original, _, _, original_host, _ = proposal
    try:
        group = grp.getgrgid(65534)
    except KeyError:
        pytest.skip("isolated unprivileged test group unavailable")
    # A private pytest TMPDIR has root-only ancestors. This separate test-owned
    # directory under the real sticky /tmp is traversable by the dropped UID.
    with tempfile.TemporaryDirectory(prefix="subject-projection-service-", dir="/tmp") as directory:
        scratch = Path(directory);scratch.chmod(0o755)
        data = scratch / "service-data";data.mkdir();(data / "state").mkdir()
        database = data / "state" / "eimemory.sqlite"
        copy = sqlite3.connect(database)
        runtime.store.sqlite.conn.backup(copy);copy.close()
        host_path = data / "host.db"
        copy = sqlite3.connect(host_path);original_host.backup(copy);copy.close()
        manifest = deepcopy(original);manifest["host_database"] = str(host_path)
        # Initialize only this synthetic store before changing it to the service
        # owner. No production database or source directory is opened for write.
        initialized = RuntimeStore(data);initialized.close()
        for path in [data, *data.rglob("*")]:
            os.chown(path, 65534, 65534)
        os.chown(host_path, 65534, 65534)
        source = scratch / "source";source.mkdir()
        shutil.copytree(Path(subject.__file__).parent, source / "eimemory", ignore=shutil.ignore_patterns("__pycache__"))
        # The fixed owner worker imports server code; make this isolated test
        # package traversable instead of borrowing pytest's root-only ancestors.
        monkeypatch.setattr(subject, "__file__", str(source / "eimemory" / "subject_projection.py"))
        home = scratch / "home";home.mkdir();os.chown(home,65534,65534)
        user_site = home / ".local" / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
        user_site.mkdir(parents=True)
        marker = home / "startup-ran"
        startup = user_site / "usercustomize.py"
        startup.write_text("import os\nfrom pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('untrusted startup')\n"
            "print('{\"ok\":true,\"planned_count\":999}', flush=True)\nos._exit(0)\n")
        for path in home.rglob("*"):
            os.chown(path,65534,65534)
        def become_service():
            os.setgroups([]);os.setgid(65534);os.setuid(65534)
        startup_env = {**os.environ, "HOME": str(home), "PYTHONPATH": str(source)}
        startup_env.pop("PYTHONNOUSERSITE", None)
        startup_env.pop("PYTHONUSERBASE", None)
        control = subprocess.run([sys.executable, "-B", "-c", "raise SystemExit('worker reached')"],
            cwd=source, env=startup_env, preexec_fn=become_service, capture_output=True, text=True, timeout=30)
        assert control.returncode == 0 and json.loads(control.stdout)["planned_count"] == 999
        assert marker.exists();marker.unlink()
        original_account = pwd.getpwuid
        account = original_account(65534)
        worker_account = pwd.struct_passwd((*account[:5], str(home), account.pw_shell))
        monkeypatch.setattr(pwd, "getpwuid", lambda uid: worker_account if uid == 65534 else original_account(uid))
        for path in (database, host_path):
            connection = sqlite3.connect(path);connection.execute("PRAGMA journal_mode=WAL");connection.close()
            for suffix in ("-wal", "-shm"):
                assert not Path(str(path) + suffix).exists()
        authority_dir = scratch / "authority"
        subject.authority_api.initialize(authority_dir, owner_scope=manifest["owner_scope"], source_ids=["hermes"],
                                         service_group=group.gr_name)
        manifest_path = scratch / "manifest.private.json";manifest_path.write_text(json.dumps(manifest))
        common = ["--root", str(data), "--database", str(database), "--manifest", str(manifest_path)]
        assert cli.main(["preview", *common]) == 0
        assert json.loads(capsys.readouterr().out)["planned_count"] == 3
        assert not marker.exists(), "owner validation must never run service-writable usercustomize"
        # Native ro WAL reads can create sidecars, but the root controller must
        # never be their filesystem owner for either source or host database.
        sidecars = [Path(str(path) + suffix) for path in (database, host_path) for suffix in ("-wal", "-shm")]
        assert any(path.exists() for path in sidecars)
        assert all(path.stat().st_uid == 65534 for path in sidecars if path.exists())
        assert cli.main(["authorize", *common, "--authority-dir", str(authority_dir), "--request-text",
            "Root explicitly authorizes these synthetic subject projections for the service test.", "--apply"]) == 0
        grant = json.loads(capsys.readouterr().out)
        assert not marker.exists(), "root authorization must use the fixed worker, not user-site code"
        startup.unlink()
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(source), "HOME": str(home),
               "EIMEMORY_CONFIG_DIR": str(home / "config")}
        result = subprocess.run([sys.executable, "-B", "-m", "eimemory.subject_projection_cli", "apply",
            "--root", str(data), "--database", str(database), "--grant-id", grant["grant_id"],
            "--authority-dir", str(authority_dir), "--apply"], cwd=source, env=env, preexec_fn=become_service,
            text=True, capture_output=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        report = json.loads(result.stdout)
        assert report["written_count"] == 3 and report["natural_samples_created"] == 0
        assert database.stat().st_uid == 65534
        assert all(path.stat().st_uid == 65534 for path in data.rglob("*") if path.is_file())
        check = sqlite3.connect(database)
        assert check.execute("SELECT COUNT(*) FROM records WHERE source=?", (subject.SOURCE,)).fetchone()[0] == 3
        assert check.execute("SELECT COUNT(*) FROM records WHERE source='hermes.memory'").fetchone()[0] == 3
        check.close()
