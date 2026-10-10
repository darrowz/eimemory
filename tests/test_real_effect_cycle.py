from datetime import datetime, timedelta, timezone
import json

import pytest

from eimemory.api.runtime import Runtime
from eimemory.adapters.runtime.channel import resolve_channel_scope
from eimemory.models.records import RecordEnvelope, ScopeRef, RecallBundle
from eimemory.retrieval.proactive import ProactiveRecallService
from eimemory.retrieval.effect_signals import record_signal
from eimemory.governance.learning.effect_learning import run_effect_cycle, read_state, recall_policy_view
from eimemory.governance.learning.effect_policy import (
    issue_effect_policy, load_effect_policy, verify, RECEIPT_SCHEMA, stop_path, policy_path,
    write_private_json, digest,
)
from eimemory.governance.learning.effect_tick import run_effect_tick
from eimemory.governance.learning.effect_report import build_effect_report
from eimemory.governance.learning.effect_hypotheses import produce_effect_hypotheses

BASE = {"tenant_id": "default", "agent_id": "effects", "workspace_id": "workspace", "user_id": "user"}
SCOPE = resolve_channel_scope("hermes", BASE)
RELEASE = {"release_commit": "a" * 40, "release_version": "test", "deployment_receipt_id": "r", "release_session_id": "s"}


def configure(runtime):
    runtime.proactive = ProactiveRecallService(runtime, control_percent=0, release_identity=RELEASE)
    return runtime


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY", "effect-cycle-test-only-0123456789-ABCDEFGH")
    monkeypatch.delenv("EIMEMORY_REAL_EFFECT_POLICY_FILE", raising=False)
    monkeypatch.delenv("EIMEMORY_REAL_EFFECT_STOP", raising=False)
    value = configure(Runtime.create(root=tmp_path))
    yield value
    value.close()


def memory(runtime, **kwargs):
    return runtime.store.append(RecordEnvelope.create(kind="memory", title="Deployment preference",
        summary="Prefer the Borealis deployment command for my workspace.", scope=ScopeRef.from_dict(SCOPE),
        content={"text": "Prefer the Borealis deployment command for my workspace."}, **kwargs))


def observe(runtime, records, session, *, correction="suspected", turn="turn", latency=100, acceptance=False, confidence=.75):
    decision = runtime.proactive.decide(channel="hermes", scope=BASE, source_ids=["default"],
        session_id=session, query_id=turn, query="remember my previous deployment preference",
        recall_bundle=RecallBundle(items=records, rules=[], reflections=[], confidence=confidence, next_action_hint=""),
        acceptance_generated=acceptance)
    if acceptance:
        return decision
    params = {"channel": "hermes", "scope": SCOPE, "source_ids": ["default"], "session_id": session,
              "turn_id": turn, "decision_id": decision["decision_id"]}
    if decision["items"]:
        runtime.proactive.mark_injected(**params, injected_citations=[i["citation"] for i in decision["items"]], release_identity=decision["release_identity"])
    record_signal(runtime.store, **params, phase="turn_completed", event_id="completed",
                  labels={"tool_chain": "succeeded", "task_success": "unknown", "latency_ms": latency})
    record_signal(runtime.store, **params, phase="next_user", event_id="next",
                  labels={"correction": correction, "reask": "none"})
    return decision


def start(runtime, *, daily=3, records=None):
    records = records or [memory(runtime)]
    for i in range(10):
        assert observe(runtime, records, f"history-{i}")["items"]
    issue_effect_policy(runtime, scope=BASE, daily_changes=daily, min_trial_samples=10)
    preview = run_effect_cycle(runtime, scope=BASE)
    assert preview["status"] == "trial_started" and not preview["applied"]
    assert runtime.store.sqlite.conn.execute("SELECT COUNT(*) FROM real_effect_ledger").fetchone()[0] == 0
    applied = run_effect_cycle(runtime, scope=BASE, apply=True)
    assert applied["status"] == "trial_started" and applied["applied"]
    assert verify(applied["receipt"], RECEIPT_SCHEMA)
    return records, applied["proposed_state"]["trial"]


def arm_sessions(trial, arm, count=20):
    found = []
    for i in range(10000):
        session = f"trial-session-{i}"
        candidate = int(digest(["effect-session-assignment", trial["trial_id"], session])[:8], 16) % 100 < 25
        if candidate == (arm == "candidate"):
            found.append(session)
        if len(found) == count:
            return found
    raise AssertionError("session assignments unavailable")


