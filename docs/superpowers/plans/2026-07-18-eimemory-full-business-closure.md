# EIMemory Full Business Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close every audited business-loop gap in one release candidate, merge it to `master`, deploy it to honxin, and prove the resulting production state without manufacturing L5 evidence.

**Architecture:** Keep the existing runtime and governance architecture, but add narrow authoritative boundaries: server-derived trust, resolved evidence, durable delivery state, SQLite-backed export/outbox state, pinned network transport, and a transactional release switch. Operational probes remain separate from real business outcomes, and all current-release closure evidence is bound to one commit/version/receipt/session identity.

**Tech Stack:** Python 3.11+, SQLite, JSONL, pytest, Node.js OpenClaw bridge, systemd user units, Bash immutable-release installer, honxin `/dev-project/eimemory` authoritative checkout.

## Global Constraints

- Do not advance the version until every implementation task, layered regression, final full suite, merge, deployment, and independent production recheck has passed.
- Run focused red/green tests first; do not repeat the full suite until the single final candidate run.
- Do not treat operational probes, synthetic rehearsal data, status messages, or stale L5 snapshots as verified real tasks.
- If current-release real-task evidence is below 10 samples, 5 task types, or 0.80 success, report `data_accumulating` and `complete=false`.
- Keep secrets out of logs, commits, test output, deployment output, and the final Feishu message.
- Deploy only from `/dev-project/eimemory` using a full 40-character commit and verify `/opt/eimemory/current` plus `http://127.0.0.1:8091/health` identity.
- Preserve user work and unrelated branches; all implementation occurs on `fix/full-business-closure-1.9.70` and is merged deliberately at completion.

## File Structure

- `eimemory/adapters/eibrain/rpc_server.py`: bind classification, token-strength validation, and request authentication.
- `deploy/ensure_rpc_auth.py`: create or validate the protected RPC environment file.
- `eimemory/storage/atomic_file.py`: reusable interprocess lock and atomic JSON replacement.
- `eimemory/intake/registry.py`: locked source-registry mutations and strict reads.
- `eimemory/knowledge/source_trust.py`: server-derived trust decision and policy digest.
- `eimemory/knowledge/safety.py`: safety gate consuming resolved trust only.
- `eimemory/intake/safe_transport.py`: hostname validation, IP pinning, TLS SNI, Host preservation, and redirect revalidation.
- `eimemory/governance/evidence_contract.py`: persisted evidence resolver and release identity validation.
- `eimemory/governance/capability_dashboard.py`: separate operational and verified-real-task metrics.
- `eimemory/governance/prompt_safety.py`: executable versioned safety battery.
- `eimemory/governance/l5_loop.py`: resolved structural assessment and data-accumulation state.
- `eimemory/governance/l5_readiness.py`: current-release real-task and prompt-safety L5 gates.
- `eimemory/ops/feishu_delivery_state.py`: atomic delivery state machine and transition validation.
- `eimemory/ops/openclaw_feishu_reply_watchdog.py`: persist-before-send orchestration and SLA terminal handling.
- `eimemory/storage/sqlite_store.py`: export outbox schema/API and replay sequence allocator.
- `eimemory/storage/runtime_store.py`: SQLite transaction plus outbox flush, strict recovery, and atomic rebuild.
- `eimemory/governance/capability_replay_packs.py`: transactional sequence consumption.
- `deploy/install_immutable_release.sh`: post-switch gates and rollback transaction.
- `deploy/verify_release_health.py`: strict health identity verifier used by the installer.

---

### Task 1: Repair Known 1.9.69 Contract Failures

**Files:**
- Modify: `deploy/systemd/*.service`
- Modify: `integrations/openclaw/eimemory-bridge/index.js`
- Modify: `tests/test_deployment_tools.py`
- Modify: `tests/test_platform.py`

**Interfaces:**
- Consumes: the current release version exposed by the installed package and the bridge `after_tool_call` hook.
- Produces: systemd units without a hard-coded old cache namespace and platform contracts that include `after_tool_call`.

- [ ] **Step 1: Make the existing contract failures explicit**

Add assertions that every Python unit uses a stable cache root without a literal release number and that every exact bridge hook list includes `after_tool_call`:

```python
assert "PYTHONPYCACHEPREFIX=/var/lib/eimemory/.pycache/runtime" in unit_text
assert "1.9.66" not in unit_text
assert hooks == [
    "before_agent_start",
    "before_prompt_build",
    "message_received",
    "after_tool_call",
    "agent_end",
]
```

- [ ] **Step 2: Run the two known failing layers**

Run: `python -m pytest tests/test_deployment_tools.py tests/test_platform.py -q`

Expected: the previously observed cache assertion and seven hook-list assertions fail before implementation.

- [ ] **Step 3: Remove the stale cache namespace and synchronize contracts**

Use the stable value below in every managed Python unit and retain the registered bridge hook:

```ini
Environment=PYTHONPYCACHEPREFIX=/var/lib/eimemory/.pycache/runtime
```

