"""Bounded old-subject repair; real host rows and native physical versions."""
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
import json
import os
import sqlite3

import pytest

from eimemory.api.runtime import Runtime
from eimemory.identity_authority import AuthoritySnapshot, IdentityAuthorityError, canonical, digest
from eimemory.identity_ops import _exact_ref, _repair_snapshot
from eimemory.metadata import business_metadata
from eimemory.models.records import RecordEnvelope, ScopeRef, TimeRef
from eimemory import subject_projection as subject


def text_digest(text):
    return sha256(text.encode()).hexdigest()


@pytest.fixture
def proposal(tmp_path):
    host_path = (tmp_path / "host.db").resolve()
    host = sqlite3.connect(host_path)
    host.executescript("""
        CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, profile_name TEXT,
                               user_id TEXT, chat_id TEXT, origin_json TEXT);
        CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,
                               content TEXT, timestamp REAL, _compressed_summary INTEGER,
                               active INTEGER, compacted INTEGER);
    """)
    current = ScopeRef(agent_id="hongxin", workspace_id="embodied::channel::hermes", user_id="ou_current_owner")
    old_scope = ScopeRef(**(asdict(current) | {"user_id": "legacy-fallback"}))
    origin = {"platform": "feishu", "chat_type": "dm", "user_id": current.user_id, "chat_id": "oc_original_dm"}
    host.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?)",
                 ("actual-session", "feishu", "hongxin", current.user_id, "oc_original_dm", json.dumps(origin)))
    records, items = [], []
    with Runtime.create(root=tmp_path / "runtime") as runtime:
        for index in range(3):
            user = f"项目{index}的既有投资口径是什么？\n"
            assistant = f"\n项目{index}采用已有建议书的12.7亿元参照口径，资金尚未落实，不可承诺收益。"
            user_id, assistant_id = index * 10 + 1, index * 10 + 2
            # Compaction retains real original bytes. Only synthetic summaries
            # must be rejected; active=0/compacted=1 is legitimate host history.
            host.executemany("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?)", [
                (user_id, "actual-session", "user", user, 1000 + index * 10, 0, 0, 1),
                (assistant_id, "actual-session", "assistant", assistant, 1001 + index * 10, 0, 0, 1)])
            text = "User: " + user.strip() + "\nAssistant: " + assistant.strip()
            record = RecordEnvelope.create(kind="memory", title="Hermes completed turn", summary=text,
                detail=text, content={"text": text, "memory_type": "conversation", "signature": "do-not-copy"},
                scope=old_scope, source="hermes.memory", source_id="hermes", tags=["do-not-copy"],
                evidence=["old-signed-label"], provenance={"signature": "old-host-signature", "host_used": True},
                meta={"memory_type": "conversation", "capture_origin": "turn_sync", "session_id": "actual-session",
                      "turn_id": f"original-turn-{index}", "accepted": True, "signature": "old-signed-evidence"})
            record.time = TimeRef("2026-09-12T11:00:00Z", "2026-09-12T11:00:00Z", "2026-09-12T10:59:00Z")
            record.record_id = f"mem_original_{index}"
            with runtime.store.locked() as db:
                db.upsert(record)
                version = list(_repair_snapshot(db, _exact_ref(record))[1])
            evidence = {"schema": subject.HOST_SCHEMA, "session_id": "actual-session",
                        "user_message_id": user_id, "assistant_message_id": assistant_id,
                        "user_id_digest": text_digest(current.user_id), "chat_id_digest": text_digest("oc_original_dm"),
                        "query_digest": text_digest(user), "answer_digest": text_digest(assistant), "turn_digest": text_digest(text)}
            items.append({"source_ref": list(_exact_ref(record)), "source_version": version,
                          "host_evidence": evidence, "host_evidence_digest": digest(evidence)})
            records.append(record)
        host.commit()
        manifest = {"schema": subject.SCHEMA, "owner_scope": asdict(current), "source_id": "hermes",
                    "channel": "hermes", "host_database": str(host_path), "items": items}
        yield runtime, manifest, records, host_path, host, tmp_path
    host.close()


def physical(runtime):
    return runtime.store.read_consistent(lambda db: [dict(row) for row in db.execute("SELECT * FROM records ORDER BY storage_key")])


def test_default_plan_and_authorize_never_write_or_open_authority(proposal, monkeypatch):
    runtime, manifest, _, host_path, _, _ = proposal
    before = physical(runtime)
    monkeypatch.setattr(subject.authority_api, "snapshot", lambda *a, **kw: pytest.fail("dry run cannot open/grant authority"))
    assert subject.plan_subject_projection(runtime.store, manifest, host_database=host_path)["planned_count"] == 3
    result = subject.authorize_subject_projection(runtime.store, manifest, host_database=host_path,
        directory="must-not-open", request_text="Repair these exact synthetic memories.")
    assert result["dry_run"] and result["written_count"] == result["natural_samples_created"] == 0
    assert physical(runtime) == before


