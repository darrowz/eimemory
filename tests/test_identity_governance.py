"""Real protected operator requests, immutable originals and revocation."""
from copy import deepcopy
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading

import pytest

from eimemory.api.runtime import Runtime
from eimemory.identity_authority import initialize, authorize, revoke, snapshot, IdentityAuthorityError
from eimemory.identity_governance import apply_decision, decision_status, filter_current_records, SOURCE
from eimemory.identity_ops import _exact_ref, _repair_snapshot, repair_hongtu_identity
from eimemory.models.records import RecordEnvelope, ScopeRef

pytestmark = [pytest.mark.linux_deployment,
              pytest.mark.skipif(os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() != 0,
                                 reason="real root-owned Unix authority boundary")]


@pytest.fixture
def pair(tmp_path, monkeypatch):
    import grp
    import eimemory.identity as identity

    monkeypatch.setattr(identity, "HONGTU_AGENT_ID", "synthetic-agent")
    monkeypatch.setattr(identity, "HONGTU_WORKSPACE_ID", "embodied")
    monkeypatch.setattr(identity, "legacy_agent_ids", lambda: ("legacy-agent",))
    owner = ScopeRef(agent_id="synthetic-agent", workspace_id="embodied", user_id="synthetic-owner")
    with Runtime.create(root=tmp_path / "runtime") as runtime:
        old = RecordEnvelope.create(kind="memory", title="Historical cobalt setting", summary="cobalt",
            content={"text": "Historical cobalt setting", "memory_type": "fact"}, source="synthetic",
            scope=ScopeRef(agent_id="legacy-agent", workspace_id="embodied", user_id=owner.user_id))
        old.record_id = "mem_identity_retained"
        current = RecordEnvelope.from_dict(old.to_dict())
        current.scope = owner
        current.title = current.summary = "Current amber setting"
        current.content["text"] = current.title
        with runtime.store.locked() as db:
            db.upsert(old)
            db.upsert(current)
        def capture(db):
            return {side + "_version": list(_repair_snapshot(db, _exact_ref(row))[1])
                    for side, row in (("source", old), ("target", current))}
        manifest = {"schema": "identity_dualview_preview.v1", "owner_scope": asdict(owner),
            "source_id": "default", "items": [{"source_ref": list(_exact_ref(old)),
                "target_ref": list(_exact_ref(current)), **runtime.store.read_consistent(capture),
                "decision": "retain_both", "current": "target"}]}
        directory = tmp_path / "authority"
        initialize(directory, owner_scope=asdict(owner), source_ids=["default"],
                   service_group=grp.getgrgid(os.getegid()).gr_name)
        monkeypatch.setenv("EIMEMORY_IDENTITY_AUTHORITY_DIR", str(directory))
        yield runtime, manifest, old, current, directory


def grant(pair, manifest=None):
    runtime, original, _, _, directory = pair
    return authorize(directory, database=runtime.store.sqlite.path, manifest=manifest or original,
                     request_text="Authenticated operator requests retention of both synthetic originals.")


def physical_originals(pair):
    return pair[0].store.read_consistent(lambda db: [dict(r) for r in db.execute(
        "SELECT * FROM records WHERE record_id=? ORDER BY storage_key", (pair[2].record_id,)).fetchall()])


def test_real_authorization_apply_and_exact_originals(pair):
    runtime, manifest, old, current, directory = pair
    before = physical_originals(pair)
    authority = grant(pair)
    result = apply_decision(runtime, grant_id=authority["grant_id"])
    assert result["retained_pair_count"] == 1 and result["original_records_changed"] == 0
    assert physical_originals(pair) == before
    assert decision_status(runtime, grant_id=authority["grant_id"])["ok"] is True
    for row in (old, current):
        assert runtime.store.get_by_exact_ref(row.record_id, scope=row.scope, source_id=row.source_id).to_dict() == row.to_dict()
    kept, dropped = filter_current_records(runtime.store, [old, current], scope=current.scope)
    assert [r.to_dict() for r in kept] == [current.to_dict()] and dropped == 1
    kept, _ = filter_current_records(runtime.store, [old], scope=old.scope)
    assert kept == [old]
    repaired = repair_hongtu_identity(runtime, scope=current.scope, apply=True)
    assert repaired["ok"] and repaired["retained_pair_count"] == 1
    assert repaired["repaired_count"] == 0 and physical_originals(pair) == before
    assert apply_decision(runtime, grant_id=authority["grant_id"])["idempotent"] is True
    assert grant(pair)["idempotent"] is True