def collect_trial(runtime, records, trial, *, improved=True, count=20, candidate_latency=100, confidence=.75):
    decisions = {}
    for arm in ("baseline", "candidate"):
        decisions[arm] = []
        for session in arm_sessions(trial, arm, count):
            bad = (arm == "baseline") == improved
            d = observe(runtime, records, session, correction="suspected" if bad else "none",
                        latency=candidate_latency if arm == "candidate" else 100, confidence=confidence)
            saved = runtime.store.load_proactive_decision(d["decision_id"])
            assert saved["retrieval_diagnostics"]["real_effect_policy"]["arm"] == arm
            decisions[arm].append(d)
    return decisions


def state(runtime):
    policy = load_effect_policy(runtime.store, channel="hermes", scope=BASE, source_ids=["default"], allow_stopped=True)
    return read_state(runtime.store, policy, full_chain=True)[0]


def test_real_path_canary_keep_restart_and_daily_ab(runtime):
    records, trial = start(runtime)
    ds = collect_trial(runtime, records, trial)
    assert ds["baseline"][0]["items"]
    candidate_items = ds["candidate"][0]["items"]
    assert not candidate_items
    report = build_effect_report(runtime, scope=BASE, report_date=datetime.now(timezone.utc).date().isoformat(), timezone="UTC")
    experiment = report["experiment_strata"][0]
    assert experiment["baseline"]["correction_rate"]["value"] == 1
    assert experiment["candidate"]["correction_rate"]["value"] == 0
    kept = run_effect_cycle(runtime, scope=BASE, apply=True)
    assert kept["status"] == "trial_kept" and kept["applied"]
    assert not kept["certifies_l5"] and not kept["code_changes_authorized"]
    committed = state(runtime)
    assert committed["revision"] == 2 and committed["trial"] is None
    assert next(iter(committed["weights"].values()))["weight"] == .85
    root = runtime.store.root
    runtime.close()
    restarted = configure(Runtime.create(root=root))
    try:
        assert state(restarted) == committed
        view = recall_policy_view(restarted.store, channel="hermes", scope=BASE, source_ids=["default"], session_id="new-session")
        assert view["weights"] == committed["weights"]
        assert run_effect_cycle(restarted, scope=BASE, apply=True)["status"] == "monitoring"
    finally:
        restarted.close()


@pytest.mark.parametrize("reason", ["regression", "latency", "stop", "timeout", "expired", "stale"])
def test_rollback_even_with_exhausted_daily_budget(runtime, reason):
    records, trial = start(runtime, daily=1)
    kwargs = {}
    if reason == "regression":
        collect_trial(runtime, records, trial, improved=False, count=10)
    elif reason == "latency":
        collect_trial(runtime, records, trial, count=10, candidate_latency=1000)
    elif reason == "stop":
        stop_path(runtime.store).touch(mode=0o600)
        view = recall_policy_view(runtime.store, channel="hermes", scope=BASE, source_ids=["default"], session_id=arm_sessions(trial, "candidate", 1)[0])
        assert not view["enabled"]
    elif reason in {"timeout", "expired"}:
        kwargs["at_time"] = (datetime.now(timezone.utc) + timedelta(days=8 if reason == "timeout" else 31)).isoformat()
    elif reason == "stale":
        records[0].summary = "Preference corrected by user."
        runtime.store.append(records[0])
    result = run_effect_cycle(runtime, scope=BASE, apply=True, **kwargs)
    assert result["status"] == "trial_rolled_back" and result["applied"]
    assert result["receipt"]["action"] == "trial_rolled_back"
    assert not state(runtime)["weights"] and state(runtime)["trial"] is None


def test_budget_blocks_keep_until_next_day(runtime):
    records, trial = start(runtime, daily=1)
    collect_trial(runtime, records, trial)
    blocked = run_effect_cycle(runtime, scope=BASE, apply=True)
    assert not blocked["applied"] and blocked["reason"] == "effect_daily_budget_exhausted"
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    assert run_effect_cycle(runtime, scope=BASE, apply=True, at_time=tomorrow)["status"] == "trial_kept"


def test_missing_samples_and_repeated_single_session_cannot_keep(runtime):
    records, trial = start(runtime)
    assert run_effect_cycle(runtime, scope=BASE, apply=True)["status"] == "observing"
    for arm in ("baseline", "candidate"):
        session = arm_sessions(trial, arm, 1)[0]
        for i in range(25):
            observe(runtime, records, session, turn=f"turn-{i}", correction="none" if arm == "candidate" else "suspected")
    result = run_effect_cycle(runtime, scope=BASE, apply=True)
    assert result["status"] == "observing"
    assert result["evaluation"]["primary"]["candidate"]["samples"] == 1