@pytest.mark.parametrize("dimension", ["tenant_id", "agent_id", "workspace_id", "user_id"])
def test_no_scope_wildcards_or_cross_boundary(proposal, dimension):
    runtime, manifest, _, host_path, _, _ = proposal
    mutated = deepcopy(manifest)
    mutated["owner_scope"][dimension] = "foreign"
    with pytest.raises(IdentityAuthorityError):
        subject.plan_subject_projection(runtime.store, mutated, host_database=host_path)


@pytest.mark.parametrize("change", ["wildcard", "source", "same_user", "duplicate", "empty", "extra", "evidence_hash", "host_user"])
def test_manifest_is_closed_and_record_bounded(proposal, change):
    runtime, original, _, host_path, _, _ = proposal
    manifest = deepcopy(original)
    if change == "wildcard": manifest["items"][0]["source_ref"][3] = "*"
    elif change == "source": manifest["source_id"] = "foreign"
    elif change == "same_user": manifest["items"][0]["source_ref"][3] = manifest["owner_scope"]["user_id"]
    elif change == "duplicate": manifest["items"].append(deepcopy(manifest["items"][0]))
    elif change == "empty": manifest["items"] = []
    elif change == "extra": manifest["all_old_scope"] = True
    elif change == "evidence_hash": manifest["items"][0]["host_evidence_digest"] = "0" * 64
    elif change == "host_user":
        evidence = manifest["items"][0]["host_evidence"]
        evidence["user_id_digest"] = text_digest("foreign-user")
        manifest["items"][0]["host_evidence_digest"] = digest(evidence)
    with pytest.raises(IdentityAuthorityError):
        subject.plan_subject_projection(runtime.store, manifest, host_database=host_path)


@pytest.mark.parametrize("damage", ["foreign_user", "missing_origin", "origin_user", "desktop", "group", "wrong_chat",
                                    "summary", "query_bytes", "answer_bytes", "intervening_user"])
def test_real_host_subject_and_full_turn_are_rechecked(proposal, damage):
    runtime, manifest, _, host_path, host, _ = proposal
    if damage == "foreign_user": host.execute("UPDATE sessions SET user_id='another-human'")
    elif damage == "missing_origin": host.execute("UPDATE sessions SET origin_json='{}'")
    elif damage == "origin_user":
        origin = json.loads(host.execute("SELECT origin_json FROM sessions").fetchone()[0]);origin["user_id"] = "another-human"
        host.execute("UPDATE sessions SET origin_json=?", (json.dumps(origin),))
    elif damage == "desktop": host.execute("UPDATE sessions SET source='desktop'")
    elif damage == "group":
        origin = json.loads(host.execute("SELECT origin_json FROM sessions").fetchone()[0]);origin["chat_type"] = "group"
        host.execute("UPDATE sessions SET origin_json=?", (json.dumps(origin),))
    elif damage == "wrong_chat": host.execute("UPDATE sessions SET chat_id='another-chat'")
    elif damage == "summary": host.execute("UPDATE messages SET _compressed_summary=1 WHERE id=1")
    elif damage == "query_bytes": host.execute("UPDATE messages SET content=content||'changed' WHERE id=1")
    elif damage == "answer_bytes": host.execute("UPDATE messages SET content=content||'changed' WHERE id=2")
    elif damage == "intervening_user":
        host.execute("UPDATE messages SET id=4 WHERE id=2")
        host.execute("INSERT INTO messages VALUES (3,'actual-session','user','another turn',1000.5,0,0,1)")
        manifest = deepcopy(manifest);manifest["items"][0]["host_evidence"]["assistant_message_id"] = 4
        manifest["items"][0]["host_evidence_digest"] = digest(manifest["items"][0]["host_evidence"])
    host.commit()
    with pytest.raises(IdentityAuthorityError):
        subject.plan_subject_projection(runtime.store, manifest, host_database=host_path)


@pytest.mark.parametrize("component", [0, 1, 2])
def test_each_native_version_component_is_required(proposal, component):
    runtime, manifest, _, host_path, _, _ = proposal
    manifest = deepcopy(manifest);manifest["items"][0]["source_version"][component] = "0" * 64
    with pytest.raises(IdentityAuthorityError, match="source_version_changed"):
        subject.plan_subject_projection(runtime.store, manifest, host_database=host_path)


def test_host_database_path_cannot_be_swapped(proposal):
    runtime, manifest, _, host_path, _, tmp_path = proposal
    other = tmp_path / "other.db";other.write_bytes(host_path.read_bytes())
    with pytest.raises(IdentityAuthorityError, match="host_database_mismatch"):
        subject.plan_subject_projection(runtime.store, manifest, host_database=other)


