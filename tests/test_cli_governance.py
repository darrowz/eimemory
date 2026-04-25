from __future__ import annotations

import json

import eimemory.cli.main as cli_module
from eimemory.api.runtime import Runtime
from eimemory.cli.main import main as cli_main
from eimemory.identity import hongtu_scope


def _cli_scope() -> dict[str, str]:
    return hongtu_scope({})


def test_cli_governance_snapshot_reports_runtime_state(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path / "runtime"))
    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = _cli_scope()
    runtime.memory.ingest(
        text="Remember concise governance output",
        memory_type="fact",
        title="Governance memory",
        scope=scope,
    )
    runtime.evolution.store_rule(
        title="Governance rule",
        summary="Prefer concise output",
        task_type="chat.reply",
        retrieval_policy={"route_hint": "task_context_first"},
        scope=scope,
        status="active",
    )

    assert cli_main(["governance", "snapshot"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["scope"] == scope
    assert payload["memory_quality"]["memory_count"] == 1
    assert payload["rules"]["active_count"] == 1


def test_cli_evolve_evaluate_reads_dataset_file(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path / "runtime"))
    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = _cli_scope()
    runtime.memory.ingest(
        text="Prefer concise replies for operator prompts",
        memory_type="preference",
        title="Concise replies",
        scope=scope,
    )

    dataset_path = tmp_path / "dataset.json"
    dataset_path.write_text(
        json.dumps(
            [
                {
                    "query": "concise replies",
                    "scope": scope,
                    "task_context": {"task_type": "chat.reply"},
                    "expect_any_title": ["Concise replies"],
                },
                {
                    "query": "missing memory",
                    "scope": scope,
                    "task_context": {"task_type": "chat.reply"},
                    "expect_any_title": ["Missing memory"],
                },
            ]
        ),
        encoding="utf-8",
    )

    assert cli_main(["evolve", "evaluate", str(dataset_path), "--task-type", "chat.reply", "--profile", "balanced"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["sample_count"] == 2
    assert payload["hit_count"] == 1
    assert payload["profile"] == "balanced"
    assert payload["task_type"] == "chat.reply"


def test_cli_evolve_promotions_lists_candidates(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path / "runtime"))
    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = _cli_scope()
    runtime.memory.ingest(
        text="Passing target memory for promotion",
        memory_type="preference",
        title="Passing target",
        scope=scope,
    )
    rule = runtime.evolution.store_rule(
        title="Promotable rule",
        summary="Promote when replay passes",
        task_type="chat.reply",
        retrieval_policy={"route_hint": "task_context_first"},
        scope=scope,
        status="accepted",
    )
    runtime.evolution.feedback(
        target_ref={"kind": "rule", "record_id": rule.record_id},
        decision="accept",
        reason="Approved",
        reviewed_by="reviewer",
        scope=scope,
    )
    runtime.evolution.replay_rule(
        record_id=rule.record_id,
        dataset=[
            {
                "query": "passing target memory",
                "scope": scope,
                "task_context": {"task_type": "chat.reply"},
                "expect_any_title": ["Passing target"],
            }
        ],
    )

    assert cli_main(["evolve", "promotions", "--min-pass-rate", "0.8"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["candidate_count"] == 1
    assert payload["blocked_count"] == 0
    assert payload["candidates"][0]["record_id"] == rule.record_id


def test_cli_governance_console_creates_escaped_html_file(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path / "runtime"))
    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = _cli_scope()
    runtime.memory.ingest(
        text="Console snapshot memory <script>alert('x')</script>",
        memory_type="fact",
        title="Console memory <script>alert('x')</script>",
        scope=scope,
    )

    output_path = tmp_path / "console" / "evolution.html"

    assert cli_main(["governance", "console", "--output", str(output_path)]) == 0

    payload = json.loads(capsys.readouterr().out)
    rendered = output_path.read_text(encoding="utf-8")

    assert payload["ok"] is True
    assert payload["path"] == str(output_path)
    assert output_path.exists()
    assert "Evolution Console" in rendered
    assert "<script>alert('x')</script>" not in rendered


def test_cli_evolve_evaluate_rejects_invalid_dataset_json(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path / "runtime"))
    dataset_path = tmp_path / "dataset.json"
    dataset_path.write_text("{not-json", encoding="utf-8")

    assert cli_main(["evolve", "evaluate", str(dataset_path)]) == 2

    payload = json.loads(capsys.readouterr().out)
    assert payload == {"ok": False, "error": "invalid_dataset_json"}


def test_cli_evolve_evaluate_rejects_missing_dataset_file(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path / "runtime"))
    dataset_path = tmp_path / "missing.json"

    assert cli_main(["evolve", "evaluate", str(dataset_path)]) == 2

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"] == "dataset_unreadable"


def test_cli_evolve_evaluate_rejects_non_list_dataset(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path / "runtime"))
    dataset_path = tmp_path / "dataset.json"
    dataset_path.write_text(json.dumps({"query": "not a list"}), encoding="utf-8")

    assert cli_main(["evolve", "evaluate", str(dataset_path)]) == 2

    payload = json.loads(capsys.readouterr().out)
    assert payload == {"ok": False, "error": "dataset must be a list"}


def test_cli_evolve_promotions_rejects_out_of_range_threshold(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path / "runtime"))

    assert cli_main(["evolve", "promotions", "--min-pass-rate", "2"]) == 2

    payload = json.loads(capsys.readouterr().out)
    assert payload == {"ok": False, "error": "min_pass_rate_out_of_range"}


def test_cli_governance_console_reports_write_failure(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path / "runtime"))

    def fail_write(snapshot, path):
        raise OSError("disk full")

    monkeypatch.setattr(cli_module, "write_evolution_console", fail_write)

    assert cli_main(["governance", "console", "--output", str(tmp_path / "console.html")]) == 2

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"] == "console_write_failed"
