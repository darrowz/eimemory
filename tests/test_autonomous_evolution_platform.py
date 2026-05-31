from __future__ import annotations

import json

from eimemory.api.runtime import Runtime
from eimemory.cli.main import main as cli_main
from eimemory.scheduler.jobs import run_nightly_jobs


def _autonomous_evolution_report_fixture() -> dict:
    return {
        "ok": True,
        "report_type": "autonomous_evolution",
        "opportunity_count": 0,
        "autonomous_evolution_candidates": [],
    }


def _default_cli_scope() -> dict[str, str]:
    return {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}


def _module_available() -> bool:
    try:
        import eimemory.governance.autonomous_evolution  # noqa: F401

        return True
    except ModuleNotFoundError:
        return False


def test_runtime_run_autonomous_evolution_returns_report_type(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = _default_cli_scope()
    if not _module_available():
        monkeypatch.setattr(
            runtime,
            "run_autonomous_evolution",
            lambda **_: _autonomous_evolution_report_fixture(),
        )

    report = runtime.run_autonomous_evolution(scope=scope, apply=False, max_apply=3, persist_report=False)

    assert report["report_type"] == "autonomous_evolution"


def test_runtime_scout_web_learning_returns_scout_report(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = _default_cli_scope()

    report = runtime.scout_web_learning(
        scope=scope,
        evidence=[
            {
                "url": "https://example.com/replay",
                "title": "Replay first learning",
                "text": "External learning should become replay hypotheses before policy changes.",
            }
        ],
    )

    assert report["source"] == "web_learning_scout"
    assert report["hypothesis_count"] == 1


def test_nightly_jobs_include_autonomous_evolution(monkeypatch, tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = _default_cli_scope()
    captured: dict = {}

    def fake_run_autonomous_evolution(**kwargs):
        captured.update(kwargs)
        return _autonomous_evolution_report_fixture()

    monkeypatch.setattr(runtime, "run_autonomous_evolution", fake_run_autonomous_evolution)

    report = run_nightly_jobs(runtime, scope=scope)

    assert report["autonomous_evolution"]["ok"] is True
    assert report["autonomous_evolution"]["report_type"] == "autonomous_evolution"
    assert captured["apply"] is False
    assert captured["persist_report"] is True
    assert captured["max_apply"] == 3


def test_cli_evolve_autonomous_prints_report(monkeypatch, tmp_path, capsys) -> None:
    class _FakeRuntime:
        def run_autonomous_evolution(self, *, scope=None, apply=False, max_apply=3, web_hypotheses=None, persist_report=False):
            report = _autonomous_evolution_report_fixture()
            report["scope"] = scope
            return report

    monkeypatch.setattr("eimemory.cli.main.Runtime.create", lambda *_args, **_kwargs: _FakeRuntime())

    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path))
    exit_code = cli_main(
        [
            "evolve",
            "autonomous",
            "--scope-agent",
            "hongtu",
            "--scope-workspace",
            "embodied",
            "--scope-user",
            "Darrow",
            "--max-apply",
            "4",
            "--web-evidence-json",
            "[{\"trigger\":\"query\",\"policy_update\":\"test policy\"}]",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["report_type"] == "autonomous_evolution"
    assert output["scope"]["user_id"] == "darrow"


def test_cli_evolve_web_scout_prints_report(monkeypatch, tmp_path, capsys) -> None:
    class _FakeRuntime:
        def scout_web_learning(self, *, scope=None, urls=None, evidence=None, timeout_seconds=8):
            return {
                "ok": True,
                "source": "web_learning_scout",
                "scope": scope,
                "requested_urls": urls,
                "provided_evidence_count": len(evidence or []),
                "timeout_seconds": timeout_seconds,
                "hypotheses": [],
                "hypothesis_count": 0,
            }

    monkeypatch.setattr("eimemory.cli.main.Runtime.create", lambda *_args, **_kwargs: _FakeRuntime())
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path))

    exit_code = cli_main(
        [
            "evolve",
            "web-scout",
            "--scope-agent",
            "hongtu",
            "--scope-workspace",
            "embodied",
            "--scope-user",
            "darrow",
            "--url",
            "https://example.com/memory",
            "--evidence-json",
            "[{\"title\":\"Memory ranking\",\"text\":\"Use replay before promotion.\"}]",
            "--timeout-seconds",
            "5",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["source"] == "web_learning_scout"
    assert output["requested_urls"] == ["https://example.com/memory"]
    assert output["provided_evidence_count"] == 1
    assert output["timeout_seconds"] == 5