def synthetic_grant(manifest):
    """Unit-test only; protected root/journal integration is tested separately below."""
    policy = {"schema": "identity_operator_policy.v1", "requester_uid": 0,
              "owner_scope": manifest["owner_scope"], "source_ids": [manifest["source_id"]]}
    grant = {"operation": "authorize", "event_id": "synthetic-unit-grant", "requester_uid": 0,
             "requested_at": "2026-10-18T10:00:00+00:00", "policy_digest": digest(policy),
             "manifest_digest": digest(manifest), "manifest": manifest, "request_text": "Explicit bounded synthetic repair.",
             "event_hash": "a" * 64}
    return grant, AuthoritySnapshot(policy, [grant])


def test_projection_reconstruction_is_stable_across_seconds_and_copies_no_old_signals(proposal, monkeypatch):
    _, manifest, records, _, _, _ = proposal
    import eimemory.scoring.evaluator as evaluator
    grant, _ = synthetic_grant(manifest)
    monkeypatch.setattr(evaluator, "now_iso", lambda: "2026-10-18T10:01:00Z")
    first = subject._projection(records[0], manifest, manifest["items"][0], grant)
    monkeypatch.setattr(evaluator, "now_iso", lambda: "2026-10-18T10:02:00Z")
    second = subject._projection(records[0], manifest, manifest["items"][0], grant)
    assert first.record_id == second.record_id and first.to_dict() == second.to_dict()
    assert first.content == {"text": records[0].content["text"], "memory_type": "conversation"}
    assert first.evidence == first.links == first.tags == []
    assert first.source == subject.SOURCE and first.scope.user_id == manifest["owner_scope"]["user_id"]
    assert "signature" not in canonical(first.to_dict()) and "host_used" not in canonical(first.to_dict())
    lineage = business_metadata(first.meta)["subject_projection"]
    assert lineage["original_ref"] == manifest["items"][0]["source_ref"]
    assert lineage["original_version"] == manifest["items"][0]["source_version"]
    assert lineage["root_request_digest"] == text_digest(grant["request_text"])
    assert lineage["original_created_at"] == records[0].time.created_at
    assert lineage["projection_created_at"] == first.time.created_at == grant["requested_at"]
    assert first.time.created_at != records[0].time.created_at


def test_projection_text_never_imports_unbound_legacy_summary_or_detail(proposal):
    _, manifest, records, _, _, _ = proposal
    grant, _ = synthetic_grant(manifest)
    old = deepcopy(records[0])
    old.title = old.summary = old.detail = "unrelated legacy private content"
    projected = subject._projection(old, manifest, manifest["items"][0], grant)
    assert projected.summary == projected.detail == old.content["text"]
    assert projected.title == "Hermes completed turn"
    assert "unrelated legacy private content" not in canonical(projected.to_dict())


def test_pure_consumer_checks_grant_source_projection_and_foreign_request(proposal):
    runtime, manifest, records, _, _, _ = proposal
    grant, authority = synthetic_grant(manifest)
    record = subject._projection(records[0], manifest, manifest["items"][0], grant)
    with runtime.store.locked() as db:
        db.upsert(record)
        assert subject.subject_projection_validation_error(db, record, authority=authority) == ""
        assert subject.subject_projection_validation_error(db, record) == "subject_projection_authority_unavailable"
        assert subject.subject_projection_validation_error(db, records[0]) == ""
        foreign = asdict(record.scope) | {"user_id": "foreign"}
        assert subject.subject_projection_validation_error(db, record, authority=authority, scope=foreign)
        assert subject.subject_projection_validation_error(db, record, authority=authority, source_ids=["foreign"])
        altered = deepcopy(record);altered.content["text"] += " altered"
        assert subject.subject_projection_validation_error(db, altered, authority=authority)
        db.execute("UPDATE records SET embedding_json='[]' WHERE record_id=?", (records[0].record_id,))
        assert subject.subject_projection_validation_error(db, record, authority=authority) == "subject_projection_source_version_changed"


@pytest.mark.parametrize("marker", ["source", "id", "meta", "provenance"])
def test_partial_projection_markers_cannot_become_ordinary_records(proposal, marker):
    runtime, _, records, _, _, _ = proposal
    record = deepcopy(records[0])
    if marker == "source": record.source = subject.SOURCE
    elif marker == "id": record.record_id = "mem_sproj_forged"
    elif marker == "meta": record.meta["subject_projection"] = None
    else: record.provenance["subject_projection"] = {}
    assert subject.is_subject_projection(record)
    with runtime.store.locked() as db:
        assert subject.subject_projection_validation_error(db, record) == "subject_projection_authority_unavailable"


