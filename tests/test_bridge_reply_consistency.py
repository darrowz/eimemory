"""Synthetic command text and observation display precedence."""
import json
from types import SimpleNamespace

import pytest

from eimemory.ei_bridge.agents import eibrain
from eimemory.ei_bridge.channels import openclaw_feishu as channel


@pytest.mark.parametrize("text", ["鸿途，休眠", "鸿途，sleep", "鸿途，结束对话"])
def test_explicit_sleep_beats_name_only_wake(text):
    assert channel._match_intent(text)[0] == "engagement.sleep"


@pytest.mark.parametrize("text", ["鸿途", "wake"])
def test_name_only_and_explicit_wake_remain_supported(text):
    assert channel._match_intent(text)[0] == "engagement.wake"


@pytest.mark.parametrize("age", [2, 7])
def test_unavailable_mode_is_not_overridden_by_age(age):
    payload = {"observation_mode": "unavailable", "raw": {"frame_age_s": age}, "scene": {"objects": ["person"]}}
    assert eibrain._observation_mode(payload, ["person"], "person nearby") == "unavailable"


@pytest.mark.parametrize("age", [float("nan"), float("inf"), -1, True])
def test_invalid_age_is_neither_displayed_nor_promoted_to_live(age):
    payload = {"raw": {"frame_age_s": age}}
    assert eibrain._frame_age_seconds(payload) is None
    assert eibrain._observation_mode(payload, ["person"], "person nearby") == "unavailable"


def test_invalid_raw_age_can_use_valid_freshness_age():
    payload = {"raw": {"frame_age_s": float("nan")}, "freshness": {"frame_age_s": 2.5}}
    assert eibrain._frame_age_seconds(payload) == 2.5


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_outer_text_does_not_mask_nested_text(blank):
    assert channel._extract_text({"text": blank, "message": {"text": "休眠"}}) == "休眠"


def test_nontext_json_content_does_not_become_command_text():
    assert channel._extract_text({"content": json.dumps({"status": "ok"})}) == ""


def test_nontext_json_content_can_fall_back_to_body():
    assert channel._extract_text({"content": json.dumps({"status": "ok"}), "body": "休眠"}) == "休眠"


def test_empty_vision_failure_uses_specific_fallback():
    result = SimpleNamespace(ok=False, summary="", audit={"capability": "vision.describe"}, error="unavailable")
    assert channel.format_reply(result) == "我这会儿还没拿到可用画面，不能把现场情况编出来。"


def test_other_empty_failure_keeps_generic_fallback():
    result = SimpleNamespace(ok=False, summary="", audit={}, error="failed")
    assert channel.format_reply(result).startswith("执行失败：请求未完成")
