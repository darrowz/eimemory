"""Regression coverage for post-deploy open items P1–P4 (2026-09-18)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from eimemory.evaluation.production_recall import evaluate_production_recall_quality_gate
from eimemory.governance.promotion_manager import _post_deploy_health_commands


REPO = Path(__file__).resolve().parents[1]


def test_p1_health_collect_exits_only_0_1_2_and_not_curl23(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import deploy.collect_release_health as collector

    calls: list[dict[str, Any]] = []

    def fake_fetch(url: str, *, timeout: float = 8.0) -> dict[str, Any]:
        calls.append({"url": url, "timeout": timeout})
        return {"ok": True, "commit": "a" * 40, "version": "1.13.15"}

    monkeypatch.setattr(collector._verify, "fetch_health", fake_fetch)

    report = collector.collect_release_health(url="http://127.0.0.1:8091/health", probe_only=True)
    assert report["ok"] is True
    assert collector.main(["--url", "http://127.0.0.1:8091/health", "--probe-only"]) == 0

    def fake_fetch_fail(url: str, *, timeout: float = 8.0) -> dict[str, Any]:
        return {"_fetch_error": "URLError"}

    monkeypatch.setattr(collector._verify, "fetch_health", fake_fetch_fail)
    assert collector.main(["--url", "http://127.0.0.1:8091/health", "--probe-only"]) == 1
    assert collector.main(["--url", "http://127.0.0.1:8091/health"]) == 2


def test_p1_default_post_deploy_health_avoids_curl_exit_23() -> None:
    commands = _post_deploy_health_commands({})
    assert commands
    flat = " ".join(str(part) for command in commands for part in command)
    assert "collect_release_health.py" in flat or "urllib.request" in flat
    assert "curl" not in flat


def test_p1_owner_check_script_remaps_health_failures_to_exit_1() -> None:
    script = (REPO / "deploy/check_user_systemd_owner.sh").read_text(encoding="utf-8")
    assert "collect_release_health.py" in script
    assert "_fail \"loopback_health_failed\"" in script
    assert "100.105.189.120" not in script


def test_p2_managed_dropins_overwrite_stale_release_dir_and_rpc_url() -> None:
    runtime = (REPO / "deploy/systemd/eimemory-python-runtime.conf").read_text(encoding="utf-8")
    hermes = (REPO / "deploy/systemd/hermes-gateway-eimemory.conf").read_text(encoding="utf-8")
    openclaw = (REPO / "deploy/systemd/openclaw-gateway-eimemory.conf").read_text(encoding="utf-8")
    assert "Environment=EIMEMORY_RUNTIME_RELEASE_DIR=/opt/eimemory/current" in runtime
    assert "Environment=EIMEMORY_RPC_URL=http://127.0.0.1:8091/" in runtime
    assert "Environment=EIMEMORY_RUNTIME_RELEASE_DIR=/opt/eimemory/current" in hermes
    assert "Environment=EIMEMORY_RPC_URL=http://127.0.0.1:8091/" in hermes
    assert "Environment=EIMEMORY_RUNTIME_RELEASE_DIR=/opt/eimemory/current" in openclaw
    assert "Environment=EIMEMORY_RPC_URL=http://127.0.0.1:8091/" in openclaw
    assert "100.105.189.120" not in openclaw


def test_p3_empty_sample_quality_gate_is_vacuous_ok_not_false_failure() -> None:
    gate = evaluate_production_recall_quality_gate({"sample_count": 0, "gate_status": "diagnostic"})
    assert gate["ok"] is True
    assert gate.get("vacuous") is True
    assert gate.get("skipped_reason") == "sample_starved_or_unconfigured"
    assert gate.get("blocked_reason") == ""
    assert gate.get("blocking_metrics") == {}


def test_p3_real_pollution_with_samples_still_fail_closed() -> None:
    gate = evaluate_production_recall_quality_gate(
        {
            "sample_count": 10,
            "hit_at_1": 1.0,
            "hit_at_5": 1.0,
            "false_recall_rate": 0.5,
            "forbidden_hit_rate": 0.0,
            "audit_pollution_rate": 0.0,
            "incident_pollution_rate": 0.0,
            "evolution_pollution_rate": 0.0,
            "stale_rule_pollution_rate": 0.0,
            "selected_record_pollution_rate": 0.0,
            "latency_ms_p95": 10.0,
            "cross_channel_leakage_count": 0,
            "source_filter_leakage_count": 0,
            "p_at_3": 1.0,
            "mrr": 1.0,
            "noise_rate": 0.0,
            "padding_rate": 0.0,
            "payload_bytes_top_1": 10,
            "payload_bytes_top_5": 10,
        }
    )
    assert gate["ok"] is False
    assert gate["blocked_reason"] == "recall_quality_gate_failed"
    assert "false_recall_rate" in gate["blocking_metrics"]


def test_p4_catalog_waiting_reason_distinguishes_incomplete_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from eimemory.ops import code_implementation_owner as owner

    class FakeCatalog:
        sealed = True

        def get_case(self, case_id: str) -> object:
            return object()

        def describe_executor(self, executor_id: str) -> object:
            return object()

    class FakeRuntime:
        capability_catalog = FakeCatalog()
        capabilities = object()
        catalog_bootstrap_error = ""

        class store:
            root = tmp_path

    monkeypatch.setattr(
        owner.provider_module,
        "code_implementation_catalog_activation_snapshot",
        lambda *args, **kwargs: {"catalog_passes": 0, "catalog_case_id": "case", "catalog_snapshot_digest": "d", "activation_state_digest": "a" * 64},
    )
    status = owner._catalog_status(FakeRuntime())
    assert status["status"] == "waiting"
    assert status["structural_ready"] is True
    assert status["valid_passes"] == 0
    assert status["required_passes"] == 2
    assert status["reason"] == "catalog_lifecycle_passes_incomplete"
