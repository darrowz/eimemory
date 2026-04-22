from __future__ import annotations

import json

from eimemory.cli.main import main as cli_main
from eimemory.scheduler.jobs import run_nightly_jobs


def test_intake_review_promote_policy_and_pack_cli_flow(tmp_path, monkeypatch, capsys) -> None:
    runtime_root = tmp_path / "runtime"
    doc = tmp_path / "source.md"
    pack_dir = tmp_path / "pack"
    doc.write_text(
        "Durable active intake knowledge can be reviewed, promoted, packed, and migrated.",
        encoding="utf-8",
    )
    monkeypatch.setenv("EIMEMORY_ROOT", str(runtime_root))

    assert cli_main(["source", "add", "--source-kind", "manual", "--title", "Durable note", "--uri", str(doc)]) == 0
    capsys.readouterr()
    assert cli_main(["intake", "run", "--persist"]) == 0
    run_report = json.loads(capsys.readouterr().out)
    fingerprint = run_report["candidates"][0]["fingerprint"]

    assert cli_main(["intake", "queue"]) == 0
    queue = json.loads(capsys.readouterr().out)
    candidate_id = queue[0]["record_id"]
    assert fingerprint[:12] in candidate_id

    assert cli_main(["intake", "review", candidate_id, "approve", "--reviewer", "tester"]) == 0
    reviewed = json.loads(capsys.readouterr().out)
    assert reviewed["status"] == "reviewed"

    assert cli_main(["intake", "promote", candidate_id, "--promoter", "tester"]) == 0
    promoted = json.loads(capsys.readouterr().out)
    assert promoted["kind"] == "memory"
    assert promoted["meta"]["promoted_from"] == candidate_id

    assert cli_main(["intake", "policy", "--gap", "active intake gaps"]) == 0
    policy = json.loads(capsys.readouterr().out)
    assert "active intake gaps" in policy["gap_queries"]

    assert cli_main(["intake", "pack", "export", str(pack_dir), "--include-candidates"]) == 0
    exported = json.loads(capsys.readouterr().out)
    assert exported["record_count"] >= 1

    assert cli_main(["intake", "pack", "import", str(pack_dir), "--dry-run"]) == 0
    imported = json.loads(capsys.readouterr().out)
    assert imported["dry_run"] is True
    assert imported["record_count"] == exported["record_count"]


def test_nightly_jobs_include_active_intake_reports(tmp_path) -> None:
    from eimemory.api.runtime import Runtime

    runtime = Runtime.create(root=tmp_path / "runtime")
    doc = tmp_path / "nightly.md"
    doc.write_text(
        "Nightly intake can safely persist durable knowledge candidates for later review.",
        encoding="utf-8",
    )
    runtime.sources.add_source(
        {
            "source_kind": "manual",
            "title": "Nightly source",
            "uri": str(doc),
            "enabled": True,
        }
    )

    report = run_nightly_jobs(runtime, scope={"agent_id": "main"})

    assert report["knowledge_intake"]["candidate_count"] == 1
    assert report["knowledge_intake"]["written_count"] == 1
    assert report["source_quality"]["source_count"] == 1


def test_nightly_jobs_do_not_reset_reviewed_candidates(tmp_path) -> None:
    from eimemory.api.runtime import Runtime

    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = {"agent_id": "main"}
    doc = tmp_path / "reviewed.md"
    doc.write_text(
        "Reviewed candidates should not be reset by the next nightly run.",
        encoding="utf-8",
    )
    runtime.sources.add_source(
        {
            "source_kind": "manual",
            "title": "Reviewed source",
            "uri": str(doc),
            "enabled": True,
        }
    )

    first = run_nightly_jobs(runtime, scope=scope)
    candidate = runtime.store.list_records(kinds=["knowledge_candidate"], scope=scope, limit=1)[0]
    runtime.review_intake_candidate(
        record_id=candidate.record_id,
        decision="approve",
        reviewer="tester",
        scope=scope,
    )
    second = run_nightly_jobs(runtime, scope=scope)
    reloaded = runtime.store.get_by_id(candidate.record_id)

    assert first["knowledge_intake"]["written_count"] == 1
    assert second["knowledge_intake"]["skipped_existing_count"] == 1
    assert reloaded.status == "reviewed"
