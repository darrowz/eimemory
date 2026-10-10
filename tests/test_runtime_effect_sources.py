import pytest

from eimemory.adapters.hermes.provider_core import _source_ids_from_env
from eimemory.adapters.runtime.sources import runtime_source_ids
from eimemory.governance.learning.effect_dataset import namespace
from eimemory.governance.learning.effect_policy import issue_effect_policy, load_effect_policy
from eimemory.governance.learning.effect_learning import run_effect_cycle
from eimemory.governance.learning.effect_report import build_effect_report
from test_real_effect_report import BASE, runtime, sample
from test_real_effect_signals import live, turn


def test_native_default_is_shared_by_producer_report_and_grant(runtime, monkeypatch):
    monkeypatch.delenv("EIMEMORY_SOURCE_IDS", raising=False)
    monkeypatch.setenv("EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY", "source-test-private-0123456789-ABCDEFGH")
    sample(runtime, 0, success="succeeded")
    assert namespace("hermes", BASE, None)[2] == _source_ids_from_env("default") == ["hermes"]
    assert build_effect_report(runtime, scope=BASE, report_date="2026-10-09")["metrics"]["decisions"] == 1
    issue_effect_policy(runtime, scope=BASE)
    assert load_effect_policy(runtime.store, channel="hermes", scope=BASE, source_ids=["hermes"])
    assert run_effect_cycle(runtime, scope=BASE)["status"] != "blocked"


def test_configured_namespace_and_explicit_legacy_grant_stay_exact(runtime, monkeypatch):
    monkeypatch.setenv("EIMEMORY_SOURCE_IDS", "project-a,default,project-a")
    assert namespace("hermes", BASE, None)[2] == _source_ids_from_env("default") == ["project-a", "default", "hermes"]
    sample(runtime, 0, source_ids=_source_ids_from_env("default"))
    assert build_effect_report(runtime, scope=BASE, report_date="2026-10-09")["metrics"]["decisions"] == 1
    assert namespace("hermes", BASE, ["default"])[2] == ["default"]
    assert runtime_source_ids("openclaw", []) == []
    monkeypatch.setenv("EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY", "source-test-private-0123456789-ABCDEFGH")
    issue_effect_policy(runtime, scope=BASE, source_ids=["default"])
    assert load_effect_policy(runtime.store, channel="hermes", scope=BASE, source_ids=["default"])
    assert run_effect_cycle(runtime, scope=BASE)["status"] == "blocked"


def test_invalid_config_and_empty_explicit_sources_are_rejected(monkeypatch):
    monkeypatch.setenv("EIMEMORY_SOURCE_IDS", "unsafe/source")
    with pytest.raises(ValueError):
        runtime_source_ids("hermes")
    with pytest.raises(ValueError, match="effect_sources_required"):
        namespace("hermes", BASE, [])


def test_native_hermes_callback_is_visible_to_default_report_and_attribution(live):
    from datetime import datetime, timezone
    from eimemory.governance.learning.effect_hypotheses import produce_effect_hypotheses
    runtime, bridge, provider, host, calls = live
    turn(provider, "private query", "native-turn", {"ok": False}, task_success=False)
    report = build_effect_report(runtime, scope=provider._scope,
                                report_date=datetime.now(timezone.utc).date().isoformat(), timezone="UTC")
    assert report["source_ids"] == ["hermes"]
    assert report["metrics"]["decisions"] == 1
    assert report["metrics"]["task_success_rate"]["value"] == 0
    attribution = produce_effect_hypotheses(runtime, scope=provider._scope)
    assert attribution["source_ids"] == ["hermes"]
    assert len(attribution["unattributed_failures"]) == 1


def test_cli_report_does_not_restore_wrong_default_namespace(runtime, monkeypatch, capsys):
    import json
    from eimemory.cli.main import main
    sample(runtime, 0, success="succeeded")
    monkeypatch.setenv("EIMEMORY_ROOT", str(runtime.store.root))
    assert main(["learn", "effect-report", "--agent-id", BASE["agent_id"],
                 "--workspace-id", BASE["workspace_id"], "--user-id", BASE["user_id"],
                 "--date", "2026-10-09"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["source_ids"] == ["hermes"] and report["metrics"]["decisions"] == 1
