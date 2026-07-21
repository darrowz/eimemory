# Codex and Hermes Channel Adapters Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver channel-isolated, authoritative long-term memory adapters for Codex and Hermes through the existing authenticated eimemory runtime.

**Architecture:** Add a small channel-scope and lifecycle service behind the existing EIBrain RPC, then ship a native Codex plugin and a native Hermes `MemoryProvider` as thin clients. OpenClaw keeps its current scope, while Codex and Hermes use deterministic channel scopes and share the same capture, recall, terminal-evidence, release, and bypass contracts.

**Tech Stack:** Python 3.11+, stdlib HTTP/JSON-RPC, SQLite/WAL, pytest, Codex lifecycle hooks and MCP, Hermes MemoryProvider, Git, systemd immutable releases.

## Global Constraints

- Authority mode is `per_channel`; OpenClaw, Codex, and Hermes memories are independent and authoritative inside their own channel after common gates pass.
- Recall must not cross channel scopes unless a future explicit federation feature is enabled.
- Existing OpenClaw scopes and records must not be migrated or rewritten.
- Do not add a SQLite channel column; use deterministic indexed scope values.
- Host integration failures must bypass without blocking Codex or Hermes.
- Do not read full Codex or Hermes transcripts.
- A success without explicit verification must not count as a verified real task.
- Do not advance the version until all adapter, adjacent regression, packaging, and live checks pass.

---

### Task 1: Channel scope and authoritative memory service

**Files:**
- Create: `eimemory/adapters/runtime/__init__.py`
- Create: `eimemory/adapters/runtime/channel.py`
- Create: `eimemory/adapters/runtime/service.py`
- Test: `tests/test_runtime_channel_adapter.py`

**Interfaces:**
- Produces: `resolve_channel_scope(channel: str, scope: dict) -> dict[str, str]`.
- Produces: `AgentRuntimeMemoryService.prefetch`, `sync_turn`, `remember`, `record_terminal`, and `status`.

- [ ] **Step 1: Write failing channel-isolation tests**

```python
def test_codex_and_hermes_memories_are_independent_authoritative_records(tmp_path):
    runtime = Runtime.create(root=tmp_path)
    service = AgentRuntimeMemoryService(runtime)
    base = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    codex = service.remember(channel="codex", scope=base, text="Codex durable preference", memory_type="preference", event_id="c1")
    hermes = service.remember(channel="hermes", scope=base, text="Hermes durable preference", memory_type="preference", event_id="h1")
    assert codex["record"]["scope"]["workspace_id"] == "embodied::channel::codex"
    assert hermes["record"]["scope"]["workspace_id"] == "embodied::channel::hermes"
    assert codex["record"]["meta"]["authoritative"] is True
    assert hermes["record"]["meta"]["authoritative"] is True
```

- [ ] **Step 2: Run RED**

Run: `python -m pytest tests/test_runtime_channel_adapter.py -q`

Expected: collection fails because `eimemory.adapters.runtime` does not exist.

- [ ] **Step 3: Implement deterministic scopes and minimal service**

```python
SUPPORTED_RUNTIME_CHANNELS = frozenset({"openclaw", "codex", "hermes"})

def resolve_channel_scope(channel: str, scope: dict) -> dict[str, str]:
    channel_id = normalize_runtime_channel(channel)
    resolved = ScopeRef.from_dict(scope)
    if channel_id == "openclaw":
        return asdict(resolved)
    base = resolved.workspace_id or "default"
    suffix = f"::channel::{channel_id}"
    workspace_id = base if base.endswith(suffix) else base + suffix
    return {**asdict(resolved), "workspace_id": workspace_id}
```

- [ ] **Step 4: Add idempotency, bounded content, capture metadata, and terminal RED/GREEN cases**

Run: `python -m pytest tests/test_runtime_channel_adapter.py -q`

Expected: all runtime-channel tests pass, including channel-local recall, duplicate turn handling, explicit verification, and lifecycle-only session endings.