@pytest.mark.parametrize("dimension", ["tenant_id", "agent_id", "workspace_id", "user_id"])
def test_policy_cannot_authorize_another_owner(pair, dimension):
    manifest = deepcopy(pair[1]);manifest["owner_scope"][dimension] = "different"
    with pytest.raises(IdentityAuthorityError, match="owner_source_not_authorized"):
        grant(pair, manifest)


def test_policy_cannot_authorize_another_source(pair):
    manifest = deepcopy(pair[1]);manifest["source_id"] = "other-source"
    with pytest.raises(IdentityAuthorityError, match="owner_source_not_authorized"):
        grant(pair, manifest)


def test_service_cannot_impersonate_authenticated_operator(pair, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1001)
    with pytest.raises(IdentityAuthorityError, match="authenticated_root_operator_required"):
        grant(pair)


@pytest.mark.parametrize("side", ["source", "target"])
@pytest.mark.parametrize("component", [0, 1, 2])
def test_all_supplied_version_components_rechecked_before_authorizing(pair, side, component):
    manifest = deepcopy(pair[1]);manifest["items"][0][side + "_version"][component] = "0" * 64
    with pytest.raises(IdentityAuthorityError, match="validation_failed"):
        grant(pair, manifest)


@pytest.mark.parametrize("side", ["source", "target"])
def test_changed_original_after_grant_prevents_apply(pair, side):
    runtime, _, old, current, _ = pair
    authority = grant(pair)
    row = deepcopy(old if side == "source" else current)
    row.content["text"] = "changed after request"
    with runtime.store.locked() as db:db.upsert(row)
    with pytest.raises(IdentityAuthorityError, match="version_changed"):
        apply_decision(runtime, grant_id=authority["grant_id"])
    assert runtime.store.read_consistent(lambda db: db.execute("SELECT COUNT(*) FROM records WHERE source=?", (SOURCE,)).fetchone()[0]) == 0


def test_physical_projection_change_is_not_a_payload_version_match(pair):
    runtime, _, old, _, _ = pair
    authority = grant(pair)
    with runtime.store.locked() as db:
        db.execute("UPDATE records SET embedding_json='[]' WHERE record_id=? AND agent_id=?",
                   (old.record_id, old.scope.agent_id));db.commit()
    with pytest.raises(IdentityAuthorityError, match="version_changed"):
        apply_decision(runtime, grant_id=authority["grant_id"])


@pytest.mark.parametrize("side", ["source", "target"])
def test_current_consumption_and_nightly_recheck_both_versions(pair, side):
    runtime, _, old, current, _ = pair
    authority = grant(pair);apply_decision(runtime, grant_id=authority["grant_id"])
    row = deepcopy(old if side == "source" else current);row.content["text"] = "new version"
    with runtime.store.locked() as db:db.upsert(row)
    assert not decision_status(runtime, grant_id=authority["grant_id"])["ok"]
    kept, dropped = filter_current_records(runtime.store, [old, current], scope=current.scope)
    assert not kept and dropped == 2
    assert not repair_hongtu_identity(runtime, scope=current.scope, apply=True)["ok"]


