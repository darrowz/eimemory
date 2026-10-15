import json
from types import SimpleNamespace

from eimemory.scheduler import jobs


def _rt(tmp_path):
    return SimpleNamespace(store=SimpleNamespace(root=tmp_path))


def _write(path, agent):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"name": agent, "cases": [{"case_id": agent}]}), encoding="utf-8")


def test_env_map_selects_this_agent_only(tmp_path, monkeypatch):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    _write(a, "alpha"); _write(b, "beta")
    monkeypatch.setenv("EIMEMORY_PRODUCTION_RECALL_DATASETS", json.dumps({"alpha": str(a), "beta": str(b)}))
    monkeypatch.setattr(jobs, "_load_json_dataset", lambda p: json.loads(open(p).read()))
    ds, configured, source, _ = jobs._per_agent_production_recall_dataset(_rt(tmp_path), scope={"agent_id": "beta"})
    assert ds["name"] == "beta" and configured and source == "agent_env"


def test_discovery_path_per_agent(tmp_path, monkeypatch):
    monkeypatch.delenv("EIMEMORY_PRODUCTION_RECALL_DATASETS", raising=False)
    _write(tmp_path / "evaluation" / "agents" / "alpha" / "production_recall.json", "alpha")
    monkeypatch.setattr(jobs, "_load_json_dataset", lambda p: json.loads(open(p).read()))
    ds, _, source, _ = jobs._per_agent_production_recall_dataset(_rt(tmp_path), scope={"agent_id": "alpha"})
    assert ds["name"] == "alpha" and source == "agent_conventional_path"


def test_accepted_cases_used_when_ready(tmp_path, monkeypatch):
    monkeypatch.delenv("EIMEMORY_PRODUCTION_RECALL_DATASETS", raising=False)
    import eimemory.evaluation.production_query_dataset as pq
    monkeypatch.setattr(pq, "build_production_query_dataset",
                        lambda runtime, scope: {"ready": True, "dataset": {"cases": [{"case_id": "x"}]}})
    ds, _, source, _ = jobs._per_agent_production_recall_dataset(_rt(tmp_path), scope={"agent_id": "gamma"})
    assert source == "agent_accepted_cases" and ds["cases"][0]["case_id"] == "x"


def test_none_when_nothing_and_unsafe_agent(tmp_path, monkeypatch):
    monkeypatch.delenv("EIMEMORY_PRODUCTION_RECALL_DATASETS", raising=False)
    import eimemory.evaluation.production_query_dataset as pq
    monkeypatch.setattr(pq, "build_production_query_dataset", lambda runtime, scope: {"ready": False})
    assert jobs._per_agent_production_recall_dataset(_rt(tmp_path), scope={"agent_id": "gamma"}) is None
    assert jobs._per_agent_production_recall_dataset(_rt(tmp_path), scope={"agent_id": "../x"}) is None