### Task 2: Additive authenticated RPC contract and fail-open client

**Files:**
- Modify: `eimemory/ei_bridge/protocol.py`
- Modify: `eimemory/adapters/eibrain/rpc.py`
- Create: `eimemory/adapters/runtime/http_client.py`
- Test: `tests/test_runtime_adapter_rpc.py`
- Test: `tests/test_eibrain_rpc_contract.py`

**Interfaces:**
- Consumes: `AgentRuntimeMemoryService`.
- Produces: RPC methods `adapter.prefetch`, `adapter.sync_turn`, `adapter.remember`, `adapter.record_terminal`, and `adapter.status`.
- Produces: `AgentRuntimeRPCClient.call(method, params) -> dict` and `call_or_bypass(...) -> dict`.

- [ ] **Step 1: Write failing RPC method and authentication tests**

```python
response = bridge.handle({"method": "adapter.status", "params": {"channel": "codex", "scope": scope}})
assert response["ok"] is True
assert response["result"]["adapter_contract_version"] == "agent.runtime.v1"
```

- [ ] **Step 2: Run RED**

Run: `python -m pytest tests/test_runtime_adapter_rpc.py tests/test_eibrain_rpc_contract.py -q`

Expected: `adapter.status` returns `unknown_method`.

- [ ] **Step 3: Route validated methods through the shared service**

Reject unknown channels, empty explicit memories, invalid boolean verification, and oversized client-side content before calling the runtime.

- [ ] **Step 4: Implement client timeout, circuit breaker, and bounded failure ledger**

The bypass result must have this stable shape:

```python
{"ok": False, "bypassed": True, "error": "adapter_unavailable", "result": None}
```

- [ ] **Step 5: Run GREEN**

Run: `python -m pytest tests/test_runtime_channel_adapter.py tests/test_runtime_adapter_rpc.py tests/test_eibrain_rpc_contract.py -q`

Expected: all selected tests pass.

### Task 3: Generalize verified real-task evidence by runtime channel

**Files:**
- Modify: `eimemory/experience/outcome.py`
- Modify: `eimemory/governance/capability_dashboard.py`
- Test: `tests/test_runtime_terminal_evidence.py`
- Test: `tests/test_capability_dashboard.py`

**Interfaces:**
- Consumes: terminal sources `openclaw.agent_end`, `openclaw.task_end`, `codex.stop`, and `hermes.task_end`.
- Produces: release-bound `verified_real_task` evidence validated against the matching terminal event and event outcome in the same channel scope.

- [ ] **Step 1: Write failing Codex and Hermes terminal evidence tests**

```python
assert codex_metrics["sample_counts"]["current_deployment_verified_real_tasks"] == 1
assert hermes_metrics["sample_counts"]["current_deployment_verified_real_tasks"] == 1
assert openclaw_metrics["sample_counts"]["current_deployment_verified_real_tasks"] == 0
```

- [ ] **Step 2: Run RED**

Run: `python -m pytest tests/test_runtime_terminal_evidence.py -q`

Expected: non-OpenClaw sources are not classified as server-bound real tasks.

- [ ] **Step 3: Replace the OpenClaw-only validator with a runtime-terminal validator**

The validator derives `hook = method.split(".", 1)[1]`, requires exact source/method equality, enforces same scope, and validates signed tool receipts when the verification claims them.

- [ ] **Step 4: Run GREEN and adjacent L5 tests**

Run: `python -m pytest tests/test_runtime_terminal_evidence.py tests/test_capability_dashboard.py tests/test_l5_readiness.py tests/test_openclaw_outcome_hooks.py -q`

Expected: all selected tests pass and OpenClaw behavior is unchanged.

### Task 4: Codex plugin hooks and MCP tools