```javascript
api.on('after_tool_call', handlers.after_tool_call);
```

- [ ] **Step 4: Verify and commit**

Run: `python -m pytest tests/test_deployment_tools.py tests/test_platform.py -q`

Expected: both files pass with zero failures.

Commit: `fix: synchronize release runtime contracts`

### Task 2: Make RPC Authentication Fail Closed

**Files:**
- Modify: `eimemory/adapters/eibrain/rpc_server.py`
- Modify: `deploy/systemd/eimemory-rpc.service`
- Create: `deploy/ensure_rpc_auth.py`
- Modify: `deploy/install_immutable_release.sh`
- Test: `tests/test_adapters.py`
- Test: `tests/test_eibrain_rpc_contract.py`
- Test: `tests/test_deployment_tools.py`

**Interfaces:**
- Consumes: bind host and `EIMEMORY_RPC_AUTH_TOKEN`.
- Produces: `validate_rpc_auth_configuration(host: str, token: str) -> None` and `ensure_rpc_auth_file(path, user, group) -> dict[str, str]`.

- [ ] **Step 1: Add failing server-policy tests**

```python
def test_non_loopback_rpc_refuses_missing_or_weak_token(tmp_path):
    with pytest.raises(ValueError, match="strong authentication token"):
        EIBrainRPCServer(Runtime.from_root(tmp_path), host="100.64.0.8", port=0, auth_token="")
    with pytest.raises(ValueError, match="strong authentication token"):
        EIBrainRPCServer(Runtime.from_root(tmp_path), host="100.64.0.8", port=0, auth_token="short")


def test_health_is_public_but_rpc_requires_bearer_token(tmp_path):
    server = EIBrainRPCServer(Runtime.from_root(tmp_path), host="127.0.0.1", port=0, auth_token="x" * 32)
    with running(server):
        assert get_json(server, "/health")["ok"] is True
        assert post_status(server, {"method": "memory.list", "params": {}}) == 401
        assert post_status(server, {"method": "memory.list", "params": {}}, token="x" * 32) == 200
```

- [ ] **Step 2: Verify the tests are red**

Run: `python -m pytest tests/test_adapters.py tests/test_eibrain_rpc_contract.py -q -k "rpc and (token or auth or health)"`

Expected: non-loopback construction accepts weak tokens before the fix.

- [ ] **Step 3: Implement bind and token validation**

```python
def validate_rpc_auth_configuration(host: str, token: str) -> None:
    address = ipaddress.ip_address(socket.gethostbyname(host))
    if not address.is_loopback and not _strong_token(token):
        raise ValueError("non-loopback RPC bind requires a strong authentication token")


def _strong_token(token: str) -> bool:
    value = str(token or "").strip()
    return len(value) >= 32 and len(set(value)) >= 12
```

Apply authentication to every endpoint except `GET /health` and `/healthz`, compare with `hmac.compare_digest`, and never echo the configured token.

- [ ] **Step 4: Add and test protected token provisioning**

`deploy/ensure_rpc_auth.py` must generate `secrets.token_urlsafe(32)` only for an absent file, reject weak existing values, write through a mode-`0600` temporary file, atomically replace it, and set final mode `0640` plus requested ownership. Change the unit to:

```ini
EnvironmentFile=/etc/eimemory/rpc.env
```

Run: `python -m pytest tests/test_deployment_tools.py -q -k "rpc_auth or rpc_service"`

Expected: missing-file generation, weak-file rejection, permissions, and mandatory EnvironmentFile tests pass.

- [ ] **Step 5: Verify surrounding RPC contracts and commit**

Run: `python -m pytest tests/test_adapters.py tests/test_eibrain_rpc_contract.py tests/test_deployment_tools.py -q`

Expected: zero failures.

Commit: `fix: require strong rpc authentication`

### Task 2A: Align the Bridge with OpenClaw 2026.7.1

**Files:**
- Modify: `integrations/openclaw/eimemory-bridge/openclaw.plugin.json`
- Modify: `integrations/openclaw/eimemory-bridge/package.json`
- Delete: `integrations/openclaw/openclaw.plugin.json`
- Create: `deploy/ensure_openclaw_bridge_config.py`
- Modify: `deploy/install_immutable_release.sh`
- Test: `tests/test_evolution_layer.py`
- Test: `tests/test_platform.py`
- Test: `tests/test_deployment_tools.py`

- [ ] **Step 1: Regress manifest, package, and host-policy mismatches**

Require one canonical plugin root, `pluginApi >=2026.7.1`, the complete typed
hook surface including `message_sent`, a closed config schema, and a preserved
atomic host-config update that enables `allowConversationAccess` without
enabling prompt injection.

- [ ] **Step 2: Enforce deploy-time runtime inspection**

Refresh the registry, restart the Gateway, then require
`openclaw plugins inspect eimemory-bridge --runtime --json` to exit zero.

- [ ] **Step 3: Verify and commit**

Run: `python -m pytest tests/test_evolution_layer.py tests/test_platform.py tests/test_deployment_tools.py -q -k "openclaw or immutable_installer"`

