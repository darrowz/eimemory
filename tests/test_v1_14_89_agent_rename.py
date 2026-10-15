import importlib
import json

from eimemory.models.records import ScopeRef


def _reload(monkeypatch, tmp_path, payload):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    for key in ("EIMEMORY_CONFIG_PATH", "EIMEMORY_CONFIG_DIR", "EIMEMORY_HONGTU_AGENT_ID", "EIMEMORY_LEGACY_AGENT_IDS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("EIMEMORY_SYSTEM_SETTINGS_PATH", str(path))
    import eimemory.identity as identity
    identity._SETTINGS_CACHE.clear()
    return importlib.reload(identity)


def test_renamed_agent_reads_legacy_scope_without_rewriting(monkeypatch, tmp_path):
    identity = _reload(monkeypatch, tmp_path, {"canonical_agent_id": "agent-new", "legacy_agent_ids": ["agent-old"]})
    try:
        assert identity.HONGTU_AGENT_ID == "agent-new"
        assert identity.canonical_agent_id("agent-old") == "agent-new"
        assert identity.canonical_agent_id("other") == "other"
        scopes = identity.hongtu_query_scopes(ScopeRef("default", "agent-new", "ws::channel::hermes", "u"))
        assert [(s.agent_id, s.workspace_id) for s in scopes] == [
            ("agent-new", "ws::channel::hermes"), ("agent-old", "ws::channel::hermes")]
        other = identity.hongtu_query_scopes(ScopeRef("default", "other", "ws", "u"))
        assert [s.agent_id for s in other] == ["other"]
    finally:
        monkeypatch.delenv("EIMEMORY_SYSTEM_SETTINGS_PATH")
        identity._SETTINGS_CACHE.clear()
        importlib.reload(identity)


def test_no_rename_configured_is_a_noop(monkeypatch, tmp_path):
    identity = _reload(monkeypatch, tmp_path, {})
    try:
        assert identity.legacy_agent_ids() == ()
        assert identity.canonical_agent_id("x") == "x"
    finally:
        monkeypatch.delenv("EIMEMORY_SYSTEM_SETTINGS_PATH")
        identity._SETTINGS_CACHE.clear()
        importlib.reload(identity)