**Files:**
- Create: `eimemory/adapters/codex/__init__.py`
- Create: `eimemory/adapters/codex/hook.py`
- Create: `eimemory/adapters/codex/mcp_server.py`
- Create: `integrations/codex/eimemory/.codex-plugin/plugin.json`
- Create: `integrations/codex/eimemory/hooks/hooks.json`
- Create: `integrations/codex/eimemory/.mcp.json`
- Create: `integrations/codex/eimemory/README.md`
- Modify: `eimemory/cli/main.py`
- Test: `tests/test_codex_adapter.py`
- Test: `tests/test_codex_plugin_package.py`

**Interfaces:**
- Consumes: Codex `SessionStart`, `UserPromptSubmit`, `PostToolUse`, and `Stop` JSON from stdin.
- Produces: bounded hook JSON and MCP tools `eimemory_recall`, `eimemory_remember`, `eimemory_verify_outcome`, and `eimemory_status`.

- [ ] **Step 1: Scaffold the plugin with plugin-creator**

Run from the plugin-creator skill root:

```powershell
python scripts/create_basic_plugin.py eimemory --path E:\eimemory\.worktrees\codex-hermes-adapters\integrations\codex --with-hooks --with-mcp
```

- [ ] **Step 2: Write failing hook mapping and package tests**

```python
assert run_hook("UserPromptSubmit", prompt_event)["hookSpecificOutput"]["additionalContext"]
assert run_hook("Stop", unverified_success)["continue"] is True
assert terminal_spy["verification"] == ""
```

- [ ] **Step 3: Run RED**

Run: `python -m pytest tests/test_codex_adapter.py tests/test_codex_plugin_package.py -q`

Expected: hook runner and plugin files are incomplete.

- [ ] **Step 4: Implement hooks, MCP framing, CLI entry, and fail-open output**

Do not parse `transcript_path`. Hash/truncate tool inputs and results before forwarding them. `Stop` must never emit `continue: false` because eimemory is advisory.

- [ ] **Step 5: Validate and run GREEN**

Run:

```powershell
python -m pytest tests/test_codex_adapter.py tests/test_codex_plugin_package.py -q
python C:\Users\maiph\.codex\skills\.system\plugin-creator\scripts\validate_plugin.py integrations\codex\eimemory
```

Expected: tests and plugin validation pass.

### Task 5: Hermes MemoryProvider plugin

**Files:**
- Create: `eimemory/adapters/hermes/__init__.py`
- Create: `eimemory/adapters/hermes/provider_core.py`
- Create: `integrations/hermes/eimemory/__init__.py`
- Create: `integrations/hermes/eimemory/plugin.yaml`
- Create: `integrations/hermes/eimemory/README.md`
- Test: `tests/test_hermes_adapter.py`
- Test: `tests/test_hermes_plugin_package.py`

**Interfaces:**
- Consumes: Hermes `MemoryProvider` lifecycle.
- Produces: provider name `eimemory`, channel-local prefetch/sync/remember/status/verified-outcome behavior.

- [ ] **Step 1: Write failing provider lifecycle and package tests**

```python
provider.initialize("hermes-session", hermes_home=str(tmp_path))
assert "Hermes durable preference" in provider.prefetch("durable preference")
provider.sync_turn("remember Hermes preference", "stored", session_id="hermes-session")
assert provider.name == "eimemory"
```

- [ ] **Step 2: Run RED**

Run: `python -m pytest tests/test_hermes_adapter.py tests/test_hermes_plugin_package.py -q`

Expected: provider modules do not exist.

- [ ] **Step 3: Implement the provider core and thin Hermes registration wrapper**

All network calls use `call_or_bypass`. `sync_turn` and mirrors never raise into Hermes. `get_tool_schemas()` returns only the four eimemory tools.

- [ ] **Step 4: Run GREEN**

Run: `python -m pytest tests/test_hermes_adapter.py tests/test_hermes_plugin_package.py -q`

Expected: all Hermes tests pass without requiring Hermes as an eimemory dependency.

### Task 6: Documentation, layered verification, and external audit

