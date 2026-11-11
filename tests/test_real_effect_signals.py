import json
import pytest

from eimemory.adapters.eibrain.rpc import EIBrainRPCBridge
from eimemory.adapters.hermes.provider_core import HermesMemoryProviderCore
from eimemory.adapters.hermes.effect_observer import tool_status
from eimemory.api.runtime import Runtime
from eimemory.retrieval.proactive import ProactiveRecallService
from test_hermes_multiplex_context import native_profiles


@pytest.fixture
def live(tmp_path, trusted_dataset_path_ancestors):
    runtime = Runtime.create(root=tmp_path / "store")
    runtime.proactive = ProactiveRecallService(runtime, control_percent=0, release_identity={
        "release_commit": "a" * 40, "release_version": "1.14.53",
        "deployment_receipt_id": "receipt", "release_session_id": "release"})
    bridge = EIBrainRPCBridge(runtime)
    calls = []

    class Client:
        producer = ""
        unavailable = False

        def call_or_bypass(self, method, params):
            calls.append((method, params))
            if self.unavailable:
                return {"ok": False}
            return dict(bridge.handle({"method": method, "params": params}, attestation_producer=self.producer))

    client = Client()
    host = Client()
    host.producer = "hermes"
    provider = HermesMemoryProviderCore(client=client, attestation_client=host)
    provider.initialize("signal-session", agent_workspace="signal-workspace", user_id="tester", hermes_home=str(tmp_path / "hermes"))
    yield runtime, bridge, provider, host, calls
    provider.shutdown()
    runtime.close()


def turn(provider, query, turn_id, result=None, **kwargs):
    provider.on_pre_llm_call(user_message=query, session_id="signal-session", turn_id=turn_id)
    provider.prefetch(query, session_id="signal-session")
    if result is not None:
        provider.observe_tool_execution(session_id="signal-session", turn_id=turn_id, tool_call_id="tool-call", result=result)
    provider.on_post_llm_call(user_message=query, assistant_message="Done", session_id="signal-session", turn_id=turn_id, **kwargs)
    provider._effects.flush()


def rows(runtime):
    return [json.loads(r[0]) for r in runtime.store.sqlite.conn.execute("SELECT payload_json FROM proactive_effect_signals ORDER BY rowid")]


def test_real_callbacks_bind_negative_signals_without_inventing_task_success(live):
    runtime, bridge, provider, host, calls = live
    secret_text = "查询我的私密订单 secret-987"
    turn(provider, secret_text, "h1", {"exit_code": 1, "error": "private error"})
    first = rows(runtime)[0]
    assert first["labels"]["tool_chain"] == "failed"
    assert first["labels"]["task_success"] == "unknown"
    assert first["verified_task_outcome"] is False
    assert first["release_identity"]["release_commit"] == "a" * 40
    provider.on_pre_llm_call(user_message="不对，我说的是其他订单", session_id="signal-session", turn_id="h2")
    provider.on_pre_llm_call(user_message="不对，我说的是其他订单", session_id="signal-session", turn_id="h2")
    provider._effects.flush()
    assert len(rows(runtime)) == 2
    assert rows(runtime)[1]["decision_id"] == first["decision_id"]
    assert rows(runtime)[1]["labels"] == {"correction": "suspected", "reask": "none"}
    assert provider.on_user_feedback(session_id="signal-session", turn_id="h1", rating="negative", event_id="vote1")
    assert provider.on_user_feedback(session_id="signal-session", turn_id="h1", rating="negative", event_id="vote1")
    provider._effects.flush()
    assert not provider.on_user_feedback(session_id="other", turn_id="h1", rating="positive", event_id="vote2")
    assert len(rows(runtime)) == 3
    stored = json.dumps(rows(runtime), ensure_ascii=False)
    for private in (secret_text, "不对，我说的是", "private error", "Done"):
        assert private not in stored
        assert private not in provider._effects.path.read_text()
    decision = runtime.store.load_proactive_decision(first["decision_id"])
    assert decision["terminal"] and not decision["outcome_verified"] and decision["outcome_success"] is None


def test_quick_reask_and_tool_success_are_only_observations(live):
    runtime, bridge, provider, host, calls = live
    turn(provider, "相同问题", "h1", {"ok": True})
    provider.on_pre_llm_call(user_message="相同问题？", session_id="signal-session", turn_id="h2")
    provider._effects.flush()
    assert rows(runtime)[0]["labels"]["tool_chain"] == "succeeded"
    assert rows(runtime)[0]["labels"]["task_success"] == "unknown"
    assert rows(runtime)[1]["labels"]["reask"] == "suspected"


