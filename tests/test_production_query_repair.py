from __future__ import annotations

from hashlib import sha256
import json

from eimemory.adapters.runtime.channel import resolve_channel_scope
from eimemory.api.runtime import Runtime
from eimemory.evaluation.production_query_dataset import (
    ACCEPTED_SOURCE,
    LABEL_EVIDENCE_SOURCE,
    PENDING_SOURCE,
    accept_pending_production_query,
    build_production_query_dataset,
    collect_pending_production_queries,
)
from eimemory.evaluation.production_query_repair import repair_production_query_channel_scopes
from eimemory.identity_ops import repair_hongtu_identity
from eimemory.models.records import RecordEnvelope, ScopeRef


BASE_SCOPE = {"tenant_id": "default", "agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
LABEL_PACKET_EVIDENCE = {
    "schema": "secure_dataset_fingerprint.v1",
    "digest": "d" * 64,
    "size": 512,
    "device": 1,
    "inode": 1,
}
REPAIR_SOURCES = {PENDING_SOURCE, LABEL_EVIDENCE_SOURCE, ACCEPTED_SOURCE}


def _seed_accepted_cases(runtime: Runtime, *, channels: tuple[str, ...], total: int) -> None:
    expected: dict[tuple[str, int], RecordEnvelope] = {}
    for channel in channels:
        scope = resolve_channel_scope(channel, BASE_SCOPE)
        source_id = f"source-{channel}"
        for index in range(total):
            record = RecordEnvelope.create(
                kind="memory",
                title=f"{channel} verified release memory {index}",
                summary="safe durable evidence",
                source=f"{channel}.memory",
                source_id=source_id,
                scope=ScopeRef.from_dict(scope),
            )
            runtime.store.append(record)
            expected[(channel, index)] = record
            digest = sha256(f"production query {channel} {index}".encode()).hexdigest()
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
                        "release_version": "1.11.24",
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

    collected = collect_pending_production_queries(runtime, scope=BASE_SCOPE)
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


def _flatten_channel_evidence(runtime: Runtime, *, channels: tuple[str, ...]) -> list[str]:
    moved: list[str] = []
    for channel in channels:
        exact = ScopeRef.from_dict(resolve_channel_scope(channel, BASE_SCOPE))
        records = runtime.store.list_records(kinds=["evaluation_packet"], scope=exact, status="active", limit=500)
        for record in records:
            if record.source not in REPAIR_SOURCES:
                continue
            flattened = RecordEnvelope.from_dict(record.to_dict())
            flattened.scope = ScopeRef.from_dict(BASE_SCOPE)
            runtime.store.rewrite(flattened, previous_scope=exact)
            moved.append(record.record_id)
    return moved


def test_repair_restores_flattened_channel_evidence_and_is_idempotent(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    _seed_accepted_cases(runtime, channels=("openclaw", "codex", "hermes"), total=5)
    moved = _flatten_channel_evidence(runtime, channels=("codex", "hermes"))

    assert len(moved) == 30
    assert build_production_query_dataset(runtime, scope=BASE_SCOPE)["progress"]["per_channel_accepted"] == {
        "codex": 0,
        "hermes": 0,
        "openclaw": 5,
    }

    repaired = repair_production_query_channel_scopes(runtime, scope=BASE_SCOPE, persist_receipt=False)
    rerun = repair_production_query_channel_scopes(runtime, scope=BASE_SCOPE, persist_receipt=False)
    identity = repair_hongtu_identity(runtime, apply=True)
    dataset = build_production_query_dataset(runtime, scope=BASE_SCOPE)

    assert repaired["ok"] is True
    assert repaired["repaired_count"] == 30
    assert repaired["conflict_count"] == 0
    assert rerun["repaired_count"] == 0
    assert rerun["conflict_count"] == 0
    assert identity["repaired_count"] >= 30
    assert dataset["ready"] is True
    assert dataset["progress"]["per_channel_accepted"] == {"codex": 5, "hermes": 5, "openclaw": 5}
    assert "production query" not in json.dumps(repaired)
    runtime.close()


def test_repair_rejects_forged_embedded_channel_scope(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    _seed_accepted_cases(runtime, channels=("codex",), total=1)
    _flatten_channel_evidence(runtime, channels=("codex",))
    accepted = next(
        record
        for record in runtime.store.list_records(kinds=["evaluation_packet"], scope=BASE_SCOPE, status="active", limit=20)
        if record.source == ACCEPTED_SOURCE
    )
    forged = RecordEnvelope.from_dict(accepted.to_dict())
    forged.content["case"]["scope"] = resolve_channel_scope("hermes", BASE_SCOPE)
    runtime.store.rewrite(forged, previous_scope=ScopeRef.from_dict(BASE_SCOPE))

    result = repair_production_query_channel_scopes(runtime, scope=BASE_SCOPE, persist_receipt=False)

    assert result["ok"] is False
    assert result["conflict_count"] == 1
    assert runtime.store.get_by_id(accepted.record_id, scope=BASE_SCOPE) is not None
    assert runtime.store.get_by_id(accepted.record_id, scope=resolve_channel_scope("codex", BASE_SCOPE)) is None
    runtime.close()