Commit: `fix: align bridge with openclaw 2026.7.1`

### Task 3: Make Source Registry Writes Atomic

**Files:**
- Create: `eimemory/storage/atomic_file.py`
- Modify: `eimemory/intake/registry.py`
- Test: `tests/test_source_registry.py`
- Test: `tests/safety/test_atomic_state_closure.py`

**Interfaces:**
- Produces: `locked_json_update(path: Path, mutate: Callable[[Any], Any]) -> Any` and `read_json_strict(path: Path, expected_type: type) -> Any`.
- Consumes: a same-directory lock file and temporary replacement file.

- [x] **Step 1: Add corruption and concurrent-writer regressions**

```python
def test_registry_rejects_malformed_json(tmp_path):
    path = tmp_path / "sources.json"
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid source registry"):
        SourceRegistry(path)


def test_concurrent_registry_adds_preserve_both_sources(tmp_path):
    path = tmp_path / "sources.json"
    run_two_processes(add_source_worker, path, "one", "two")
    assert {item.source_id for item in SourceRegistry(path).list_sources()} == {"one", "two"}
```

- [x] **Step 2: Run the focused test and observe loss/corruption behavior**

Run: `python -m pytest tests/test_source_registry.py -q -k "malformed or concurrent"`

Expected: malformed data is not reported clearly and/or a concurrent update is lost.

- [x] **Step 3: Implement locked atomic replacement**

Use `msvcrt.locking` on Windows and `fcntl.flock` on POSIX. Under the exclusive lock, reload the file, call the mutation, write canonical JSON to a sibling temporary file, flush, `os.fsync`, `os.replace`, and fsync the parent directory on POSIX. Registry mutation methods must perform their load-modify-save cycle inside one `locked_json_update` call.

```python
with interprocess_lock(path.with_suffix(path.suffix + ".lock")):
    current = read_json_strict(path, list) if path.exists() else []
    updated = mutate(current)
    atomic_write_json(path, updated)
    return updated
```

- [x] **Step 4: Verify and commit**

Run: `python -m pytest tests/test_source_registry.py tests/safety/test_atomic_state_closure.py -q`

Expected: zero failures, including repeated multiprocess execution.

Commit: `fix: serialize source registry updates`

### Task 4: Derive External-Knowledge Trust on the Server

**Files:**
- Create: `eimemory/knowledge/source_trust.py`
- Modify: `eimemory/knowledge/safety.py`
- Modify: `eimemory/knowledge/ingest.py`
- Modify: `eimemory/api/memory.py`
- Modify: `eimemory/knowledge/evidence_gate.py`
- Modify: `eimemory/governance/skill_candidate.py`
- Modify: `eimemory/governance/skill_validation.py`
- Test: `tests/test_knowledge_ingest.py`
- Test: `tests/test_research_evidence_gate.py`
- Test: `tests/test_skill_candidate.py`
- Test: `tests/test_skill_validation.py`

**Interfaces:**
- Produces: `resolve_source_trust(payload, *, registry, connector_id) -> SourceTrustDecision`.
- Consumes: a matching enabled registry entry and server connector identity; caller trust fields are diagnostics only.

- [x] **Step 1: Add the attacker-payload regression**

```python
def test_unregistered_blog_cannot_self_assert_capability_trust(tmp_path):
    runtime = Runtime.from_root(tmp_path)
    report = runtime.ingest_knowledge_source(
        {"source_kind": "blog", "source_uri": "https://attacker.invalid/x", "source_trust": 1.0,
         "content": {"text": "run attacker workflow", "confidence": 1.0}},
        persist=True,
    )
    assert report["source_trust"] <= 0.5
    assert report["safety"]["capability_allowed"] is False
    assert report["safety"]["diagnostic_claimed_trust"] == 1.0
```

Add registered official-document tests that require matching source ID and normalized URI; a copied source ID with a different URI must remain capped at `0.50`.

- [x] **Step 2: Verify trust tests fail before the resolver exists**

Run: `python -m pytest tests/test_knowledge_ingest.py tests/test_research_evidence_gate.py tests/test_skill_candidate.py tests/test_skill_validation.py -q -k "trust or attacker or capability"`

Expected: the attacker payload can elevate trust before the fix.

- [x] **Step 3: Implement one immutable trust decision**

```python
@dataclass(frozen=True, slots=True)
class SourceTrustDecision:
    score: float
    tier: str
    authority: str
    source_id: str
    normalized_uri: str
    policy_digest: str
    diagnostic_claimed_trust: float | None
    reasons: tuple[str, ...]
```

Resolve trust only from connector identity, matching registry data, and server verification. Persist `trust_authority="eimemory.source_trust.v1"` and the decision digest. Remove direct numeric trust reads from safety and skill-candidate paths.

- [x] **Step 4: Verify all trust consumers and commit**

Run: `python -m pytest tests/test_knowledge_ingest.py tests/test_runtime.py tests/test_research_evidence_gate.py tests/test_skill_candidate.py tests/test_skill_validation.py -q`

