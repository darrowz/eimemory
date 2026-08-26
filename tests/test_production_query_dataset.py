from __future__ import annotations

from hashlib import sha256
import json

import pytest

from eimemory.api.runtime import Runtime
from eimemory.adapters.runtime.channel import resolve_channel_scope
from eimemory.cli.main import main as cli_main
from eimemory.evaluation.production_query_dataset import (
    ACCEPTED_QUERY_SCHEMA,
    ACCEPTED_SOURCE,
    LABEL_EVIDENCE_SOURCE,
    PENDING_QUERY_SCHEMA,
    PENDING_SOURCE,
    accept_pending_production_query,
    build_production_query_dataset,
    collect_pending_production_queries,
    write_production_query_dataset,
)
from eimemory.evaluation.real_query_gate import _stable_digest, freeze_production_recall_dataset
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.scheduler.jobs import load_json_dataset_with_evidence


BASE_SCOPE = {"tenant_id": "default", "agent_id": "main", "workspace_id": "production", "user_id": "darrow"}
LABEL_PACKET_EVIDENCE = {
    "schema": "secure_dataset_fingerprint.v1",
    "digest": "d" * 64,
    "size": 512,
    "device": 1,
    "inode": 1,
}


def _seed_decision(runtime: Runtime, *, channel: str, index: int) -> RecordEnvelope:
    scope = resolve_channel_scope(channel, BASE_SCOPE)
    source_id = f"source-{channel}"
    record = RecordEnvelope.create(
        kind="memory",
        title=f"{channel} verified release memory {index}",
        summary="safe durable evidence",
        source=f"{channel}.memory",
        source_id=source_id,
        scope=ScopeRef.from_dict(scope),
        meta={"force_capture": True},
    )
    runtime.store.append(record)
    digest = sha256(f"raw secret query {channel} {index}".encode()).hexdigest()
    runtime.store.record_proactive_decision(
        {
            "decision_id": f"decision-{channel}-{index}",
            "channel": channel,
            "scope": scope,
            "source_key": sha256(source_id.encode()).hexdigest(),
            "source_ids": [source_id],
            "session_id": f"session-{channel}-{index}",
            "turn_id": f"turn-{index}",
            "query_id": f"query-{index}",
            "query_digest": digest,
            "effective_query_digest": digest,
            "task_type": "memory.recall",
            "policy_version": "proactive.test.v1",
            "release_identity": {
                "release_commit": "a" * 40,
                "release_version": "1.9.80",
                "deployment_receipt_id": "receipt",
                "release_session_id": "session",
            },
            "release_bound": True,
            "control_cohort": False,
            "pair_id": f"pair-{channel}-{index}",
        },
        [{"citation": "M1", "record_id": record.record_id, "source_id": source_id, "confidence": 0.9, "order": 0, "render_digest": "d" * 64}],
        [],
    )
    return record


def _append_legacy_low_signal_accepted_case(runtime: Runtime, *, channel: str, index: int, record: RecordEnvelope) -> None:
    scope = resolve_channel_scope(channel, BASE_SCOPE)
    features = {
        "terms": [
            channel,
            "default",
            "proactive_audit_capture",
            "Ground",
            "truth",
            "behavior",
            "When",
        ],
        "intent": "production recall",
    }
    case = {
        "case_id": f"real-low-signal-{channel}-{index}",
        "collection_window": {
            "started_at": "2026-07-20T00:00:00+00:00",
            "ended_at": "2026-07-21T00:00:00+00:00",
        },
        "channel": channel,
        "source_id": record.source_id,
        "scope": scope,
        "query_features": features,
        "query_digest": _stable_digest(features),
        "labels": [
            {
                "record_ref": record.record_id,
                "grade": 3,
                "accepted": True,
                "provenance": {
                    "labeler": "operator",
                    "labelled_at": "2026-07-20T12:00:00+00:00",
                    "evidence_ref": f"legacy-label-{channel}-{index}",
                },
            }
        ],
        "provenance": {"collector": "proactive_audit_capture", "capture_ref": f"legacy-{channel}-{index}"},
    }
    accepted = RecordEnvelope.create(
        kind="evaluation_packet",
        title=f"Accepted low signal production recall case {channel}",
        summary="Legacy human-labelled production recall case with collector-only query features.",
        content={"schema": ACCEPTED_QUERY_SCHEMA, "case": case},
        source=ACCEPTED_SOURCE,
        source_id=record.source_id,
        scope=ScopeRef.from_dict(scope),
        status="active",
        evidence=[record.record_id],
        meta={"report_type": "production_recall_accepted_case", "schema": ACCEPTED_QUERY_SCHEMA, "channel": channel},
    )
    accepted.record_id = f"legacy-low-signal-{channel}-{index}"
    runtime.store.append(accepted)


