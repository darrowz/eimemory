import getpass
from hashlib import sha256
import json

import pytest

from eimemory.adapters.runtime.channel import resolve_channel_scope
from eimemory.api.runtime import Runtime
from eimemory.evaluation.production_query_dataset import (
    build_production_query_dataset, collect_pending_production_queries, resolve_bound_logical_scope,
)
from eimemory.governance.evidence_contract import _pin_scope_matches
import eimemory.identity as identity
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.scheduler import jobs
from eimemory.scheduler.memory_benchmark_context import resolve_memory_benchmark_context
from test_nightly_production_recall_scope import bound_dataset, run_nightly_recall


SCOPE = {"tenant_id": "tenant-one", "agent_id": identity.HONGTU_AGENT_ID,
         "workspace_id": "workspace-one", "user_id": "logical-member"}
HOST = {**SCOPE, "user_id": "host-user"}
LEGACY = "previous-agent"


@pytest.fixture(autouse=True)
def isolate_identity(monkeypatch):
    for name in ("EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE", "EIMEMORY_DEPLOY_SCOPE_USER",
                 "EIMEMORY_SOURCE_IDS", "EIMEMORY_MEMORY_EVAL_DATASET",
                 "EIMEMORY_PRODUCTION_RECALL_DATASETS", "EIMEMORY_PRODUCTION_RECALL_DATASET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EIMEMORY_LEGACY_AGENT_IDS", LEGACY)
    monkeypatch.setattr(identity, "_installation_settings", lambda: {})
    monkeypatch.setattr(getpass, "getuser", lambda: "host-user")


def bindings(tmp_path, monkeypatch, scopes, *, mode=0o600):
    path = tmp_path / "bindings.json"
    path.write_text(json.dumps([{"scope": scope, "receipt_id": "receipt",
                                "receipt_sha256": "a" * 64} for scope in scopes]))
    path.chmod(mode)
    monkeypatch.setenv("EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE", str(path))
    return path


def legacy_pin(**overrides):
    return resolve_channel_scope("hermes", {**SCOPE, "agent_id": LEGACY, **overrides})


def test_existing_legacy_pin_policy_resolves_logical_user_and_channel_without_renaming_caller(tmp_path, monkeypatch):
    pin = legacy_pin()
    bindings(tmp_path, monkeypatch, [pin])
    assert _pin_scope_matches(pin, ScopeRef.from_dict(resolve_channel_scope("hermes", SCOPE)))

    logical, resolution = resolve_bound_logical_scope(HOST)
    context = resolve_memory_benchmark_context(scope=HOST)

    assert logical == SCOPE
    assert resolution["reason"] == "host_user_replaced_by_bound_logical_user"
    assert resolution["rewritten"] is True
    assert context["ok"] is True
    assert context["scope"] == resolve_channel_scope("hermes", SCOPE)
    assert context["resolution"]["matching_channel_count"] == 1
    assert context["resolution"]["logical_user_rewritten"] is True
    assert HOST["user_id"] == "host-user"


@pytest.mark.parametrize("pin_kind", ["unconfigured_alias", "other_agent", "reverse_alias"])
def test_only_existing_directed_agent_alias_policy_applies(tmp_path, monkeypatch, pin_kind):
    requested = HOST
    pin = legacy_pin()
    if pin_kind == "unconfigured_alias":
        monkeypatch.delenv("EIMEMORY_LEGACY_AGENT_IDS")
    elif pin_kind == "other_agent":
        pin["agent_id"] = "unrelated-agent"
    else:
        pin["agent_id"] = SCOPE["agent_id"]
        requested = {**HOST, "agent_id": LEGACY}
    bindings(tmp_path, monkeypatch, [pin])

    logical, resolution = resolve_bound_logical_scope(requested)
    context = resolve_memory_benchmark_context(scope=requested)

    assert logical == requested
    assert resolution["reason"] == "binding_user_missing"
    assert context["ok"] is False
    assert context["reason"] == "memory_benchmark_channel_binding_missing"
    assert context["resolution"]["matching_channel_count"] == 0