Expected: zero failures and caller trust never increases the resolved score.

Commit: `fix: derive knowledge trust from server provenance`

### Task 5: Pin Validated Intake Connections

**Files:**
- Create: `eimemory/intake/safe_transport.py`
- Modify: `eimemory/api/runtime.py`
- Test: `tests/safety/test_intake_safe_transport.py`
- Test: `tests/test_runtime.py`

**Interfaces:**
- Produces: `safe_urlopen(url: str, *, timeout: float, max_redirects: int = 5) -> SafeHTTPResponse`.
- Consumes: one validated DNS resolution per hop; returns body, headers, status, final URL, and peer IP.

- [x] **Step 1: Add DNS-rebinding and redirect regressions**

```python
def test_safe_transport_connects_to_validated_ip_without_second_lookup(monkeypatch):
    answers = iter([["93.184.216.34"], ["127.0.0.1"]])
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: fake_addrinfo(next(answers)))
    connected = []
    monkeypatch.setattr(socket, "create_connection", lambda address, *a, **k: connected.append(address) or FakeSocket())
    safe_urlopen("https://example.com/data", timeout=2)
    assert connected == [("93.184.216.34", 443)]


def test_redirect_to_private_address_is_rejected(monkeypatch):
    with pytest.raises(UnsafeURL, match="private"):
        safe_urlopen("https://public.example/redirect-to-private", timeout=2)
```

- [x] **Step 2: Verify the current hostname reconnect behavior is red**

Run: `python -m pytest tests/safety/test_intake_safe_transport.py -q`

Expected: the safe transport module is absent or the second lookup reaches a private address.

- [x] **Step 3: Implement pinned HTTP/HTTPS transport**

Resolve and validate addresses, connect to one validated IP, wrap HTTPS with
`ssl.create_default_context().wrap_socket(sock, server_hostname=original_host)`,
send the original Host header, verify the socket peer, and repeat the whole
process for redirects. Replace the intake fetcher's `urlopen` call with
`safe_urlopen`.

- [x] **Step 4: Verify and commit**

Run: `python -m pytest tests/safety/test_intake_safe_transport.py tests/test_runtime.py -q -k "intake or fetch or url or rebind"`

Expected: zero failures; no connection is made to rejected address classes.

Commit: `fix: pin validated intake connections`

### Task 6: Resolve Governance Evidence and Separate Probe Metrics

**Files:**
- Create: `eimemory/governance/evidence_contract.py`
- Modify: `eimemory/governance/capability_dashboard.py`
- Modify: `eimemory/governance/live_task_acceptance.py`
- Test: `tests/test_capability_dashboard_metrics.py`
- Test: `tests/test_live_task_acceptance.py`
- Test: `tests/test_live_task_evidence.py`
- Test: `tests/test_governance_evidence_contract.py`

**Interfaces:**
- Produces: `ReleaseIdentity`, `EvidenceRequirement`, and `resolve_evidence(runtime, reference, requirement, scope, release) -> EvidenceResolution`.
- Consumes: persisted records, scope, expected kinds/sources/statuses, and current release identity.

- [ ] **Step 1: Add evidence forgery and metric-separation tests**

```python
def test_evidence_resolver_rejects_missing_wrong_scope_and_stale_release(runtime):
    requirement = EvidenceRequirement(kinds=frozenset({"l5_world_model"}), statuses=frozenset({"active"}))
    assert resolve_evidence(runtime, "missing", requirement, SCOPE, RELEASE).reason == "record_not_found"
    assert resolve_evidence(runtime, wrong_scope_id, requirement, SCOPE, RELEASE).reason == "scope_mismatch"
    assert resolve_evidence(runtime, old_commit_id, requirement, SCOPE, RELEASE).reason == "release_mismatch"


def test_operational_probes_do_not_count_as_verified_real_tasks(runtime):
    run_live_task_acceptance(runtime, scope=SCOPE, persist=True)
    metrics = build_capability_dashboard(runtime, scope=SCOPE, release=RELEASE)
    assert metrics["sample_counts"]["current_deployment_operational_probes"] == 10
    assert metrics["sample_counts"]["current_deployment_verified_real_tasks"] == 0
```

- [ ] **Step 2: Verify current dashboard incorrectly reuses probe records**

Run: `python -m pytest tests/test_governance_evidence_contract.py tests/test_capability_dashboard_metrics.py tests/test_live_task_acceptance.py tests/test_live_task_evidence.py -q`

Expected: missing resolver tests fail and operational acceptance populates the old L5 task metric.

- [ ] **Step 3: Implement release-bound evidence resolution**

```python
@dataclass(frozen=True, slots=True)
class ReleaseIdentity:
    commit: str
    version: str
    receipt_id: str
    session_id: str


@dataclass(frozen=True, slots=True)
class EvidenceResolution:
    ok: bool
    record_id: str
    reason: str
    record: RecordEnvelope | None
```