def test_revocation_is_append_only_and_restores_unresolved_without_data_rollback(pair):
    runtime, _, old, current, directory = pair
    authority = grant(pair);apply_decision(runtime, grant_id=authority["grant_id"])
    before = physical_originals(pair);journal = (directory / "requests.jsonl").read_bytes()
    revoke(directory, grant_id=authority["grant_id"], reason="Operator withdraws this exact current-view decision")
    assert (directory / "requests.jsonl").read_bytes().startswith(journal)
    assert physical_originals(pair) == before
    assert decision_status(runtime, grant_id=authority["grant_id"])["unresolved_count"] == 1
    assert not filter_current_records(runtime.store, [old, current], scope=current.scope)[0]
    # Both originals would otherwise need identity metadata backfills.
    assert repair_hongtu_identity(runtime, scope=current.scope, apply=True)["blocked_count"] == 2
    for row in (old, current):
        assert runtime.store.get_by_exact_ref(row.record_id, scope=row.scope, source_id=row.source_id)


def test_missing_decision_does_not_reenable_a_legacy_union(pair):
    runtime, _, old, current, _ = pair
    authority = grant(pair);result = apply_decision(runtime, grant_id=authority["grant_id"])
    with runtime.store.locked() as db:
        db.execute("DELETE FROM records WHERE record_id=?", (result["record_id"],));db.commit()
    assert not filter_current_records(runtime.store, [old, current], scope=current.scope)[0]
    assert not repair_hongtu_identity(runtime, scope=current.scope, apply=True)["ok"]


@pytest.mark.parametrize("damage", ["scope", "refs", "kind", "source"])
def test_corrupt_persisted_decision_cannot_hide_protected_grant(pair, damage):
    from eimemory.identity_governance import PARTITION

    runtime, _, old, current, _ = pair
    authority = grant(pair);result = apply_decision(runtime, grant_id=authority["grant_id"])
    decision = runtime.store.get_by_exact_ref(result["record_id"], scope=current.scope, source_id=PARTITION)
    if damage == "scope":decision.scope = ScopeRef(agent_id="different", workspace_id="different", user_id="different")
    elif damage == "refs":
        for side in ("source", "target"):
            decision.content["manifest"]["items"][0][side + "_ref"][-1] = "different"
    elif damage == "kind":decision.kind = "memory"
    else:decision.source = "untrusted"
    with runtime.store.locked() as db:
        db.execute("DELETE FROM records WHERE record_id=?", (result["record_id"],));db.upsert(decision)
    assert not filter_current_records(runtime.store, [old, current], scope=current.scope)[0]
    assert not repair_hongtu_identity(runtime, scope=current.scope, apply=True)["ok"]


def test_policy_change_or_missing_authority_does_not_grant_success(pair):
    runtime, _, old, current, directory = pair
    authority = grant(pair);apply_decision(runtime, grant_id=authority["grant_id"])
    policy = json.loads((directory / "policy.json").read_text());policy["source_ids"].append("new-source")
    (directory / "policy.json").write_text(json.dumps(policy))
    assert not filter_current_records(runtime.store, [old, current], scope=current.scope)[0]
    (directory / "requests.jsonl").unlink()
    assert not filter_current_records(runtime.store, [old, current], scope=current.scope)[0]


@pytest.mark.parametrize("damage", ["mode", "link", "chain", "truncated"])
def test_untrusted_authority_files_fail_closed(pair, damage):
    runtime, _, old, current, directory = pair
    authority = grant(pair);apply_decision(runtime, grant_id=authority["grant_id"])
    journal = directory / "requests.jsonl"
    if damage == "mode":journal.chmod(0o666)
    elif damage == "link":os.link(journal, directory / "untrusted-hardlink")
    elif damage == "chain":journal.write_bytes(journal.read_bytes().replace(b'"requester_uid":0', b'"requester_uid":1'))
    else:journal.write_bytes(journal.read_bytes()[:-1])
    assert not filter_current_records(runtime.store, [old, current], scope=current.scope)[0]


def test_overlapping_request_must_first_be_revoked(pair):
    first = grant(pair)
    proposal = deepcopy(pair[1]);proposal["request_note"] = "different request, same original versions"
    with pytest.raises(IdentityAuthorityError, match="overlapping_request"):
        grant(pair, proposal)
    revoke(pair[4], grant_id=first["grant_id"], reason="Replace the prior request")
    second = grant(pair, proposal)
    assert second["grant_id"] != first["grant_id"]
    apply_decision(pair[0], grant_id=second["grant_id"])
    assert decision_status(pair[0], grant_id=second["grant_id"])["ok"]