@pytest.mark.parametrize("field", ["tenant_id", "workspace_id"])
def test_legacy_alias_never_crosses_other_scope_dimensions(tmp_path, monkeypatch, field):
    bindings(tmp_path, monkeypatch, [legacy_pin(**{field: "another-identity"})])

    logical, resolution = resolve_bound_logical_scope(HOST)
    context = resolve_memory_benchmark_context(scope=HOST)

    assert logical == HOST
    assert resolution["reason"] == "binding_user_missing"
    assert context["reason"] == "memory_benchmark_channel_binding_missing"


def test_explicit_user_is_not_replaced_by_legacy_pin(tmp_path, monkeypatch):
    bindings(tmp_path, monkeypatch, [legacy_pin()])
    requested = {**SCOPE, "user_id": "explicit-member"}

    logical, resolution = resolve_bound_logical_scope(requested)

    assert logical == requested
    assert resolution["reason"] == "explicit_user_preserved"
    assert resolve_memory_benchmark_context(scope=requested)["reason"] == "memory_benchmark_channel_binding_missing"


def test_equivalent_legacy_and_canonical_pins_do_not_make_one_user_or_channel_ambiguous(tmp_path, monkeypatch):
    bindings(tmp_path, monkeypatch, [legacy_pin(), resolve_channel_scope("hermes", SCOPE)])

    logical, resolution = resolve_bound_logical_scope(HOST)
    context = resolve_memory_benchmark_context(scope=HOST)

    assert logical == SCOPE
    assert resolution["rewritten"] is True
    assert context["ok"] is True
    assert context["resolution"]["matching_channel_count"] == 1


def test_different_logical_users_under_aliases_still_block_translation(tmp_path, monkeypatch):
    bindings(tmp_path, monkeypatch, [legacy_pin(), resolve_channel_scope(
        "hermes", {**SCOPE, "user_id": "another-member"})])

    logical, resolution = resolve_bound_logical_scope(HOST)
    context = resolve_memory_benchmark_context(scope=HOST)

    assert logical == HOST
    assert resolution["reason"] == "binding_user_ambiguous"
    assert context["ok"] is False
    assert context["resolution"]["logical_user_rewritten"] is False


def test_unique_user_with_multiple_bound_channels_still_blocks_channel_selection(tmp_path, monkeypatch):
    bindings(tmp_path, monkeypatch, [legacy_pin(), resolve_channel_scope("codex", SCOPE)])

    logical, resolution = resolve_bound_logical_scope(HOST)
    context = resolve_memory_benchmark_context(scope=HOST)

    assert logical == SCOPE
    assert resolution["rewritten"] is True
    assert context["ok"] is False
    assert context["reason"] == "memory_benchmark_channel_binding_ambiguous"
    assert context["resolution"]["matching_channel_count"] == 2


@pytest.mark.parametrize("invalid", ["extra", "missing", "empty", "non_string", "separator",
                                   "unknown_channel", "nested_channel"])
def test_invalid_pin_scopes_never_translate_user_or_select_channel(tmp_path, monkeypatch, invalid):
    pin = legacy_pin()
    if invalid == "extra":
        pin["extra"] = "not-scope-authority"
    elif invalid == "missing":
        del pin["user_id"]
    elif invalid == "empty":
        pin["user_id"] = ""
    elif invalid == "non_string":
        pin["user_id"] = 42
    elif invalid == "separator":
        pin["user_id"] = "invalid\x1fmember"
    elif invalid == "unknown_channel":
        pin["workspace_id"] = SCOPE["workspace_id"] + "::channel::unknown"
    else:
        pin["workspace_id"] += "::channel::codex"
    bindings(tmp_path, monkeypatch, [pin])

    logical, resolution = resolve_bound_logical_scope(HOST)
    context = resolve_memory_benchmark_context(scope=HOST)

    assert logical == HOST
    assert resolution["reason"] == "binding_user_missing"
    assert context["ok"] is False
    assert context["resolution"]["matching_channel_count"] == 0