def test_cron_operator_decision_never_queues_natural_effect_signals(live, monkeypatch):
    from test_hermes_single_gateway_acceptance import operator_topology, declared
    from eimemory.adapters.runtime.channel import resolve_channel_scope
    from eimemory.retrieval.effect_signals import record_signal

    runtime, bridge, provider, host, calls = live
    topology = operator_topology()
    provider._scope = dict(topology['profiles'][0]['operator_scope'])
    declared(monkeypatch, topology)
    # Serialize delivery so two cron-end flush attempts exercise the actual
    # rejection/retry boundary deterministically without a network worker.
    monkeypatch.setattr(provider._effects, 'request_flush', lambda: None)
    turn(provider, 'Synthetic scheduled health query', 'cron-end')
    provider._effects.flush()
    prefetch = next(p for m, p in calls if m == 'adapter.proactive_prefetch')
    stored = runtime.store.find_proactive_decision({
        'channel': 'hermes', 'scope': resolve_channel_scope('hermes', provider._scope),
        'source_key': runtime.proactive._source_digest(('hermes',)),
        'session_id': 'signal-session', 'turn_id': prefetch['turn_id'],
        'release_identity': runtime.proactive._current_release(provider._scope, channel='hermes'),
    })
    assert stored and stored['acceptance_generated'] is True
    assert stored['terminal'] is True
    # The server exclusion remains independently enforced even if a client
    # ignores the returned sampling classification.
    with pytest.raises(ValueError, match='effect_signal_acceptance_decision_rejected'):
        record_signal(runtime.store, channel='hermes', scope=stored['scope'], source_ids=stored['source_ids'],
            session_id=stored['session_id'], turn_id=stored['turn_id'], decision_id=stored['decision_id'],
            phase='turn_completed', event_id='fixture-host', labels={'tool_chain': 'not_run', 'task_success': 'unknown'})
    assert not [p for m, p in calls if m == 'adapter.proactive_signal']
    assert provider._effects.status()['pending'] == 0
    assert rows(runtime) == []


def test_native_cron_owner_excludes_signals_and_recovers_legacy_retry(live, native_profiles, monkeypatch):
    from eimemory.adapters.hermes.effect_observer import EffectObserver
    runtime, bridge, _provider, host, calls = live
    with native_profiles.activate('default', user=''):
        provider = HermesMemoryProviderCore(client=_provider._client, attestation_client=host)
        provider.initialize('signal-session', hermes_home=str(native_profiles.homes['default']),
                            platform='cron', agent_context='cron')
        try:
            assert provider._host_binding['binding_kind'] == 'operator'
            assert provider._common_params()['runtime_profile_binding']['natural_sample_eligible'] is False
            monkeypatch.setattr(provider._effects, 'request_flush', lambda: None)
            provider.on_pre_llm_call(user_message='Synthetic cron health', session_id='signal-session', turn_id='cron')
            provider.prefetch('Synthetic cron health', session_id='signal-session')
            pending = next(iter(provider._pending_proactive.values()))
            assert pending['acceptance_generated'] is True
            provider.on_post_llm_call(user_message='Synthetic cron health', assistant_message='Done',
                                      session_id='signal-session', turn_id='cron')
            provider._effects.flush()
            provider._effects.flush()
            assert not [p for m, p in calls if m == 'adapter.proactive_signal']
            # Seed only synthetic old-format retry state; use the actual
            # decision from this cron invocation and the real RPC rejection.
            namespace = {k: pending[k] for k in ('scope', 'source_ids', 'decision_id', 'session_id')}
            namespace.update(channel='hermes', turn_id=pending['decision_turn_id'])
            provider._effects.emit(namespace, 'turn_completed', 'fixture-legacy-host',
                                   {'tool_chain': 'not_run', 'task_success': 'unknown'})
            provider._effects = EffectObserver(provider)
            host.producer = 'codex'
            provider._effects.flush()
            assert provider._effects.status()['pending'] == 1
            host.producer = 'hermes'
            provider._effects.flush()
            assert provider._effects.status()['pending'] == 0
            assert provider._effects.status()['sent'] == 0
            assert provider._effects.status()['error'] == ''
            assert provider._effects.status()['excluded_acceptance_signals'] == 1
            assert json.loads(provider._effects.path.read_text()) == []
            assert rows(runtime) == []
        finally:
            host.producer = 'hermes'
            provider.shutdown()


def test_signal_auth_namespace_privacy_idempotency_and_conflict(live):
    runtime, bridge, provider, host, calls = live
    turn(provider, "query", "h1")
    params = next(p for m, p in calls if m == "adapter.proactive_signal")
    def rpc(p, producer="hermes"):
        return bridge.handle({"method": "adapter.proactive_signal", "params": p}, attestation_producer=producer)
    assert not rpc(params, "")["ok"]
    assert not rpc(params, "codex")["ok"]
    assert rpc(params)["result"]["replayed"]
    for mutation in ({"session_id": "wrong"}, {"source_ids": ["other"]}, {"turn_id": "wrong"},
                     {"labels": {"tool_chain": "failed", "raw_text": "private"}},
                     {"labels": {"latency_ms": float("nan")}},
                     {"labels": {"task_success": "failed"}}, {"summary": "private"}):
        assert not rpc({**params, **mutation})["ok"]
    assert len(rows(runtime)) == 1