def test_revocation_cannot_commit_in_the_middle_of_a_decision_transaction(pair, monkeypatch):
    runtime, _, _, _, directory = pair
    authority = grant(pair);entered = threading.Event();finished = threading.Event()
    original = runtime.store.mutate_records_atomically
    def revoke_concurrently():
        entered.set();revoke(directory, grant_id=authority["grant_id"], reason="Concurrent revocation");finished.set()
    worker = threading.Thread(target=revoke_concurrently)
    def during_commit(callback):
        worker.start();assert entered.wait(2)
        assert not finished.wait(0.1)
        result = original(callback)
        assert not finished.is_set()
        return result
    monkeypatch.setattr(runtime.store, "mutate_records_atomically", during_commit)
    apply_decision(runtime, grant_id=authority["grant_id"])
    worker.join(5);assert finished.is_set()
    assert not decision_status(runtime, grant_id=authority["grant_id"])["ok"]


def test_actual_recall_does_not_fall_back_to_historical_content(pair):
    runtime, _, old, current, _ = pair
    authority = grant(pair);apply_decision(runtime, grant_id=authority["grant_id"])
    for strategy in ("canonical_first", "legacy_union"):
        result = runtime.memory.recall(query="cobalt", scope=asdict(current.scope), limit=5,
                                       task_context={"scope_strategy": strategy})
        assert old.content["text"] not in json.dumps(result.to_dict())
        assert all(item.scope == current.scope for item in result.items)
    result = runtime.memory.recall(query="amber", scope=asdict(current.scope), limit=5,
                                   task_context={"scope_strategy": "canonical_first"})
    assert current.record_id in [item.record_id for item in result.items]


def test_a_service_written_decision_cannot_create_operator_authority(pair):
    from eimemory.identity_governance import PARTITION, SCHEMA

    runtime, manifest, old, current, _ = pair
    forged = RecordEnvelope.create(kind="evaluation_packet", title="Untrusted decision", source=SOURCE,
        source_id=PARTITION, scope=current.scope,
        content={"schema": SCHEMA, "grant_id": "forged", "manifest": manifest},
        provenance={"identity_scope_preserved": True})
    with runtime.store.locked() as db:db.upsert(forged)
    assert not filter_current_records(runtime.store, [old, current], scope=current.scope)[0]
    assert not repair_hongtu_identity(runtime, scope=current.scope, apply=True)["ok"]


def test_invalid_second_pair_rolls_back_entire_decision(pair):
    runtime, manifest, old, current, _ = pair
    proposal = deepcopy(manifest)
    extra_old, extra_current = deepcopy(old), deepcopy(current)
    extra_old.record_id = extra_current.record_id = "mem_second_original"
    with runtime.store.locked() as db:
        db.upsert(extra_old);db.upsert(extra_current)
    def capture(db):
        return {"source_ref": list(_exact_ref(extra_old)), "target_ref": list(_exact_ref(extra_current)),
            "source_version": list(_repair_snapshot(db, _exact_ref(extra_old))[1]),
            "target_version": list(_repair_snapshot(db, _exact_ref(extra_current))[1]),
            "decision": "retain_both", "current": "target"}
    proposal["items"].append(runtime.store.read_consistent(capture))
    authority = grant(pair, proposal)
    extra_current.content["text"] = "Changed second pair"
    with runtime.store.locked() as db:db.upsert(extra_current)
    before = physical_originals(pair)
    with pytest.raises(IdentityAuthorityError, match="version_changed"):
        apply_decision(runtime, grant_id=authority["grant_id"])
    assert physical_originals(pair) == before
    assert runtime.store.read_consistent(lambda db: db.execute(
        "SELECT COUNT(*) FROM records WHERE source=?", (SOURCE,)).fetchone()[0]) == 0


