"""Natural dataset -> discovered base scope -> configured strict gate."""
from copy import deepcopy
from dataclasses import asdict
import getpass
import json

import pytest

from eimemory.adapters.runtime.channel import resolve_channel_scope
from eimemory.api.runtime import Runtime
from eimemory.evaluation import real_query_gate
from eimemory.evaluation.production_query_dataset import (
    accept_pending_production_query, build_production_query_dataset,
    collect_pending_production_queries, write_production_query_dataset,
)
from eimemory.evaluation.recall_agent_scopes import recall_agent_scopes, run_per_agent
from test_production_query_dataset import BASE_SCOPE, LABEL_PACKET_EVIDENCE, _seed_decision, _decision_id
from test_production_real_query_gate import RELEASE


@pytest.mark.parametrize("boundary", ["valid", "host_user", "missing_pin", "ambiguous_channel",
    "foreign_tenant_id", "foreign_agent_id", "foreign_workspace_id", "foreign_user_id",
    "explicit_other_user", "explicit_other_channel"])
def test_built_dataset_runs_discovered_agent_only_in_exact_private_channel(
        tmp_path, monkeypatch, trusted_dataset_path_ancestors, boundary):
    monkeypatch.setenv("EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY", "fixture-only-0123456789-abcdefghijklmnopqrstuvwxyz")
    for name in ("EIMEMORY_PRODUCTION_RECALL_DATASET", "EIMEMORY_PRODUCTION_RECALL_DATASETS",
                 "EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE", "EIMEMORY_RECALL_GATE_AGENTS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(getpass, "getuser", lambda: "host-user")
    exact = resolve_channel_scope("hermes", BASE_SCOPE)
    if boundary != "missing_pin":
        pins = [{"scope": exact, "receipt_id": "receipt", "receipt_sha256": "a" * 64}]
        if boundary == "ambiguous_channel":
            pins.append({"scope": resolve_channel_scope("codex", BASE_SCOPE),
                         "receipt_id": "receipt", "receipt_sha256": "a" * 64})
        path = tmp_path / "operator-bindings.json"
        path.write_text(json.dumps(pins))
        path.chmod(0o600)
        monkeypatch.setenv("EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE", str(path))
    monkeypatch.setattr(real_query_gate, "current_release_identity", lambda *args: RELEASE)
    with Runtime.create(root=tmp_path / "runtime") as runtime:
        gold = {index: _seed_decision(runtime, channel="hermes", index=index) for index in range(3)}
        captured = collect_pending_production_queries(runtime, scope=BASE_SCOPE)
        assert captured["created"] == 3
        for pending_id in captured["pending_record_ids"]:
            pending = runtime.store.get_by_id(pending_id)
            index = next(index for index in range(3)
                         if _decision_id("hermes", index) == pending.content["capture_ref"])
            accept_pending_production_query(runtime, pending_record_id=pending_id,
                query_features={"terms": ["hermes", "verified", "release", f"case-{index}"]},
                labels=[{"record_ref": gold[index].record_id, "grade": 3}], labeler="operator",
                operator_scope=BASE_SCOPE, label_packet_evidence=LABEL_PACKET_EVIDENCE)
        built = build_production_query_dataset(runtime, scope=BASE_SCOPE)
        assert built["ready"] and built["progress"]["accepted_case_count"] == 3
        # Installed channel dataset, with the same valid native accepted cases.
        dataset = deepcopy(built["dataset"])
        dataset["scope"] = exact
        if boundary.startswith("foreign_"):
            field = boundary.removeprefix("foreign_")
            dataset["scope"] = {**exact, field: "foreign-partition::channel::hermes"
                                if field == "workspace_id" else "foreign-partition"}
            for case in dataset["cases"]:
                case["scope"] = dict(dataset["scope"])
        path = runtime.store.root / "evaluation" / "agents" / BASE_SCOPE["agent_id"] / "production_recall.json"
        write_production_query_dataset(dataset, path)
        scopes = recall_agent_scopes(runtime, BASE_SCOPE)
        assert len(scopes) == 1 and scopes[0]["workspace_id"] == BASE_SCOPE["workspace_id"]
        requested = {key: scopes[0][key] for key in BASE_SCOPE}
        if boundary == "host_user":
            requested["user_id"] = "host-user"
        elif boundary == "explicit_other_user":
            requested["user_id"] = "explicit-member"
        elif boundary == "explicit_other_channel":
            requested = resolve_channel_scope("codex", requested)
        hydration_scopes = []
        original_hydrate = real_query_gate._hydrate_real_query_labels
        def validate_then_stop(runtime, cases, **kwargs):
            okay, reason, capacities = original_hydrate(runtime, cases, **kwargs)
            assert okay, reason
            hydration_scopes.extend(case["scope"] for case in cases)
            # Scope and genuine persisted label authority were checked. The
            # regression needs no retrieval/model or fabricated release baseline.
            return False, "label_authority_required", capacities
        monkeypatch.setattr(real_query_gate, "_hydrate_real_query_labels", validate_then_stop)
        reports = []
        def evaluate(scope):
            report = runtime.run_configured_production_recall_gate(scope=scope)
            reports.append(report)
            return report
        run_per_agent([{**scopes[0], **requested}], {**BASE_SCOPE, "agent_id": "other-primary"}, {}, evaluate)
        report = reports[0]
        assert report["accepted"] is False
        if boundary in {"valid", "host_user"}:
            assert report["blocked_reason"] == "label_authority_required"
            assert hydration_scopes == [exact] * 6  # preload plus persisted run
            assert asdict(runtime.store.get_by_id(report["persisted_record_id"]).scope) == exact
        else:
            assert report["blocked_reason"] == "dataset_scope_override_mismatch"
            assert hydration_scopes == []