def test_real_audit_collection_operator_acceptance_and_immutable_dataset_build(
    tmp_path,
    trusted_dataset_path_ancestors,
) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    expected: dict[tuple[str, int], RecordEnvelope] = {}
    for channel in ("openclaw", "codex", "hermes"):
        for index in range(5):
            expected[(channel, index)] = _seed_decision(runtime, channel=channel, index=index)

    collected = collect_pending_production_queries(runtime, scope=BASE_SCOPE)
    assert collected["created"] == 15
    assert "raw secret query" not in json.dumps(collected)

    for pending_id in collected["pending_record_ids"]:
        pending = runtime.store.get_by_id(pending_id)
        assert pending is not None
        channel = str(pending.content["channel"])
        index = int(str(pending.content["capture_ref"]).rsplit("-", 1)[1])
        accepted = accept_pending_production_query(
            runtime,
            pending_record_id=pending_id,
            query_features={"terms": [channel, "verified", "release", f"case-{index}"], "intent": "memory recall"},
            labels=[{"record_ref": expected[(channel, index)].record_id, "grade": 3}],
            labeler="operator",
            operator_scope=BASE_SCOPE,
            label_packet_evidence=LABEL_PACKET_EVIDENCE,
        )
        assert accepted["ok"] is True

    dataset = build_production_query_dataset(runtime, scope=BASE_SCOPE)
    assert dataset["ready"] is True
    assert dataset["progress"]["per_channel_accepted"] == {"codex": 5, "hermes": 5, "openclaw": 5}
    output = tmp_path / "production-redacted.json"
    written = write_production_query_dataset(dataset["dataset"], output)
    loaded, evidence = load_json_dataset_with_evidence(str(output))
    frozen = freeze_production_recall_dataset({**loaded, "_secure_dataset_evidence": evidence})
    assert written["ok"] is True
    assert frozen["eligibility"]["ok"] is True
    assert "raw secret query" not in output.read_text(encoding="utf-8")
    runtime.close()


def test_dataset_build_requires_all_production_channels(
    tmp_path,
    trusted_dataset_path_ancestors,
) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    expected: dict[int, RecordEnvelope] = {}
    for index in range(5):
        expected[index] = _seed_decision(runtime, channel="openclaw", index=index)

    collected = collect_pending_production_queries(runtime, scope=BASE_SCOPE)
    assert collected["created"] == 5

    for pending_id in collected["pending_record_ids"]:
        pending = runtime.store.get_by_id(pending_id)
        assert pending is not None
        index = int(str(pending.content["capture_ref"]).rsplit("-", 1)[1])
        accepted = accept_pending_production_query(
            runtime,
            pending_record_id=pending_id,
            query_features={"terms": ["openclaw", "verified", "release", f"case-{index}"], "intent": "memory recall"},
            labels=[{"record_ref": expected[index].record_id, "grade": 3}],
            labeler="operator",
            operator_scope=BASE_SCOPE,
            label_packet_evidence=LABEL_PACKET_EVIDENCE,
        )
        assert accepted["ok"] is True

    dataset = build_production_query_dataset(runtime, scope=BASE_SCOPE)

    assert dataset["ready"] is False
    assert dataset["progress"]["accepted_case_count"] == 5
    assert dataset["progress"]["active_channels"] == ["openclaw"]
    assert dataset["progress"]["required_case_count"] == 15
    assert dataset["progress"]["required_channels"] == ["codex", "hermes", "openclaw"]
    assert dataset["progress"]["required_per_channel"] == 5
    assert dataset["progress"]["per_channel_accepted"] == {"codex": 0, "hermes": 0, "openclaw": 5}
    output = tmp_path / "production-redacted.json"
    written = write_production_query_dataset(dataset["dataset"], output)
    loaded, evidence = load_json_dataset_with_evidence(str(output))
    frozen = freeze_production_recall_dataset({**loaded, "_secure_dataset_evidence": evidence})
    assert written["ok"] is True
    assert frozen["eligibility"]["ok"] is False
    assert "required_channel_coverage_missing" in frozen["eligibility"]["blocked_reasons"]
    assert frozen["eligibility"]["active_channels"] == ["openclaw"]
    runtime.close()


