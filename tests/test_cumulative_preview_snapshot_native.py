from dataclasses import asdict
from types import SimpleNamespace

import pytest

from eimemory.api.runtime import Runtime
from eimemory.governance.learning import autonomous_learning, closed_loop, self_model
from eimemory.intake.connectors import FetchResult
from eimemory.models.records import ScopeRef
from eimemory.storage.runtime_store import RuntimeStore


class ForbiddenRuntime:
    def __getattribute__(self, name):
        raise AssertionError("preview touched runtime: " + name)


@pytest.mark.parametrize("entry", ["direct", "closed_loop", "controller", "runtime"])
def test_preview_does_not_touch_runtime_or_report_evidence(entry):
    runtime = ForbiddenRuntime()
    kwargs = dict(scope=ScopeRef(), dry_run=True, apply=True, allow_network=True, legacy_compatibility=True)
    if entry == "direct":
        result = autonomous_learning.run_autonomous_learning_cycle(runtime, **kwargs)
    elif entry == "closed_loop":
        result = closed_loop.autonomy_cycle(runtime, **kwargs)
    else:
        from eimemory.governance.learning.autonomy_controller import run_autonomy_cycle
        kwargs.pop("allow_network")
        kwargs.pop("legacy_compatibility")
        kwargs["smoke"] = True
        result = (run_autonomy_cycle(runtime, **kwargs) if entry == "controller"
                  else Runtime.run_autonomy_cycle(runtime, **kwargs))
    assert result["ok"] is None
    assert result["executed"] is False
    assert result["apply"] is False
    assert result["network_research"]["enabled"] is False
    assert result["eval_verdict"] == "not_run"
    assert result["candidate_id"] == ""


@pytest.mark.parametrize("ok", [True, False])
@pytest.mark.parametrize("persist", [True, False])
def test_source_preview_scan_write_is_guarded(monkeypatch, ok, persist):
    import eimemory.intake.connectors as connectors
    calls = []
    source = SimpleNamespace(source_id="fake-source", source_kind="url", config={})
    sources = SimpleNamespace(list_sources=lambda **kw: [source], mark_source_scanned=lambda *a, **kw: calls.append(kw))
    monkeypatch.setattr(connectors, "collect_from_source_entry", lambda *a, **kw: FetchResult(ok=ok, items=[], error="" if ok else "fake"))
    result = Runtime.collect_external_sources(SimpleNamespace(sources=sources), persist=persist)
    assert result["ok"] is ok
    assert len(calls) == int(persist)


def test_native_self_model_retry_tracks_content_and_preserves_old_snapshot(tmp_path, monkeypatch):
    import eimemory.governance.release.evidence_contract as contract
    monkeypatch.setattr(contract, "current_release_identity", lambda *a, **kw: None)
    store = RuntimeStore(tmp_path)
    try:
        runtime = SimpleNamespace(store=store)
        scope = ScopeRef(user_id="fake-person")
        model = {"scope": asdict(scope), "capabilities": [{"capability": "dynamic-cap", "score": .1}], "weaknesses": [], "metrics": {}}
        first = self_model.persist_self_model(runtime, model, scope=scope, loop_id="fake-loop")
        assert self_model.persist_self_model(runtime, model, scope=scope, loop_id="fake-loop") == first
        old = store.get_by_exact_ref(first["model_record_id"], scope=scope, source_id="default").to_dict()
        model["capabilities"][0]["score"] = .8
        second = self_model.persist_self_model(runtime, model, scope=scope, loop_id="fake-loop")
        assert second["model_record_id"] != first["model_record_id"]
        assert store.get_by_exact_ref(first["model_record_id"], scope=scope, source_id="default").to_dict() == old
        assert first["content_fingerprint"] != second["content_fingerprint"]
    finally:
        store.close()


@pytest.mark.parametrize("command", ["cycle", "autonomy"])
def test_cli_preview_returns_zero_without_claiming_success(tmp_path, monkeypatch, capsys, command):
    import json
    from eimemory.cli.main import main
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path))
    assert main(["learn", command, "--dry-run"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is None
    assert report["executed"] is False
    assert report["planned"] is True
