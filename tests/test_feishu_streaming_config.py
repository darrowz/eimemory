import json

import pytest

from deploy.ensure_openclaw_bridge_config import (
    OpenClawBridgeConfigError,
    ensure_openclaw_bridge_config,
)


@pytest.mark.parametrize("legacy,mode", [(False, "off"), (True, "partial"), ("off", "off")])
def test_legacy_streaming_is_canonical_and_idempotent(tmp_path, legacy, mode):
    path = tmp_path / "openclaw.json"
    models = {"providers": {"custom": {"compat": {"legacy": True}}}}
    path.write_text(json.dumps({"models": models, "channels": {"feishu": {
        "streaming": legacy, "chunkMode": "length", "blockStreaming": False,
        "blockStreamingCoalesce": {"minChars": 10, "idleMs": 20, "enabled": True,
                                   "minDelayMs": 30, "maxDelayMs": 40},
        "appId": "keep", "accounts": {"main": {"appId": "also-keep"}},
    }}}), encoding="utf-8")
    ensure_openclaw_bridge_config(path)
    result = json.loads(path.read_text(encoding="utf-8"))
    feishu = result["channels"]["feishu"]
    assert feishu["streaming"] == {"mode": mode, "chunkMode": "newline", "block": {
        "enabled": True, "coalesce": {"minChars": 10, "idleMs": 20}}}
    assert not ({"blockStreaming", "blockStreamingCoalesce", "chunkMode", "streamMode"} & feishu.keys())
    assert result["models"] == models
    assert feishu["accounts"] == {"main": {"appId": "also-keep"}}
    assert feishu["appId"] == "keep"
    assert ensure_openclaw_bridge_config(path)["changed"] is False


def test_canonical_mode_and_coalesce_win_over_aliases(tmp_path):
    path = tmp_path / "openclaw.json"
    path.write_text(json.dumps({"channels": {"feishu": {
        "streamMode": "partial", "blockStreamingCoalesce": {"idleMs": 90},
        "streaming": {"mode": "off", "block": {"coalesce": {"idleMs": 12}}},
    }}}), encoding="utf-8")
    ensure_openclaw_bridge_config(path)
    feishu = json.loads(path.read_text())["channels"]["feishu"]
    assert feishu["streaming"] == {"mode": "off", "chunkMode": "newline", "block": {
        "enabled": True, "coalesce": {"idleMs": 12}}}
    assert "streamMode" not in feishu


@pytest.mark.parametrize("streaming", [[], 1, None, {"block": []}, {"mode": "bogus"}])
def test_invalid_streaming_is_not_overwritten(tmp_path, streaming):
    path = tmp_path / "openclaw.json"
    original = json.dumps({"channels": {"feishu": {"streaming": streaming}}})
    path.write_text(original, encoding="utf-8")
    with pytest.raises(OpenClawBridgeConfigError):
        ensure_openclaw_bridge_config(path)
    assert path.read_text(encoding="utf-8") == original