def test_failed_send_survives_restart_and_retries_same_event(live):
    runtime, bridge, provider, host, calls = live
    host.unavailable = True
    turn(provider, "private query", "h1", {"ok": False})
    assert provider._effects.status()["pending"] == 1
    assert not rows(runtime)
    path = provider._effects.path
    from eimemory.adapters.hermes.effect_observer import EffectObserver
    provider._effects = EffectObserver(provider)
    host.unavailable = False
    provider._effects.flush()
    assert len(rows(runtime)) == 1
    assert provider._effects.status()["pending"] == 0
    assert json.loads(path.read_text()) == []


def test_real_hook_observes_exceptions_without_attestation_receipt(live, monkeypatch):
    from types import SimpleNamespace
    from integrations.hermes import eimemory_hook as hook
    runtime, bridge, provider, host, calls = live
    monkeypatch.setattr(hook, "get_hermes_provider", lambda session: provider if session == "signal-session" else None)
    monkeypatch.setattr(hook, "hermes_attestation_client_from_env", lambda **_kwargs: None)
    monkeypatch.setattr(hook, "register_code_implementation_task", lambda ctx: None)
    hooks, middleware = {}, {}
    ctx = SimpleNamespace(register_hook=lambda name, cb: hooks.update({name: cb}),
                          register_middleware=lambda name, cb: middleware.update({name: cb}))
    hook.register(ctx)
    ctx._manager = SimpleNamespace(_middleware={"tool_execution": [middleware["tool_execution"]]})
    hooks["pre_llm_call"](user_message="question", session_id="signal-session", turn_id="h1")
    provider.prefetch("question", session_id="signal-session")
    def failed(args):
        raise RuntimeError("private failure")
    with pytest.raises(RuntimeError, match="private failure"):
        middleware["tool_execution"]("tool", {}, failed, session_id="signal-session", turn_id="h1", tool_call_id="c1")
    hooks["post_llm_call"](user_message="question", assistant_message="failed", session_id="signal-session", turn_id="h1")
    provider._effects.flush()
    assert rows(runtime)[0]["labels"]["tool_chain"] == "failed"
    assert not [m for m, _ in calls if m == "adapter.attest_tool_result"]
    hooks["pre_llm_call"](user_message="不对", session_id="signal-session", turn_id="h2",
                           feedback_rating="negative", feedback_turn_id="h1", feedback_event_id="vote")
    provider._effects.flush()
    assert {r["phase"] for r in rows(runtime)} == {"turn_completed", "next_user", "explicit_rating"}


def test_missing_host_turn_id_still_collects_observations(live):
    runtime, bridge, provider, host, calls = live
    turn(provider, "without host turn", "", {"ok": True})
    assert rows(runtime)[0]["labels"]["tool_chain"] == "succeeded"
    provider.on_pre_llm_call(user_message="不对", session_id="signal-session")
    provider._effects.flush()
    assert rows(runtime)[1]["labels"]["correction"] == "suspected"


def test_http_signal_requires_host_producer_not_public_bearer(live):
    import urllib.request
    from urllib.error import HTTPError
    from eimemory.adapters.eibrain.rpc_server import EIBrainRPCServer
    runtime, bridge, provider, host, calls = live
    turn(provider, "question", "h1")
    params = next(p for m, p in calls if m == "adapter.proactive_signal")
    normal_token = "RuntimeSignalTestToken_0123456789-Strong"
    host_token = "HermesSignalTestToken_0123456789-Strong"
    other_token = "CodexSignalTestToken_0123456789-Strong"
    server = EIBrainRPCServer(runtime, host="127.0.0.1", port=0, auth_token=normal_token,
                             attestation_tokens={host_token: "hermes", other_token: "codex"})
    server.start()
    def post(token):
        request = urllib.request.Request(f"http://{server.address[0]}:{server.address[1]}/",
            data=json.dumps({"method": "adapter.proactive_signal", "params": params}).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status, json.loads(response.read())
        except HTTPError as exc:
            return exc.code, json.loads(exc.read())
    try:
        assert post(normal_token)[0] == 401
        assert post(other_token)[0] == 400
        status, result = post(host_token)
        assert status == 200 and result["result"]["replayed"] is True
    finally:
        server.stop()


@pytest.mark.parametrize("value,status", [("Everything worked", "unknown"), ({}, "unknown"),
    ({"ok": True, "exit_code": 4}, "failed"), ({"exit_code": False}, "unknown"),
    ({"success": False, "ok": True}, "failed"), ('{"exit_code":0}', "succeeded")])
def test_tool_result_requires_explicit_execution_fields(value, status):
    assert tool_status(value) == status
