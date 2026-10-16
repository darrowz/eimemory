from types import SimpleNamespace

import eimemory.evaluation.query_input_vault as vault
from eimemory.evaluation import real_query_gate as g
from eimemory.evaluation.real_query_schema import _stable_digest


def _case(ref, raw, agent="hongrui"):
    digest = vault.query_text_digest(raw)
    return {
        "case_id": "real-" + _stable_digest({"decision_id": ref, "query_digest": digest})[:24],
        "channel": "hermes", "source_id": "hermes",
        "scope": {"tenant_id": "default", "agent_id": agent, "workspace_id": "w", "user_id": "u"},
        "provenance": {"collector": "x", "capture_ref": ref},
    }


def _patch(monkeypatch, store, legacy=("hongtu",)):
    def fake(runtime, *, decision_id, scope, channel, source_id):
        key = (decision_id, scope.agent_id)
        if key not in store:
            raise ValueError("missing")
        return store[key]
    monkeypatch.setattr(vault, "load_query_input", fake)
    import eimemory.identity as ident
    monkeypatch.setattr(ident, "legacy_agent_ids", lambda: list(legacy))
    monkeypatch.setattr(ident, "canonical_agent_id", lambda a: "hongrui" if a in legacy else a)


def test_bound_query_via_legacy_agent(monkeypatch):
    raw = "原始问题"
    _patch(monkeypatch, {("pd:1", "hongtu"): {"query": raw, "effective_query": "有效问题"}})
    assert g._vault_bound_replay_query(SimpleNamespace(), _case("pd:1", raw)) == "有效问题"


def test_mismatched_binding_falls_back(monkeypatch):
    _patch(monkeypatch, {("pd:1", "hongrui"): {"query": "other", "effective_query": "x"}})
    assert g._vault_bound_replay_query(SimpleNamespace(), _case("pd:1", "原始问题")) == ""


def test_missing_vault_or_ref_falls_back(monkeypatch):
    _patch(monkeypatch, {})
    assert g._vault_bound_replay_query(SimpleNamespace(), _case("pd:1", "q")) == ""
    c = _case("pd:1", "q"); c["provenance"] = {}
    assert g._vault_bound_replay_query(SimpleNamespace(), c) == ""
