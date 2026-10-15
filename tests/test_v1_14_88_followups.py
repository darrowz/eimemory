import json

from eimemory.ops.natural_outcomes import outcome_stats


def test_settings_fall_back_to_system_settings(tmp_path, monkeypatch):
    import importlib
    import eimemory.config.loader as loader

    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"root": str(tmp_path / "prod-root")}), encoding="utf-8")
    for key in ("EIMEMORY_CONFIG_PATH", "EIMEMORY_CONFIG_DIR", "EIMEMORY_ROOT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("EIMEMORY_SYSTEM_SETTINGS_PATH", str(settings))
    loader = importlib.reload(loader)
    try:
        assert str(loader.load_settings().root) == str(tmp_path / "prod-root")
    finally:
        monkeypatch.delenv("EIMEMORY_SYSTEM_SETTINGS_PATH")
        importlib.reload(loader)


def test_outcome_stats_errors_clearly_on_missing_store(tmp_path):
    report = outcome_stats(tmp_path / "missing.sqlite")
    assert report["ok"] is False and report["error"] == "store_not_found"
    assert report["db_path"].endswith("missing.sqlite")