def test_dataset_build_blocks_active_channel_until_minimum_cases(
    tmp_path,
    trusted_dataset_path_ancestors,
) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    expected: dict[int, RecordEnvelope] = {}
    for index in range(4):
        expected[index] = _seed_decision(runtime, channel="openclaw", index=index)

    collected = collect_pending_production_queries(runtime, scope=BASE_SCOPE)
    assert collected["created"] == 4

    for pending_id in collected["pending_record_ids"]:
        pending = runtime.store.get_by_id(pending_id)
        assert pending is not None
        index = int(str(pending.content["capture_ref"]).rsplit("-", 1)[1])
        accept_pending_production_query(
            runtime,
            pending_record_id=pending_id,
            query_features={"terms": ["openclaw", "verified", "release", f"case-{index}"], "intent": "memory recall"},
            labels=[{"record_ref": expected[index].record_id, "grade": 3}],
            labeler="operator",
            operator_scope=BASE_SCOPE,
            label_packet_evidence=LABEL_PACKET_EVIDENCE,
        )

    dataset = build_production_query_dataset(runtime, scope=BASE_SCOPE)
    output = tmp_path / "production-redacted-mixed.json"
    write_production_query_dataset(dataset["dataset"], output)
    loaded, evidence = load_json_dataset_with_evidence(str(output))
    frozen = freeze_production_recall_dataset({**loaded, "_secure_dataset_evidence": evidence})

    assert dataset["ready"] is False
    assert dataset["progress"]["active_channels"] == ["openclaw"]
    assert dataset["progress"]["required_case_count"] == 15
    assert dataset["progress"]["required_per_channel"] == 5
    assert "minimum_case_count_missing" in frozen["eligibility"]["blocked_reasons"]
    runtime.close()


def test_dataset_build_uses_overall_minimum_across_active_channels(
    tmp_path,
    trusted_dataset_path_ancestors,
) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    expected: dict[tuple[str, int], RecordEnvelope] = {}
    for channel, total in (("openclaw", 3), ("codex", 2)):
        for index in range(total):
            expected[(channel, index)] = _seed_decision(runtime, channel=channel, index=index)

    collected = collect_pending_production_queries(runtime, scope=BASE_SCOPE)
    assert collected["created"] == 5

    for pending_id in collected["pending_record_ids"]:
        pending = runtime.store.get_by_id(pending_id)
        assert pending is not None
        channel = str(pending.content["channel"])
        index = int(str(pending.content["capture_ref"]).rsplit("-", 1)[1])
        accept_pending_production_query(
            runtime,
            pending_record_id=pending_id,
            query_features={"terms": [channel, "verified", "release", f"case-{index}"], "intent": "memory recall"},
            labels=[{"record_ref": expected[(channel, index)].record_id, "grade": 3}],
            labeler="operator",
            operator_scope=BASE_SCOPE,
            label_packet_evidence=LABEL_PACKET_EVIDENCE,
        )

    dataset = build_production_query_dataset(runtime, scope=BASE_SCOPE)
    output = tmp_path / "production-redacted-overall.json"
    write_production_query_dataset(dataset["dataset"], output)
    loaded, evidence = load_json_dataset_with_evidence(str(output))
    frozen = freeze_production_recall_dataset({**loaded, "_secure_dataset_evidence": evidence})

    assert dataset["ready"] is False
    assert dataset["progress"]["accepted_case_count"] == 5
    assert dataset["progress"]["active_channels"] == ["codex", "openclaw"]
    assert dataset["progress"]["required_case_count"] == 15
    assert dataset["progress"]["required_per_channel"] == 5
    assert frozen["eligibility"]["ok"] is False
    assert "required_channel_coverage_missing" in frozen["eligibility"]["blocked_reasons"]
    runtime.close()


