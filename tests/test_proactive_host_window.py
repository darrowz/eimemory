"""1.14.24: proactive work is bounded by the host's delivery window."""
from time import perf_counter
from types import SimpleNamespace

from eimemory.core import budgets
from eimemory.retrieval import caller_assistance as assistance


def test_host_window_defaults_only_for_hermes(monkeypatch):
    monkeypatch.delenv("EIMEMORY_PROACTIVE_HOST_WINDOW_SECONDS", raising=False)
    assert budgets.proactive_host_window_seconds("hermes") == 8.0
    assert budgets.proactive_host_window_seconds("codex") == 0.0
    assert budgets.proactive_host_window_seconds("openclaw") == 0.0
    monkeypatch.setenv("EIMEMORY_PROACTIVE_HOST_WINDOW_SECONDS", "0")
    assert budgets.proactive_host_window_seconds("hermes") == 0.0
    monkeypatch.setenv("EIMEMORY_PROACTIVE_HOST_WINDOW_SECONDS", "1")
    assert budgets.proactive_host_window_seconds("hermes") == 2.0
    monkeypatch.setenv("EIMEMORY_PROACTIVE_HOST_WINDOW_SECONDS", "junk")
    assert budgets.proactive_host_window_seconds("hermes") == 8.0
    monkeypatch.setenv("EIMEMORY_PROACTIVE_HOST_WINDOW_SECONDS", "12")
    assert budgets.proactive_host_window_seconds("codex") == 0.0
    assert budgets.proactive_host_margin_seconds() == 0.75


def _client(seen):
    def complete(**_):
        seen.append(client.timeout_seconds)
        return SimpleNamespace(text='{"selected":[{"id":"0","quote":"Read the complete document"}]}')
    client = SimpleNamespace(timeout_seconds=90, complete=complete)
    return client


def test_verifier_completion_is_capped_by_host_deadline(monkeypatch):
    seen = []
    monkeypatch.setattr(assistance, 'llm_client_from_env', lambda _: _client(seen))
    record = SimpleNamespace(record_id='memory-1')
    cands = [(record, 'Read the complete document, not merely its title.')]
    with assistance.host_delivery_deadline(perf_counter() + 3.0):
        selected, report = assistance.verify_candidates(query='Must I skip the full document?',
                                                        candidates=cands, limit=1)
    assert selected == [record] and report['status'] == 'evidence_found'
    assert seen and seen[0] <= 3.0 and report.get('host_window_capped') is True


def test_verifier_skips_when_host_window_exhausted(monkeypatch):
    seen = []
    monkeypatch.setattr(assistance, 'llm_client_from_env', lambda _: _client(seen))
    record = SimpleNamespace(record_id='memory-1')
    with assistance.host_delivery_deadline(perf_counter() + 0.5):
        selected, report = assistance.verify_candidates(query='Must I skip the full document?',
            candidates=[(record, 'Read the complete document.')], limit=1)
    assert selected == [] and report['reason'] == 'host_window_exhausted' and not seen


def test_no_host_deadline_keeps_verifier_ceiling(monkeypatch):
    seen = []
    monkeypatch.setattr(assistance, 'llm_client_from_env', lambda _: _client(seen))
    record = SimpleNamespace(record_id='memory-1')
    assistance.verify_candidates(query='Must I skip the full document?',
        candidates=[(record, 'Read the complete document, not merely its title.')], limit=1)
    assert seen == [12.0]


def test_proactive_recall_passes_host_deadline_for_hermes(tmp_path, monkeypatch):
    from eimemory.api.runtime import Runtime
    monkeypatch.delenv("EIMEMORY_PROACTIVE_HOST_WINDOW_SECONDS", raising=False)
    runtime = Runtime.create(root=tmp_path)
    try:
        captured = {}

        def fake_recall(**kwargs):
            captured['ctx'] = dict(kwargs['task_context'])
            captured['host'] = assistance._HOST_DEADLINE.get()
            captured['now'] = perf_counter()
            from eimemory.retrieval.contracts import RecallBundle
            return RecallBundle(items=[], rules=[], reflections=[], confidence=0.0,
                                next_action_hint='', explanation={})

        monkeypatch.setattr(runtime.memory, 'recall', fake_recall)
        runtime.proactive._recall_with_timeout(query='q', scope={}, source_ids=('hermes',),
                                               task_type='', channel='hermes')
        deadline = captured['ctx']['_recall_deadline_monotonic']
        assert deadline == captured['host']
        assert 6.5 < deadline - captured['now'] <= 7.25 + 0.01
        runtime.proactive._recall_with_timeout(query='q', scope={}, source_ids=('codex',),
                                               task_type='', channel='codex')
        assert '_recall_deadline_monotonic' not in captured['ctx'] and captured['host'] == 0.0
    finally:
        runtime.close()


def test_hermes_proactive_client_times_out_inside_host_window(monkeypatch, tmp_path):
    from eimemory.adapters.hermes import provider_core
    monkeypatch.delenv("EIMEMORY_PROACTIVE_HOST_WINDOW_SECONDS", raising=False)
    monkeypatch.setenv("EIMEMORY_ADAPTER_TIMEOUT_SECONDS", "11.5")
    client = provider_core.hermes_proactive_client_from_env(hermes_home=str(tmp_path))
    assert client.timeout_seconds == 7.6
    # The server's host deadline (window - 0.75s) lands before the client gives up.
    assert 8.0 - budgets.proactive_host_margin_seconds() < client.timeout_seconds < 8.0
    monkeypatch.setenv("EIMEMORY_PROACTIVE_HOST_WINDOW_SECONDS", "0")
    assert provider_core.hermes_proactive_client_from_env(hermes_home=str(tmp_path)).timeout_seconds == 11.5


def test_bypass_fallback_only_when_window_remains(monkeypatch):
    import time
    from eimemory.adapters.hermes.provider_core import HermesMemoryProviderCore
    monkeypatch.delenv("EIMEMORY_PROACTIVE_HOST_WINDOW_SECONDS", raising=False)
    now = time.monotonic()
    assert HermesMemoryProviderCore._host_window_allows_fallback(now) is True
    assert HermesMemoryProviderCore._host_window_allows_fallback(now - 5.5) is False