native = pytest.mark.skipif(os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() != 0,
                            reason="native protected root operator authority")


@pytest.fixture
def protected(proposal):
    import grp
    runtime, manifest, records, host_path, host, tmp_path = proposal
    directory = tmp_path / "subject-authority"
    subject.authority_api.initialize(directory, owner_scope=manifest["owner_scope"], source_ids=["hermes"],
                                     service_group=grp.getgrgid(os.getegid()).gr_name)
    grant = subject.authorize_subject_projection(runtime.store, manifest, host_database=host_path,
        directory=directory, request_text="The operator authorizes these three exact synthetic prior-subject memories.", dry_run=False)
    return (*proposal, directory, grant)


@native
@pytest.mark.linux_deployment
def test_native_apply_defaults_to_dry_run_then_appends_atomically_and_is_idempotent(protected, monkeypatch):
    runtime, manifest, records, host_path, _, _, directory, grant = protected
    before = physical(runtime)
    kwargs = dict(grant_id=grant["grant_id"], host_database=host_path, directory=directory)
    assert subject.apply_subject_projection(runtime, **kwargs)["written_count"] == 0
    assert physical(runtime) == before
    result = subject.apply_subject_projection(runtime, **kwargs, dry_run=False)
    assert result["written_count"] == 3 and result["original_records_changed"] == result["natural_samples_created"] == 0
    import eimemory.scoring.evaluator as evaluator
    monkeypatch.setattr(evaluator, "now_iso", lambda: "2027-01-01T00:00:00Z")
    assert subject.apply_subject_projection(runtime, **kwargs, dry_run=False)["idempotent"] is True
    for original in records:
        assert runtime.store.get_by_exact_ref(original.record_id, scope=original.scope, source_id=original.source_id).to_dict() == original.to_dict()
    for record_id in result["record_ids"]:
        projected = runtime.store.get_by_exact_ref(record_id, scope=ScopeRef.from_dict(manifest["owner_scope"]), source_id="hermes")
        assert subject.validate_subject_projection(runtime.store, projected, directory=directory)
    assert subject.authorize_subject_projection(runtime.store, manifest, host_database=host_path, directory=directory,
        request_text="Same exact manifest retry.", dry_run=False)["idempotent"]


@native
@pytest.mark.linux_deployment
def test_revocation_changes_consumption_but_preserves_original_and_projection_history(protected):
    runtime, manifest, _, host_path, _, _, directory, grant = protected
    result = subject.apply_subject_projection(runtime, grant_id=grant["grant_id"], host_database=host_path, directory=directory, dry_run=False)
    before = physical(runtime)
    subject.authority_api.revoke(directory, grant_id=grant["grant_id"], reason="Withdraw this precise subject repair.")
    projected = runtime.store.get_by_exact_ref(result["record_ids"][0], scope=ScopeRef.from_dict(manifest["owner_scope"]), source_id="hermes")
    with pytest.raises(IdentityAuthorityError, match="grant_not_active"):
        subject.validate_subject_projection(runtime.store, projected, directory=directory)
    with pytest.raises(IdentityAuthorityError, match="grant_not_active"):
        subject.apply_subject_projection(runtime, grant_id=grant["grant_id"], host_database=host_path, directory=directory, dry_run=False)
    assert physical(runtime) == before


@native
@pytest.mark.linux_deployment
@pytest.mark.parametrize("damage", ["source_payload", "source_physical", "host", "target_conflict", "authority"])
def test_native_apply_failure_never_partially_materializes(protected, damage):
    runtime, manifest, records, host_path, host, _, directory, grant = protected
    if damage == "source_payload":
        changed = deepcopy(records[-1]);changed.content["text"] += " changed"
        with runtime.store.locked() as db: db.upsert(changed)
    elif damage == "source_physical":
        with runtime.store.locked() as db:
            db.execute("UPDATE records SET embedding_json='[]' WHERE record_id=?", (records[-1].record_id,));db.commit()
    elif damage == "host": host.execute("UPDATE sessions SET user_id='different-human'");host.commit()
    elif damage == "target_conflict":
        with subject.authority_api.snapshot(directory) as (authority, _): event = authority.active(grant["grant_id"])
        collision = subject._projection(records[-1], manifest, manifest["items"][-1], event)
        collision.source_id = "foreign"
        with runtime.store.locked() as db: db.upsert(collision)
    else: (directory / "requests.jsonl").chmod(0o666)
    before = physical(runtime)
    with pytest.raises(IdentityAuthorityError):
        subject.apply_subject_projection(runtime, grant_id=grant["grant_id"], host_database=host_path, directory=directory, dry_run=False)
    assert physical(runtime) == before