@pytest.mark.parametrize("invalid", ["public", "symlink"])
def test_alias_pins_keep_private_file_requirement(tmp_path, monkeypatch, invalid):
    path = bindings(tmp_path, monkeypatch, [legacy_pin()], mode=0o644 if invalid == "public" else 0o600)
    if invalid == "symlink":
        link = tmp_path / "link.json"
        link.symlink_to(path)
        monkeypatch.setenv("EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE", str(link))

    logical, resolution = resolve_bound_logical_scope(HOST)
    context = resolve_memory_benchmark_context(scope=HOST)

    assert logical == HOST
    assert resolution["reason"] == "bindings_unreadable"
    assert context["reason"] == "memory_benchmark_bindings_unreadable"


@pytest.mark.parametrize("dataset_agent", ["canonical", "legacy"])
def test_alias_pin_does_not_relax_production_dataset_scope(tmp_path, monkeypatch, dataset_agent):
    bindings(tmp_path, monkeypatch, [legacy_pin()])
    declared = SCOPE if dataset_agent == "canonical" else {**SCOPE, "agent_id": LEGACY}

    report, calls = run_nightly_recall(tmp_path, monkeypatch, HOST, bound_dataset(declared))

    assert calls["build_scope"] == calls["eval_scope"] == SCOPE
    if dataset_agent == "canonical":
        assert calls["hydrate_count"] == 1
        assert report["blocked_reason"] == "label_authority_required"
    else:
        assert calls["hydrate_count"] == 0
        assert report["blocked_reason"] == "dataset_scope_override_mismatch"
        assert report["scope_mismatch_fields"] == ["agent_id"]
    assert report["accepted"] is False


def test_alias_pin_reaches_existing_natural_decision_and_controlled_memory_without_creating_gold(tmp_path, monkeypatch):
    bindings(tmp_path, monkeypatch, [legacy_pin()])
    exact = resolve_channel_scope("hermes", SCOPE)
    with Runtime.create(root=tmp_path / "runtime") as runtime:
        memory = RecordEnvelope.create(
            kind="memory", title="Atlas notebooks", summary="Atlas uses green notebooks",
            content={"text": "Atlas uses green notebooks"}, source="operator.memory", source_id="hermes",
            scope=ScopeRef.from_dict(exact), meta={"memory_type": "fact", "force_capture": True})
        runtime.store.append(memory)
        runtime.store.record_proactive_decision({
            "decision_id": "natural-decision", "channel": "hermes", "scope": exact,
            "source_key": sha256(b"hermes").hexdigest(), "source_ids": ["hermes"],
            "session_id": "natural-session", "turn_id": "natural-turn", "query_id": "natural-query",
            "query_digest": sha256(b"notebook colour").hexdigest(),
            "effective_query_digest": sha256(b"notebook colour").hexdigest(),
            "task_type": "memory.recall", "policy_version": "proactive.test.v1",
            "release_identity": {"release_commit": "a" * 40, "release_version": "1.14.112",
                                 "deployment_receipt_id": "receipt", "release_session_id": "session"},
            "release_bound": True, "control_cohort": False, "pair_id": "natural-pair",
        }, [], [])
        collection = collect_pending_production_queries(runtime, scope=HOST, channel="hermes")
        monkeypatch.setattr(runtime, "build_replay_dataset", lambda **kwargs: {"ok": True, "cases": []})
        benchmark = jobs._run_memory_eval_ci(runtime, scope=HOST)

        assert collection["decision_count"] == collection["new_count"] == 1
        assert collection["scope_resolution"]["rewritten"] is True
        assert benchmark["dataset_source"] == "controlled_records"
        assert benchmark["retrieval_case_count"] == 1
        assert benchmark["memory_benchmark_accepted"] is False
        built = build_production_query_dataset(runtime, scope=HOST)
        assert built["dataset"]["scope"] == SCOPE
        assert built["progress"]["accepted_case_count"] == 0
