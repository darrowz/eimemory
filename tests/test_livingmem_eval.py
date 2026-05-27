from __future__ import annotations

import json

from eimemory.api.runtime import Runtime
from eimemory.cli.main import main as cli_main
from eimemory.evaluation.livingmem import run_livingmem_eval
from eimemory.governance.snapshot import build_governance_snapshot
from eimemory.models.records import RecordEnvelope, ScopeRef


def _scope() -> dict[str, str]:
    return {"agent_id": "hongtu", "workspace_id": "living", "user_id": "darrow"}


def _dataset(scope: dict | None = None) -> dict:
    return {
        "name": "living-smoke",
        "scope": scope or _scope(),
        "seed": [
            {
                "id": "concise-style",
                "title": "Concise style",
                "text": "Prefer concise answers. No fluff, get straight to the point today.",
                "memory_type": "preference",
            },
            {
                "id": "repair-boundary",
                "title": "Repair boundary",
                "text": "You broke my trust by ignoring the boundary again; repair this before proceeding.",
                "memory_type": "preference",
            },
        ],
        "cases": [
            {
                "id": "temporal-motive-posture",
                "seed_id": "concise-style",
                "expect_temporal": {"temporal_distance": "present"},
                "expect_motive": {"motive": "efficiency"},
                "expect_affective": {"valence": "neutral"},
                "expect_posture": {"naturalness": "concise"},
                "expect_stale": False,
            },
            {
                "id": "repair",
                "seed_id": "repair-boundary",
                "expect_motive": {"motive": "trust_repair"},
                "expect_affective": {"repair_needed": True},
                "expect_repair_needed": True,
                "expect_posture": {"recommended": "act"},
            },
        ],
    }


def test_livingmem_eval_computes_living_metrics(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    report = run_livingmem_eval(runtime, _dataset())

    assert report["ok"] is True
    assert report["report_type"] == "livingmem_eval"
    assert report["sample_count"] == 2
    assert report["pass_rate"] == 1.0
    assert report["temporal_accuracy"] == 1.0
    assert report["motive_accuracy"] == 1.0
    assert report["affective_grounding"] == 1.0
    assert report["repair_recall"] == 1.0
    assert report["stale_label_avoidance"] == 1.0
    assert report["posture_accuracy"] == 1.0


def test_cli_eval_living_writes_report_file(tmp_path, monkeypatch, capsys) -> None:
    root = tmp_path / "runtime"
    monkeypatch.setenv("EIMEMORY_ROOT", str(root))
    dataset_path = tmp_path / "living.json"
    output_path = tmp_path / "living-report.json"
    dataset_path.write_text(json.dumps(_dataset(), ensure_ascii=False), encoding="utf-8")

    assert cli_main(["eval", "living", str(dataset_path), "--output", str(output_path)]) == 0
    printed = json.loads(capsys.readouterr().out)
    written = json.loads(output_path.read_text(encoding="utf-8"))

    assert printed["output"] == str(output_path)
    assert written["report_type"] == "livingmem_eval"
    assert written["sample_count"] == 2
    assert written["motive_accuracy"] == 1.0


def test_governance_snapshot_surfaces_living_memory_metrics(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = _scope()
    scope_ref = ScopeRef.from_dict(scope)
    runtime.store.append(
        RecordEnvelope.create(
            kind="memory",
            title="Future repair",
            summary="Repair the boundary tomorrow.",
            scope=scope_ref,
            meta={
                "living_memory_v1": {
                    "temporal": {
                        "life_phase": "transition",
                        "future_intent": {"status": "open", "intent": "repair the boundary"},
                    },
                    "affective": {"repair_needed": True},
                    "action_posture": {"ripeness": "high"},
                }
            },
        )
    )

    snapshot = build_governance_snapshot(runtime, scope)

    assert snapshot["living_memory"]["enriched_count"] == 1
    assert snapshot["living_memory"]["repair_needed_count"] == 1
    assert snapshot["living_memory"]["future_intent_count"] == 1
    assert snapshot["living_memory"]["by_life_phase"] == {"transition": 1}
    assert snapshot["living_memory"]["average_ripeness"] == 1.0


def test_living_cli_commands_are_safe_on_empty_store(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path / "runtime"))

    assert cli_main(["living", "enrich", "--limit", "5"]) == 0
    enrich_payload = json.loads(capsys.readouterr().out)
    assert enrich_payload["ok"] is True
    assert enrich_payload["enriched_count"] == 0

    assert cli_main(["living", "timeline"]) == 0
    timeline_payload = json.loads(capsys.readouterr().out)
    assert timeline_payload["ok"] is True
    assert timeline_payload["record_count"] == 0

    assert cli_main(["living", "posture", "repair before proceeding"]) == 0
    posture_payload = json.loads(capsys.readouterr().out)
    assert posture_payload["ok"] is True
    assert posture_payload["record_count"] == 0


def test_runtime_living_methods_enrich_timeline_and_recommend_posture(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = _scope()
    runtime.store.append(
        RecordEnvelope.create(
            kind="memory",
            title="Legacy repair preference",
            summary="You broke my trust; repair this before proceeding.",
            scope=ScopeRef.from_dict(scope),
            meta={"force_capture": True},
        )
    )

    enrich_report = runtime.enrich_living_memory(scope=scope, limit=10)
    timeline = runtime.build_living_timeline(scope=scope, limit=10)
    posture = runtime.recommend_action_posture("repair trust", scope=scope, limit=5)

    assert enrich_report["enriched_count"] == 1
    assert timeline["repair_needed_count"] == 1
    assert posture["items"][0]["posture"]["recommended"] == "act"


def test_auto_enriched_future_intent_surfaces_in_timeline_and_governance(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = _scope()
    runtime.memory.ingest(
        text="Tomorrow repair this boundary before proceeding.",
        memory_type="preference",
        title="Future repair boundary",
        scope=scope,
        force_capture=True,
    )

    timeline = runtime.build_living_timeline(scope=scope, limit=10)
    snapshot = build_governance_snapshot(runtime, scope)

    assert timeline["future_intent_count"] == 1
    assert timeline["future_intents"][0]["intent"] == "Tomorrow repair this boundary before proceeding."
    assert snapshot["living_memory"]["future_intent_count"] == 1