def test_signed_grant_namespace_and_forged_state_fail_closed(runtime):
    records, trial = start(runtime)
    for scope, sources in (({**BASE, "user_id": "other"}, ["default"]), (BASE, ["other"])):
        result = run_effect_cycle(runtime, scope=scope, source_ids=sources, apply=True)
        assert result["status"] == "blocked" and not result["applied"]
    assert not recall_policy_view(runtime.store, channel="hermes", scope=BASE, source_ids=["default"], session_id="test", acceptance_generated=True)["enabled"]
    conn = runtime.store.sqlite.conn
    payload = state(runtime)
    payload["threshold"] = .9
    conn.execute("UPDATE real_effect_states SET payload_json=?", (json.dumps(payload),))
    conn.commit()
    assert run_effect_cycle(runtime, scope=BASE, apply=True)["status"] == "blocked"
    assert not recall_policy_view(runtime.store, channel="hermes", scope=BASE, source_ids=["default"], session_id="test")["enabled"]


def test_policy_permissions_signature_and_wildcards(runtime, monkeypatch):
    issue_effect_policy(runtime, scope=BASE)
    path = policy_path(runtime.store, "hermes", BASE, ["default"])
    path.chmod(0o644)
    assert run_effect_cycle(runtime, scope=BASE)["status"] == "blocked"
    path.chmod(0o600)
    grant = json.loads(path.read_text())
    grant["limits"]["daily_changes"] = 100
    write_private_json(path, grant)
    assert run_effect_cycle(runtime, scope=BASE)["status"] == "blocked"
    monkeypatch.setenv("EIMEMORY_REAL_EFFECT_POLICY_FILE", str(path))
    with pytest.raises(ValueError, match="source"):
        issue_effect_policy(runtime, scope=BASE, source_ids=["*"])


def test_audit_write_failure_rolls_back_entire_trial(runtime, monkeypatch):
    record = memory(runtime)
    for i in range(10):
        observe(runtime, [record], f"history-{i}")
    issue_effect_policy(runtime, scope=BASE)
    real = runtime.store.sqlite.upsert
    def reject_audit(value, **kwargs):
        if value.source == "recall.real_effect_receipt":
            raise RuntimeError("audit storage unavailable")
        return real(value, **kwargs)
    monkeypatch.setattr(runtime.store.sqlite, "upsert", reject_audit)
    with pytest.raises(RuntimeError, match="audit storage"):
        run_effect_cycle(runtime, scope=BASE, apply=True)
    assert state(runtime)["revision"] == 0
    assert runtime.store.sqlite.conn.execute("SELECT COUNT(*) FROM real_effect_ledger").fetchone()[0] == 0


def test_threshold_trial_uses_multiple_memories_and_protects_mandatory(runtime):
    records = [memory(runtime) for _ in range(3)]
    for index, record in enumerate(records):
        for i in range(4):
            observe(runtime, [record], f"threshold-history-{index}-{i}")
    issue_effect_policy(runtime, scope=BASE, min_trial_samples=10)
    hypotheses = produce_effect_hypotheses(runtime, scope=BASE)
    global_h = next(h for h in hypotheses["hypotheses"] if h["target_ref"] is None)
    assert global_h["proposal"]["action"] == "raise_injection_threshold"
    threshold = run_effect_cycle(runtime, scope=BASE, apply=True)
    assert threshold["status"] == "trial_started" and threshold["proposed_state"]["trial"]["target_ref"] is None
    assert threshold["proposed_state"]["trial"]["candidate"]["threshold"] == .75
    hard = memory(runtime, meta={"hard_policy": True})
    new_trial = threshold["proposed_state"]["trial"]
    d = observe(runtime, [hard], arm_sessions(new_trial, "candidate", 1)[0], correction="none")
    assert d["items"][0]["mandatory"]


def test_tick_runs_only_signed_owners_and_is_idempotent(runtime):
    records, trial = start(runtime)
    tick = run_effect_tick(runtime)
    assert tick["ok"] and tick["reports"][0]["status"] == "observing" and tick["applied_count"] == 0
    bad = runtime.store.root / "state" / "real-effect-policy-forged.json"
    write_private_json(bad, {"channel": "hermes", "signature": "fake"})
    tick = run_effect_tick(runtime)
    assert not tick["ok"] and len(tick["reports"]) == 2


def test_committed_regression_restores_previous_policy(runtime):
    records, trial = start(runtime)
    collect_trial(runtime, records, trial)
    assert run_effect_cycle(runtime, scope=BASE, apply=True)["status"] == "trial_kept"
    for i in range(10):
        observe(runtime, records, f"post-promotion-{i}", correction="suspected")
    result = run_effect_cycle(runtime, scope=BASE, apply=True)
    assert result["status"] == "committed_rolled_back" and result["applied"]
    assert result["reason"] == "post_promotion_metric_regression"
    assert not state(runtime)["weights"] and state(runtime)["monitor"] is None