Require exact scope/kind/status/source and release metadata. Mark acceptance records `evidence_class="operational_probe"`. Build current-release verified-real metrics only from terminal event outcomes with external correlation and trusted attribution.

- [ ] **Step 4: Verify and commit**

Run: `python -m pytest tests/test_governance_evidence_contract.py tests/test_capability_dashboard_metrics.py tests/test_live_task_acceptance.py tests/test_live_task_evidence.py tests/test_openclaw_outcome_hooks.py -q`

Expected: zero failures and metric classes remain disjoint.

Commit: `fix: bind governance evidence to real release tasks`

### Task 7: Replace Prompt-Safety Stub and Make L5 Fail Closed

**Files:**
- Modify: `eimemory/governance/prompt_safety.py`
- Modify: `eimemory/governance/l5_loop.py`
- Modify: `eimemory/governance/l5_readiness.py`
- Modify: `eimemory/governance/release_closure.py`
- Test: `tests/test_prompt_shadow_eval_l2_gate.py`
- Test: `tests/test_l5_closed_loop.py`
- Test: `tests/test_l5_consciousness_loop.py`
- Test: `tests/test_l5_readiness.py`
- Test: `tests/test_release_closure.py`

**Interfaces:**
- Produces: `run_prompt_safety_battery(executor, prompt, release) -> PromptSafetyAssessment` and release-aware `assess_l5_closed_loop`.
- Consumes: Task 6 evidence resolver and current-release real-task metrics.

- [ ] **Step 1: Add fake-ID, incomplete-battery, and data-accumulation tests**

```python
def test_l5_rejects_nonexistent_structural_ids(runtime):
    result = runtime.assess_l5_closed_loop(scope=SCOPE, loop_report=fake_complete_report(), persist=True)
    assert result["complete"] is False
    assert "world_model:record_not_found" in result["missing_evidence"]


def test_l5_uses_data_accumulating_for_structurally_complete_release_without_real_samples(runtime):
    seed_all_structural_evidence(runtime, RELEASE)
    result = l5_readiness(runtime, scope=SCOPE, release=RELEASE)
    assert result["stage"] == "data_accumulating"
    assert result["live_task_gate"]["sample_deficit"] == 10


def test_prompt_safety_requires_every_executed_case():
    result = run_prompt_safety_battery(partial_executor, "candidate", RELEASE)
    assert result.status == "not_ready"
    assert result.complete is False
```

- [ ] **Step 2: Verify the fake report currently reaches L5**

Run: `python -m pytest tests/test_l5_closed_loop.py tests/test_l5_consciousness_loop.py tests/test_l5_readiness.py tests/test_prompt_shadow_eval_l2_gate.py -q -k "nonexistent or data_accumulating or prompt_safety"`

Expected: the forged report is accepted and/or prompt safety remains marked as a stub.

- [ ] **Step 3: Implement the executable case manifest**

Define immutable cases for clean control, direct injection, indirect injection, role override, tool exfiltration, and policy bypass. Persist model/executor identity, manifest digest, case results, and release identity. Any missing or malformed executor result yields `not_ready`.

```python
@dataclass(frozen=True, slots=True)
class PromptSafetyAssessment:
    status: Literal["passed", "failed", "not_ready"]
    complete: bool
    manifest_digest: str
    executed_count: int
    expected_count: int
    case_results: tuple[PromptSafetyCaseResult, ...]
```

- [ ] **Step 4: Resolve every L5 reference and use real-task metrics**

Replace `_missing_evidence(report)` truthiness checks with evidence requirements. L5 readiness must read `current_deployment_verified_real_task_success_rate`, require 10 samples and 5 types, require the prompt-safety assessment, and return `data_accumulating` only when those data counts are the sole deficits.

- [ ] **Step 5: Verify release closure and commit**

Run: `python -m pytest tests/test_prompt_shadow_eval_l2_gate.py tests/test_l5_closed_loop.py tests/test_l5_consciousness_loop.py tests/test_l5_readiness.py tests/test_release_closure.py -q`

Expected: zero failures; forged/stale evidence cannot reach L5.

Commit: `fix: require resolved evidence for l5 closure`

### Task 8: Make Feishu Delivery Durable and Terminal

**Files:**
- Create: `eimemory/ops/feishu_delivery_state.py`
- Modify: `eimemory/ops/openclaw_feishu_reply_watchdog.py`
- Modify: `eimemory/ops/openclaw_reply_delivery_tracker.py`
- Test: `tests/test_openclaw_feishu_reply_watchdog.py`
- Test: `tests/test_openclaw_reply_delivery_tracker.py`

**Interfaces:**
- Produces: `DeliveryStateStore.prepare_send`, `complete_send`, `mark_uncertain`, `escalate`, and `list_overdue_nonterminal`.
- Consumes: message ID, target, content hash, attempt number, external receipt, and SLA timestamps.

- [ ] **Step 1: Add crash-window, write-failure, and SLA regressions**