def test_final_consumption_checks_auxiliary_only_evidence_after_revocation(pair, monkeypatch):
    from eimemory.models.records import RecallBundle
    from eimemory.retrieval.engine import GovernedRecallEngine

    runtime, _, _, current, directory = pair
    authority = grant(pair);apply_decision(runtime, grant_id=authority["grant_id"])
    def completed_before_consumption(self, request):
        revoke(directory, grant_id=authority["grant_id"], reason="Revoke before delivery")
        return RecallBundle(items=[], rules=[], reflections=[], confidence=0.5, next_action_hint="",
            explanation={"cascade_evidence": [{"record_id": current.record_id, "title": current.title}],
                         "raw_evidence": [{"record": current.to_dict()}]})
    monkeypatch.setattr(GovernedRecallEngine, "_recall", completed_before_consumption)
    result = runtime.memory.recall(query="amber", scope=asdict(current.scope), limit=5)
    assert current.title not in json.dumps(result.to_dict())
    assert result.explanation["identity_current_view"]["ok"] is False


def test_operator_cli_does_not_open_the_service_runtime(pair, monkeypatch, capsys):
    from eimemory.cli.main import main

    runtime, manifest, _, _, directory = pair
    path = directory / "proposal.json";path.write_text(json.dumps(manifest))
    def forbidden_runtime(*args, **kwargs):
        raise AssertionError("operator request must not instantiate the memory writer")
    monkeypatch.setattr(Runtime, "create", forbidden_runtime)
    assert main(["identity", "authorize", "--authority-dir", str(directory),
        "--database", str(runtime.store.sqlite.path), "--manifest", str(path),
        "--request-text", "Root operator requests the exact synthetic retain-both proposal"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert main(["identity", "decision-revoke", "--authority-dir", str(directory),
        "--grant-id", result["grant_id"], "--reason", "CLI test revocation"]) == 0


def test_symlinked_authority_directory_is_untrusted(pair, monkeypatch):
    runtime, _, old, current, directory = pair
    authority = grant(pair);apply_decision(runtime, grant_id=authority["grant_id"])
    alias = directory.parent / "authority-alias";alias.symlink_to(directory, target_is_directory=True)
    monkeypatch.setenv("EIMEMORY_IDENTITY_AUTHORITY_DIR", str(alias))
    assert not filter_current_records(runtime.store, [old, current], scope=current.scope)[0]


def test_actual_nonroot_service_can_read_but_cannot_issue_or_edit_authority():
    import grp
    import pwd

    account = pwd.getpwnam("nobody")
    with tempfile.TemporaryDirectory(prefix="eimemory-authority-permissions-", dir="/tmp") as temporary:
        parent = Path(temporary);os.chown(parent, 0, account.pw_gid);parent.chmod(0o750)
        directory = parent / "authority"
        initialize(directory, owner_scope=asdict(ScopeRef(agent_id="synthetic", workspace_id="synthetic", user_id="synthetic")),
                   source_ids=["default"], service_group=grp.getgrgid(account.pw_gid).gr_name)
        script = """
import os
from eimemory.identity_authority import snapshot, authorize, IdentityAuthorityError
directory = os.environ['EIMEMORY_IDENTITY_AUTHORITY_DIR']
with snapshot(directory) as (authority, _):
    assert authority.policy['requester_uid'] == 0
try:
    authorize(directory, database='unused', manifest={}, request_text='Impersonation')
except IdentityAuthorityError:
    pass
else:
    raise AssertionError('service issued a root request')
for name in ('policy.json', 'requests.jsonl'):
    try:
        fd = os.open(directory + '/' + name, os.O_WRONLY)
    except PermissionError:
        continue
    os.close(fd)
    raise AssertionError('service can edit protected authority')
print('protected read-only service access verified')
"""
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
               "EIMEMORY_IDENTITY_AUTHORITY_DIR": str(directory), "PYTHONDONTWRITEBYTECODE": "1"}
        result = subprocess.run(["runuser", "-u", account.pw_name, "--", "/usr/bin/python3", "-B", "-c", script],
                                cwd=parent, env=env, capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stderr
        assert "read-only service access verified" in result.stdout