def test_admin_cli_is_wired_and_adapter_cannot_issue_grants(runtime, capsys):
    from eimemory.cli.main import _build_parser, dispatch
    from eimemory.adapters.eibrain.rpc import EIBrainRPCBridge
    parser = _build_parser()
    args = parser.parse_args(["learn", "effect-policy-issue", "--agent-id", BASE["agent_id"],
        "--workspace-id", BASE["workspace_id"], "--user-id", BASE["user_id"]])
    assert dispatch("learn", args, runtime, BASE) == 0
    assert json.loads(capsys.readouterr().out)["code_changes_authorized"] is False
    args = parser.parse_args(["effect-tick", "--dry-run"])
    assert dispatch("effect-tick", args, runtime, BASE)["reports"][0]["status"] == "awaiting_evidence"
    args = parser.parse_args(["learn", "effect-stop"])
    assert dispatch("learn", args, runtime, BASE) == 0 and stop_path(runtime.store).exists()
    bridge = EIBrainRPCBridge(runtime)
    assert not bridge.handle({"method": "adapter.issue_real_effect_policy", "params": {"scope": BASE}}, attestation_producer="hermes")["ok"]


def test_different_release_never_mixes_into_trial_evidence(runtime):
    records, trial = start(runtime)
    runtime.proactive = ProactiveRecallService(runtime, control_percent=0, release_identity={**RELEASE, "release_commit": "b" * 40})
    observe(runtime, records, "new-release-session", correction="none")
    result = run_effect_cycle(runtime, scope=BASE, apply=True)
    assert result["status"] == "trial_rolled_back" and result["reason"] == "trial_release_or_policy_changed"


def test_score_only_change_cannot_claim_effect_improvement(runtime):
    records, trial = start(runtime)
    collect_trial(runtime, records, trial, confidence=.99)
    result = run_effect_cycle(runtime, scope=BASE, apply=True)
    assert result["status"] == "observing" and not result["applied"]
    assert result["evaluation"]["candidate_changed_delivery_sessions"] == 0


def test_overlapping_controllers_cannot_double_apply(runtime):
    from concurrent.futures import ThreadPoolExecutor
    record = memory(runtime)
    for i in range(10):
        observe(runtime, [record], f"parallel-history-{i}")
    issue_effect_policy(runtime, scope=BASE)
    with ThreadPoolExecutor(max_workers=2) as pool:
        reports = list(pool.map(lambda _: run_effect_cycle(runtime, scope=BASE, apply=True), range(2)))
    assert sum(r["applied"] for r in reports) == 1
    assert state(runtime)["revision"] == 1
    assert runtime.store.sqlite.conn.execute("SELECT COUNT(*) FROM real_effect_ledger").fetchone()[0] == 1


def test_receipt_hash_chain_survives_retired_key_and_detects_old_tampering(runtime, monkeypatch):
    from eimemory.governance.tool_receipts import receipt_key_set, RECEIPT_KEYRING_FILE_ENV
    records, trial = start(runtime)
    old = receipt_key_set()
    second_key = "effect-rotation-second-0123456789-ABCDEFGH"
    third_key = "effect-rotation-third-0123456789-ABCDEFGH"
    ring = runtime.store.root / "state" / "test-keyring.json"
    # Use the production key-id helper so the test follows the supported format.
    from eimemory.governance.tool_receipts import _key_id
    def entry(secret):
        return {"key_id": _key_id(secret), "key": secret}
    write_private_json(ring, {"active": entry(second_key), "previous": [{"key_id": old.active_id, "key": old.active_key}]})
    monkeypatch.delenv("EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY")
    monkeypatch.setenv(RECEIPT_KEYRING_FILE_ENV, str(ring))
    stop_path(runtime.store).touch(mode=0o600)
    assert run_effect_cycle(runtime, scope=BASE, apply=True)["status"] == "trial_rolled_back"
    write_private_json(ring, {"active": entry(third_key), "previous": [entry(second_key)]})
    issue_effect_policy(runtime, scope=BASE)
    assert state(runtime)["revision"] == 2
    conn = runtime.store.sqlite.conn
    first = conn.execute("SELECT payload_json FROM real_effect_ledger WHERE sequence=1").fetchone()[0]
    changed = json.loads(first)
    changed["threshold"] = .9
    conn.execute("UPDATE real_effect_ledger SET payload_json=? WHERE sequence=1", (json.dumps(changed),))
    conn.commit()
    assert run_effect_cycle(runtime, scope=BASE, apply=True)["status"] == "blocked"