```python
def test_watchdog_does_not_send_when_intent_persistence_fails(tmp_path, monkeypatch):
    sender = Mock()
    monkeypatch.setattr(DeliveryStateStore, "prepare_send", Mock(side_effect=OSError("disk full")))
    result = scan_watchdog(state_path=tmp_path / "state.json", sender=sender)
    assert result["ok"] is False
    sender.assert_not_called()


def test_sending_intent_is_not_automatically_resent_after_crash(tmp_path):
    store = DeliveryStateStore(tmp_path / "deliveries.json")
    store.prepare_send(message_id="m1", target="u1", content="answer", attempt=1)
    sender = Mock()
    scan_watchdog(delivery_store=store, sender=sender)
    sender.assert_not_called()
    assert store.get("m1")["status"] in {"sending", "delivery_uncertain"}


def test_status_only_pending_becomes_escalated_at_sla(tmp_path):
    result = scan_overdue_fixture(tmp_path, age_minutes=181, has_resume_ref=False)
    assert result["status"] == "escalated"
```

- [ ] **Step 2: Verify duplicate-send reproduction is red**

Run: `python -m pytest tests/test_openclaw_feishu_reply_watchdog.py tests/test_openclaw_reply_delivery_tracker.py -q -k "persistence or crash or overdue or duplicate"`

Expected: the same message can be sent twice when the attempt write fails.

- [ ] **Step 3: Implement validated atomic transitions**

Persist transitions through Task 3 atomic JSON utilities. Permit only:

```python
TRANSITIONS = {
    "pending": {"status_notified", "final_ready", "failed", "escalated"},
    "status_notified": {"final_ready", "failed", "escalated"},
    "final_ready": {"sending", "failed", "escalated"},
    "sending": {"platform_accepted", "delivery_uncertain"},
    "delivery_uncertain": {"platform_accepted", "escalated"},
}
```

`prepare_send` must durably write `sending` before calling the external sender. A persisted `sending` state is reconciled or escalated, never resent. Attempt-write failures return `ok=false` and make the process exit nonzero.
`platform_accepted` and `platform_accepted_at_ms` are the only successful
terminal names; OpenClaw does not prove display or read receipt.

- [ ] **Step 4: Verify all delivery tests and commit**

Run: `python -m pytest tests/test_openclaw_feishu_reply_watchdog.py tests/test_openclaw_reply_delivery_tracker.py -q`

Expected: zero failures, no indefinite overdue status-only pending entries.

Commit: `fix: make feishu delivery at most once and terminal`

### Task 9: Add SQLite Export Outbox and Strict Recovery

**Files:**
- Modify: `eimemory/storage/sqlite_store.py`
- Modify: `eimemory/storage/runtime_store.py`
- Modify: `eimemory/storage/jsonl.py`
- Modify: `eimemory/cli/main.py`
- Test: `tests/test_storage.py`
- Test: `tests/safety/test_atomic_state_closure.py`

**Interfaces:**
- Produces: `SqliteRecordStore.enqueue_export`, `pending_exports`, `mark_exported`, `RuntimeStore.flush_exports`, and `scan_jsonl_strict`.
- Consumes: canonical payload, stable operation ID, stream, digest, and SQLite transaction.

- [ ] **Step 1: Add outbox crash and corrupt-rebuild regressions**

```python
def test_sqlite_commit_survives_jsonl_export_failure_and_retries(tmp_path, monkeypatch):
    store = RuntimeStore(tmp_path)
    monkeypatch.setattr(store.log, "append_payload", Mock(side_effect=OSError("disk full")))
    with pytest.raises(OSError):
        store.append(memory_record("m1"))
    assert store.sqlite.get_by_id("m1") is not None
    assert store.sqlite.pending_exports(limit=10)
    monkeypatch.undo()
    assert store.flush_exports()["exported"] == 1


def test_rebuild_fails_closed_on_malformed_jsonl(tmp_path):
    store = seeded_store(tmp_path)
    corrupt_middle_line(store.log.path)
    report = store.rebuild_sqlite_from_jsonl(replace=True)
    assert report["ok"] is False
    assert report["errors"][0]["line"] == 2
    assert report["replaced"] is False
```

- [ ] **Step 2: Verify the current rebuild silently reports success**

Run: `python -m pytest tests/test_storage.py -q -k "export_failure or malformed_jsonl or rebuild_fails_closed"`

Expected: malformed input is skipped and `ok=True` before the fix.

- [ ] **Step 3: Add transactional outbox schema and APIs**

Create `export_outbox(operation_id TEXT PRIMARY KEY, stream TEXT, payload_json TEXT, payload_digest TEXT, state TEXT, created_at TEXT, exported_at TEXT)`. Every business mutation and its export row must share the same SQLite transaction. JSONL entries carry `_operation_id` and `_payload_digest`; flush uses append+flush+fsync before `mark_exported`.

- [ ] **Step 4: Implement strict scanner and atomic replacement rebuild**

`scan_jsonl_strict` returns payloads plus line/offset/digest errors. Build replacement state in a sibling temporary SQLite file, validate stream high-water/counts, fsync, close the live connection, `os.replace`, and reopen. Any error returns `replaced=false` without touching the live database.