**Files:**
- Modify: `README.md`
- Modify: `docs/operations.md`
- Modify: `deploy/systemd/eimemory-rpc.service`
- Test: `tests/test_deployment_tools.py`

**Interfaces:**
- Produces: reproducible install/configure/disable/status procedures for both hosts and a production RPC endpoint suitable for the adapters.

- [ ] **Step 1: Write failing documentation/deployment assertions**

Require the RPC token, loopback bind, Codex install path, Hermes `memory.provider: eimemory`, channel scope behavior, and bypass semantics to be documented.

- [ ] **Step 2: Run RED, implement docs and deployment defaults, then run GREEN**

Run: `python -m pytest tests/test_deployment_tools.py tests/test_codex_plugin_package.py tests/test_hermes_plugin_package.py -q`

- [ ] **Step 3: Run the complete affected test layers**

```powershell
python -m pytest tests/test_runtime_channel_adapter.py tests/test_runtime_adapter_rpc.py tests/test_runtime_terminal_evidence.py tests/test_codex_adapter.py tests/test_codex_plugin_package.py tests/test_hermes_adapter.py tests/test_hermes_plugin_package.py tests/test_adapters.py tests/test_eibrain_rpc_contract.py tests/test_openclaw_outcome_hooks.py tests/test_capability_dashboard.py tests/test_l5_readiness.py tests/test_deployment_tools.py -q
python -m compileall -q eimemory integrations
git diff --check
```

Expected: zero failures and no whitespace or compilation errors.

- [ ] **Step 4: Run third-party review and repair every Critical/Important finding**

Refresh the structural graph and review the complete feature diff:

```powershell
E:\code-review-graph\.venv\Scripts\code-review-graph.exe update --repo E:\eimemory\.worktrees\codex-hermes-adapters --base origin/master --brief
E:\code-review-graph\.venv\Scripts\code-review-graph.exe detect-changes --repo E:\eimemory\.worktrees\codex-hermes-adapters --base origin/master --brief
E:\open-code-review\bin\ocr.exe review --repo E:\eimemory\.worktrees\codex-hermes-adapters --from origin/master --to HEAD --audience agent --format json --background-file docs\superpowers\specs\2026-07-21-codex-hermes-channel-adapters-design.md
```

Add a failing regression test before each behavior-changing repair, rerun the
affected layer, and retain both outputs as release evidence.

### Task 7: Patch release, merge, deploy, and live closure

**Files:**
- Modify: `pyproject.toml`
- Modify: `eimemory/version.py`
- Modify: `CHANGELOG.md` when present
- Deployment source: `/dev-project/eimemory`
- Immutable target: `/opt/eimemory/releases/<full-commit>`

**Interfaces:**
- Consumes: all Task 1-6 checks and repaired review findings.
- Produces: one patch release on `master`, production identity alignment, and adapter closure evidence.

- [ ] **Step 1: Advance both version declarations to 1.9.78 and add release notes**

Set `pyproject.toml` and `eimemory/version.py` to `1.9.78`. Both declarations
must match.

- [ ] **Step 2: Run fresh release verification**

Run the complete affected layer, `compileall`, version tests, plugin validation, and `git diff --check` again after the version edit.

- [ ] **Step 3: Commit the feature and release, merge to master, and push**

Use conventional commits, tag the release, and verify `origin/master` equals local `master`.

- [ ] **Step 4: Deploy the full commit from `/dev-project/eimemory`**

Run `deploy/install_immutable_release.sh <full-40-character-commit>`, restart the user services, and verify `/opt/eimemory/current` points to that commit.

- [ ] **Step 5: Execute live adapter and production closure checks**

Verify RPC health and auth, OpenClaw services, Codex hook prefetch/bypass, Hermes provider prefetch/sync/bypass, per-channel record isolation, deployment receipt, release identity, and independent L5 readiness. The release is complete only when all evidence binds to the deployed commit and no fixable verification gap remains.
