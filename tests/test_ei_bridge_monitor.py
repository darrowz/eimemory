from __future__ import annotations

from eimemory.ei_bridge.eibrain_monitor import _vision_payload


def test_vision_payload_treats_missing_frame_evidence_as_unavailable() -> None:
    payload = _vision_payload(
        {
            "system_health": "healthy",
            "visual_diagnostics": {
                "data_status": "live",
                "data_health": "healthy",
            },
        }
    )

    assert payload["observation_mode"] == "unavailable"
    assert payload["visual_status"] == "live"


def test_vision_payload_uses_worst_available_age_for_staleness() -> None:
    payload = _vision_payload(
        {
            "system_health": "healthy",
            "visual_diagnostics": {
                "data_status": "live",
                "data_health": "healthy",
                "frame_available": True,
                "scene_summary": "person in front of camera",
                "scene_labels": ["person"],
                "frame_age_s": 0.4,
                "state_age_s": 7.0,
            },
        }
    )

    assert payload["observation_mode"] == "stale"
    assert payload["freshness"]["frame_age_s"] == 0.4
    assert payload["freshness"]["state_age_s"] == 7.0