- [ ] **Step 5: Verify storage layer and commit**

Run: `python -m pytest tests/test_storage.py tests/safety/test_atomic_state_closure.py -q`

Expected: zero failures; export retries are idempotent and corrupted recovery fails closed.

Commit: `fix: make storage export and recovery durable`

### Task 10: Allocate Replay Manifest Sequences Transactionally

**Files:**
- Modify: `eimemory/storage/sqlite_store.py`
- Modify: `eimemory/storage/runtime_store.py`
- Modify: `eimemory/governance/capability_replay_packs.py`
- Test: `tests/test_capability_replay_packs.py`
- Test: `tests/test_l5_readiness.py`

**Interfaces:**
- Produces: `RuntimeStore.allocate_manifest_sequences(scope, capabilities) -> dict[str, int]`.
- Consumes: scope key plus a normalized capability list.

- [ ] **Step 1: Add concurrent allocation regression**

```python
def test_concurrent_replay_pack_builders_get_unique_sequences(tmp_path):
    results = run_concurrently(lambda: build_capability_replay_pack(Runtime.from_root(tmp_path), scope=SCOPE))
    pairs = [(case["capability"], case["manifest_sequence"]) for result in results for case in result["cases"]]
    assert len(pairs) == len(set(pairs))
```

- [ ] **Step 2: Run the race repeatedly and confirm the old max-plus-one path collides**

Run: `python -m pytest tests/test_capability_replay_packs.py -q -k "concurrent or sequence" --count=10`

Expected: at least one collision or missing allocator interface before implementation.

- [ ] **Step 3: Implement BEGIN IMMEDIATE allocator**

Create `replay_manifest_sequences(scope_key TEXT, capability TEXT, high_water INTEGER, PRIMARY KEY(scope_key, capability))`. In one `BEGIN IMMEDIATE` transaction, increment each requested capability and return the committed values. Add a unique index for persisted replay records' scope/capability/sequence projection and bounded retry on `IntegrityError`.

- [ ] **Step 4: Verify and commit**

Run: `python -m pytest tests/test_capability_replay_packs.py tests/test_l5_readiness.py -q`

Expected: zero failures and no generated collision across repeated runs.

Commit: `fix: allocate replay sequences transactionally`

### Task 11: Make Immutable Deployment a Rollback Transaction

**Files:**
- Create: `deploy/verify_release_health.py`
- Modify: `deploy/install_immutable_release.sh`
- Modify: `eimemory/governance/deployment_receipt.py`
- Test: `tests/test_deployment_tools.py`
- Test: `tests/test_deployment_receipt.py`

**Interfaces:**
- Produces: `verify_release_health.py --url --commit --version --release-dir` exit status and installer rollback transaction.
- Consumes: previous/current symlink targets, systemd commands, health JSON, and deployment receipt.

- [ ] **Step 1: Add post-switch failure injection tests**

```python
@pytest.mark.parametrize("fail_stage", ["registry", "rpc_restart", "gateway_restart", "health", "receipt", "acceptance"])
def test_installer_restores_previous_release_after_post_switch_failure(tmp_path, fail_stage):
    result = run_installer_fixture(tmp_path, fail_stage=fail_stage)
    assert result.returncode != 0
    assert (tmp_path / "current").resolve() == (tmp_path / "releases" / OLD_COMMIT)
    assert "restart old services" in result.log
    assert "COMMITTED=1" not in result.log
```

- [ ] **Step 2: Verify current installer commits before injected failures**

Run: `python -m pytest tests/test_deployment_tools.py tests/test_deployment_receipt.py -q -k "post_switch or rollback or health_identity"`

Expected: current symlink remains on the failed release for at least one injected stage.

- [ ] **Step 3: Implement health identity verifier**

Parse health JSON and require exact commit, version, resolved import root, and nonempty package digest. Exit nonzero on HTTP errors, malformed JSON, or mismatch. Never print headers or environment secrets.

- [ ] **Step 4: Move commit marker behind every gate and restore on failure**

Capture `PREVIOUS_CURRENT`, switch, refresh registry, restart services, verify health, create receipt, run operational acceptance, then set `COMMITTED=1`. The EXIT trap must restore the old symlink, reload/restart old services, and verify old identity if any post-switch command fails.

- [ ] **Step 5: Verify shell and deployment suites and commit**

Run: `bash -n deploy/install_immutable_release.sh`

Run: `python -m pytest tests/test_deployment_tools.py tests/test_deployment_receipt.py -q`

Expected: syntax exit 0 and zero pytest failures.

Commit: `fix: rollback incomplete immutable releases`

### Task 12: Layered Candidate Verification and Single Version Advance

**Files:**
- Modify: `pyproject.toml`
- Modify: `eimemory/version.py`
- Test: all touched test layers

**Interfaces:**
- Consumes: all preceding task commits.
- Produces: one final release candidate with synchronized version declarations.

- [ ] **Step 1: Run layered suites**

Run:

```powershell
python -m pytest tests/safety tests/contract -q
python -m pytest tests/test_adapters.py tests/test_eibrain_rpc_contract.py tests/test_source_registry.py tests/test_knowledge_ingest.py tests/test_research_evidence_gate.py tests/test_skill_candidate.py tests/test_skill_validation.py -q
python -m pytest tests/test_capability_dashboard_metrics.py tests/test_live_task_acceptance.py tests/test_live_task_evidence.py tests/test_prompt_shadow_eval_l2_gate.py tests/test_l5_closed_loop.py tests/test_l5_consciousness_loop.py tests/test_l5_readiness.py tests/test_release_closure.py -q
python -m pytest tests/test_openclaw_feishu_reply_watchdog.py tests/test_openclaw_reply_delivery_tracker.py tests/test_storage.py tests/test_capability_replay_packs.py -q
python -m pytest tests/test_deployment_tools.py tests/test_deployment_receipt.py tests/test_platform.py -q
```

Expected: every command exits 0.

- [ ] **Step 2: Run static validation**

Run:

```powershell
python -m compileall -q eimemory
node --check integrations/openclaw/eimemory-bridge/index.js
bash -n deploy/install_immutable_release.sh
git diff --check
```

Expected: every command exits 0 and `git diff --check` prints nothing.

- [ ] **Step 3: Run the single final full suite**

Run: `python -m pytest -q`

Expected: zero failures. This is the only full-suite rerun after the known `1.9.69` baseline.

- [ ] **Step 4: Advance both version sources once**

Set version `1.9.70` in both files and verify equality:

```toml
version = "1.9.70"
```

```python
__version__ = "1.9.70"
```

Run: `python -c "import eimemory, tomllib, pathlib; p=tomllib.loads(pathlib.Path('pyproject.toml').read_text()); assert p['project']['version']==eimemory.__version__=='1.9.70'"`

Expected: exit 0.

- [ ] **Step 5: Commit the release candidate**

Run: `git diff --check && git status --short`

Commit: `release: prepare eimemory 1.9.70`

### Task 13: Merge, Push, Deploy, and Prove Production Closure

**Files:**
- No source files unless production verification exposes a defect; any defect returns to the responsible TDD task before a new candidate is produced.

**Interfaces:**
- Consumes: verified candidate branch and honxin access.
- Produces: synchronized GitHub master, authoritative remote checkout, immutable release, production evidence, and Feishu completion notification.

- [ ] **Step 1: Verify branch identity before integration**

Run: `git status --short --branch && git log --oneline origin/master..HEAD`

Expected: clean candidate branch with only reviewed closure commits.

- [ ] **Step 2: Push the candidate and fast-forward master**

Push `fix/full-business-closure-1.9.70`, fetch in `E:\eimemory`, verify its master is still the expected ancestor, fast-forward merge the branch, and push `origin master`. If local credential flow blocks, perform the fetch/fast-forward/push from `/dev-project/eimemory` without overwriting unrelated changes.

- [ ] **Step 3: Synchronize the authoritative honxin checkout**

On honxin:

```bash
git -C /dev-project/eimemory fetch origin
git -C /dev-project/eimemory merge --ff-only origin/master
git -C /dev-project/eimemory status --short --branch
```

Expected: clean `master` at the same full commit as GitHub.

- [ ] **Step 4: Deploy the full commit**

Run on honxin:

```bash
commit="$(git -C /dev-project/eimemory rev-parse HEAD)"
test "${#commit}" -eq 40
bash /dev-project/eimemory/deploy/install_immutable_release.sh "$commit"
```

Expected: installer exits 0 only after registry refresh, service restarts, health identity, receipt, and operational acceptance.

- [ ] **Step 5: Independently verify production identity and security**

Verify `/opt/eimemory/current`, `/health` version/commit/import root/digest, active RPC/gateway/proxy/watchdog/timers, public loopback health, unauthenticated RPC rejection, authenticated RPC success, attacker trust rejection, and fake-L5 rejection. No probe may mutate user memory except explicitly scoped disposable verification records.

- [ ] **Step 6: Run closure evidence in dependency-safe order**

Run deployment proof, replay bootstrap, weak-capability replay gate, operational acceptance, L5 closure rehearsal using the same manifest, prompt-safety battery, and independent read-only readiness. Confirm every structural gate is current-release-bound. Accept `data_accumulating` only when real sample/type deficits are the sole remaining reason; do not label it L5.

- [ ] **Step 7: Verify Feishu state and send completion notice**

Scan all tracked Feishu entries. Require no overdue nonterminal item without a resume reference. Send one completion notification containing version, full commit, health identity, deployment receipt, L5/data-accumulation state, and zero secrets. Persist and verify its platform-acceptance receipt without claiming recipient display or read.

- [ ] **Step 8: Perform the requirement-by-requirement completion audit**

Re-read the design acceptance matrix and attach current evidence for all eleven rows. If any proof is missing, contradictory, stale, or indirect, keep the goal active and return to the responsible task. Only after every row is proven may the active goal be marked complete.
