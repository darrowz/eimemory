from types import SimpleNamespace

from eimemory.governance.learning import replay_dataset as rd


def _case(cid, task_type):
    return {"case_id": cid, "source": "event_outcome", "task_type": task_type, "query": f"q {cid}",
            "input": f"q {cid}", "expected": f"do {cid}", "expected_text": [f"do {cid}"],
            "labels": ["bad", "memory.recall"], "target_capability": "memory.recall",
            "capability_attribution": {"status": "classified"}, "outcome": "bad", "quality_score": 0.9}


def test_operational_probe_outcomes_are_not_replay_cases(monkeypatch):
    names = [n for n in dir(rd) if n.startswith("_cases_from_") and n != "_cases_from_evaluation_catalog"]
    for n in names:
        monkeypatch.setattr(rd, n, lambda *a, **k: [])
    monkeypatch.setattr(rd, "_cases_from_replay_results", lambda *a, **k: [
        _case("real", "chat.reply"), _case("probe", "capability.acceptance"), _case("live", "live.acceptance.recall")])
    report = rd.build_replay_dataset(SimpleNamespace(), scope={"tenant_id": "t", "agent_id": "a", "workspace_id": "w", "user_id": "u"},
                                     persist=False, legacy_compatibility=True)
    ids = {c.get("case_id") for c in (report.get("cases") or report.get("dataset", {}).get("cases") or [])}
    assert "real" in ids, report
    assert "probe" not in ids and "live" not in ids