def test_operator_cannot_accept_low_signal_production_query_features(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    expected = _seed_decision(runtime, channel="openclaw", index=0)
    pending_id = collect_pending_production_queries(runtime, scope=BASE_SCOPE)["pending_record_ids"][0]

    with pytest.raises(ValueError, match="query_features_low_signal"):
        accept_pending_production_query(
            runtime,
            pending_record_id=pending_id,
            query_features={
                "terms": [
                    "openclaw",
                    "default",
                    "proactive_audit_capture",
                    "Ground",
                    "truth",
                    "behavior",
                    "When",
                ],
                "intent": "production recall",
            },
            labels=[{"record_ref": expected.record_id, "grade": 3}],
            labeler="operator",
            operator_scope=BASE_SCOPE,
            label_packet_evidence=LABEL_PACKET_EVIDENCE,
        )
    runtime.close()


def test_dataset_build_ignores_legacy_low_signal_accepted_cases(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    records = [_seed_decision(runtime, channel="openclaw", index=index) for index in range(5)]
    for index, record in enumerate(records):
        _append_legacy_low_signal_accepted_case(runtime, channel="openclaw", index=index, record=record)

    dataset = build_production_query_dataset(runtime, scope=BASE_SCOPE)

    assert dataset["ready"] is False
    assert dataset["progress"]["accepted_case_count"] == 0
    assert dataset["progress"]["skipped_low_signal"] == 5
    assert "minimum_case_count_missing" in dataset["progress"]["blocked_reasons"]
    runtime.close()


def test_dataset_build_uses_indexed_report_type_under_unrelated_record_load(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    expected = [_seed_decision(runtime, channel="codex", index=index) for index in range(5)]
    pending_ids = collect_pending_production_queries(runtime, scope=BASE_SCOPE)["pending_record_ids"]
    for pending_id in pending_ids:
        pending = runtime.store.get_by_id(pending_id)
        assert pending is not None
        index = int(str(pending.content["capture_ref"]).rsplit("-", 1)[1])
        accept_pending_production_query(
            runtime,
            pending_record_id=pending_id,
            query_features={"terms": ["codex", "verified", "release", f"case-{index}"], "intent": "memory recall"},
            labels=[{"record_ref": expected[index].record_id, "grade": 3}],
            labeler="operator",
            operator_scope=BASE_SCOPE,
            label_packet_evidence=LABEL_PACKET_EVIDENCE,
        )

    exact = ScopeRef.from_dict(resolve_channel_scope("codex", BASE_SCOPE))
    for index in range(501):
        runtime.store.append(
            RecordEnvelope.create(
                kind="evaluation_packet",
                title=f"Unrelated newer evaluation packet {index}",
                summary="Must not hide indexed accepted production cases.",
                source="eimemory.unrelated_evaluation",
                source_id="unrelated",
                scope=exact,
                meta={"report_type": "unrelated_evaluation"},
            )
        )

    dataset = build_production_query_dataset(runtime, scope=BASE_SCOPE)

    assert dataset["progress"]["per_channel_accepted"]["codex"] == 5
    runtime.close()


def test_operator_cannot_label_across_channel_or_source_boundary(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    correct = _seed_decision(runtime, channel="codex", index=0)
    wrong = _seed_decision(runtime, channel="hermes", index=0)
    pending_ids = collect_pending_production_queries(runtime, scope=BASE_SCOPE)["pending_record_ids"]
    pending_id = next(
        record_id
        for record_id in pending_ids
        if runtime.store.get_by_id(record_id).content["channel"] == "codex"
    )

    with pytest.raises(ValueError, match="boundary"):
        accept_pending_production_query(
            runtime,
            pending_record_id=pending_id,
            query_features={"terms": ["codex", "verified", "release"]},
            labels=[{"record_ref": wrong.record_id, "grade": 3}],
            labeler="operator",
            operator_scope=BASE_SCOPE,
            label_packet_evidence=LABEL_PACKET_EVIDENCE,
        )
    with pytest.raises(ValueError, match="boundary"):
        accept_pending_production_query(
            runtime,
            pending_record_id=pending_id,
            query_features={"terms": ["codex", "verified", "release"]},
            labels=[{"record_ref": correct.record_id, "grade": 3}],
            labeler="operator",
            operator_scope={**BASE_SCOPE, "tenant_id": "other-tenant"},
            label_packet_evidence=LABEL_PACKET_EVIDENCE,
        )
    runtime.close()


def test_dataset_reader_rejects_synthetic_accepted_source_shell(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    exact = ScopeRef.from_dict(resolve_channel_scope("codex", BASE_SCOPE))
    forged_case = {
        "case_id": "forged-case",
        "collection_window": {
            "started_at": "2026-01-01T00:00:00+00:00",
            "ended_at": "2026-01-02T00:00:00+00:00",
        },
        "channel": "codex",
        "source_id": "forged-source",
        "scope": resolve_channel_scope("hermes", BASE_SCOPE),
        "query_features": {
            "terms": ["codex", "verified", "release", "production"],
            "intent": "memory recall",
        },
        "query_digest": "f" * 64,
        "labels": [],
        "provenance": {"collector": "proactive_audit_capture", "capture_ref": "forged"},
    }
    forged = RecordEnvelope.create(
        kind="evaluation_packet",
        title="Forged accepted production case",
        summary="Must never satisfy the production query gate.",
        content={"schema": "wrong.schema", "case": forged_case},
        source=ACCEPTED_SOURCE,
        source_id="different-source",
        scope=exact,
        status="active",
        meta={
            "report_type": "production_recall_accepted_case",
            "schema": ACCEPTED_QUERY_SCHEMA,
            "channel": "codex",
            "case_id": "forged-case",
        },
    )
    runtime.store.append(forged)

    dataset = build_production_query_dataset(runtime, scope=BASE_SCOPE)

    assert dataset["progress"]["accepted_case_count"] == 0
    assert dataset["progress"]["per_channel_accepted"]["codex"] == 0
    assert dataset["ready"] is False
    runtime.close()


def test_dataset_reader_rejects_structurally_valid_chain_without_proactive_decision(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    channel = "codex"
    exact_dict = resolve_channel_scope(channel, BASE_SCOPE)
    exact = ScopeRef.from_dict(exact_dict)
    source_id = "synthetic-source"
    candidate = RecordEnvelope.create(
        kind="memory",
        title="Synthetic candidate",
        summary="No proactive decision authorizes this record.",
        source="synthetic.memory",
        source_id=source_id,
        scope=exact,
        status="active",
    )
    runtime.store.append(candidate)
    decision_id = "nonexistent-decision"
    capture_digest = "a" * 64
    case_id = "real-" + _stable_digest(
        {"decision_id": decision_id, "query_digest": capture_digest}
    )[:24]
    pending = RecordEnvelope.create(
        kind="evaluation_packet",
        title="Synthetic pending",
        summary="Structurally valid but not ledger backed.",
        content={
            "schema": PENDING_QUERY_SCHEMA,
            "case_id": case_id,
            "channel": channel,
            "source_id": source_id,
            "scope": exact_dict,
            "capture_query_digest": capture_digest,
            "suggested_query_features": {
                "terms": ["memory.recall"],
                "intent": "production recall",
            },
            "candidate_refs": [candidate.record_id],
            "capture_ref": decision_id,
            "captured_at": "2026-01-01T00:00:00+00:00",
            "collector": "proactive_audit_capture",
        },
        source=PENDING_SOURCE,
        source_id=source_id,
        scope=exact,
        status="active",
        evidence=[candidate.record_id],
        meta={
            "report_type": "production_recall_pending_case",
            "schema": PENDING_QUERY_SCHEMA,
            "channel": channel,
            "capture_ref": decision_id,
            "query_digest": capture_digest,
        },
    )
    pending.record_id = "prqp_" + _stable_digest(
        {"schema": PENDING_QUERY_SCHEMA, "decision_id": decision_id, "query_digest": capture_digest}
    )[:32]
    runtime.store.append(pending)
    labeler = "operator"
    grade = 3
    packet_digest = "d" * 64
    label = RecordEnvelope.create(
        kind="evaluation_packet",
        title="Synthetic label",
        summary="Fingerprint shaped but not decision backed.",
        content={
            "evidence_class": "operator_relevance_label",
            "labeler": labeler,
            "pending_record_id": pending.record_id,
            "record_ref": candidate.record_id,
            "grade": grade,
            "operator_packet_evidence": {
                "schema": "secure_dataset_fingerprint.v1",
                "digest": packet_digest,
                "size": 512,
                "device": 1,
                "inode": 1,
            },
        },
        source=LABEL_EVIDENCE_SOURCE,
        source_id=source_id,
        scope=exact,
        status="active",
        evidence=[pending.record_id, candidate.record_id],
        meta={
            "report_type": "production_recall_label_evidence",
            "authoritative": True,
            "operator_packet_digest": packet_digest,
        },
    )
    label.record_id = "prle_" + _stable_digest(
        {
            "pending_record_id": pending.record_id,
            "record_ref": candidate.record_id,
            "grade": grade,
            "labeler": labeler,
        }
    )[:32]
    runtime.store.append(label)
    features = {
        "terms": ["codex", "verified", "release", "production"],
        "intent": "memory recall",
    }
    case = {
        "case_id": case_id,
        "collection_window": {
            "started_at": "2026-01-01T00:00:00+00:00",
            "ended_at": "2026-01-02T00:00:00+00:00",
        },
        "channel": channel,
        "source_id": source_id,
        "scope": exact_dict,
        "query_features": features,
        "query_digest": _stable_digest(features),
        "labels": [
            {
                "record_ref": candidate.record_id,
                "grade": grade,
                "accepted": True,
                "provenance": {
                    "labeler": labeler,
                    "labelled_at": "2026-01-02T00:00:00+00:00",
                    "evidence_ref": label.record_id,
                },
            }
        ],
        "provenance": {"collector": "proactive_audit_capture", "capture_ref": decision_id},
    }
    accepted = RecordEnvelope.create(
        kind="evaluation_packet",
        title="Synthetic accepted",
        summary="Complete-looking synthetic chain.",
        content={"schema": ACCEPTED_QUERY_SCHEMA, "case": case},
        source=ACCEPTED_SOURCE,
        source_id=source_id,
        scope=exact,
        status="active",
        evidence=[pending.record_id, candidate.record_id],
        meta={
            "report_type": "production_recall_accepted_case",
            "schema": ACCEPTED_QUERY_SCHEMA,
            "channel": channel,
            "case_id": case_id,
        },
    )
    accepted.record_id = "prqa_" + _stable_digest(
        {"schema": ACCEPTED_QUERY_SCHEMA, "case": case}
    )[:32]
    runtime.store.append(accepted)

    dataset = build_production_query_dataset(runtime, scope=BASE_SCOPE)

    assert dataset["progress"]["accepted_case_count"] == 0
    assert dataset["progress"]["per_channel_accepted"][channel] == 0
    assert dataset["ready"] is False
    runtime.close()


def test_production_query_cli_collect_accept_and_status_without_raw_echo(
    tmp_path,
    monkeypatch,
    capsys,
    trusted_dataset_path_ancestors,
) -> None:
    root = tmp_path / "runtime"
    runtime = Runtime.create(root=root)
    expected = _seed_decision(runtime, channel="codex", index=7)
    runtime.close()
    monkeypatch.setenv("EIMEMORY_ROOT", str(root))
    scope_args = [
        "--scope-agent", BASE_SCOPE["agent_id"],
        "--scope-workspace", BASE_SCOPE["workspace_id"],
        "--scope-user", BASE_SCOPE["user_id"],
    ]

    assert cli_main(["eval", "production-query", "collect", *scope_args]) == 0
    collected_text = capsys.readouterr().out
    collected = json.loads(collected_text)
    assert "raw secret query" not in collected_text
    pending_id = collected["pending_record_ids"][0]

    packet = tmp_path / "operator-label.json"
    packet.write_text(
        json.dumps(
            {
                "query_features": {"terms": ["codex", "verified", "release"], "intent": "memory recall"},
                "labels": [{"record_ref": expected.record_id, "grade": 3}],
                "labeler": "operator",
            }
        ),
        encoding="utf-8",
    )
    packet.chmod(0o600)
    assert cli_main(
        ["eval", "production-query", "accept", pending_id, "--label-json", str(packet), *scope_args]
    ) == 0
    accepted_text = capsys.readouterr().out
    assert "query_features" not in accepted_text
    assert "raw secret query" not in accepted_text

    assert cli_main(["eval", "production-query", "status", *scope_args]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["ready"] is False
    assert status["progress"]["per_channel_accepted"]["codex"] == 1
