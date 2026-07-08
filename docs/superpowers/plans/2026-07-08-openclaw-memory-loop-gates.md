# OpenClaw Memory Loop Gates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a fast closed-loop release that prevents OpenClaw/model/bridge failure samples from becoming positive memory or replay evidence, and makes gateway double-binding health part of deploy verification.

**Architecture:** Keep the change in the existing OpenClaw hook and loop doctor paths. Terminal hook classification remains fail-closed, while `openclaw_loop.doctor` extends the current config/live health checks to cover the local loopback gateway and `openclaw-loopback-proxy.service`.

**Tech Stack:** Python stdlib, existing `Runtime`/OpenClaw hook APIs, `pytest`, existing `scripts/openclaw_loop.py` compatibility wrapper.

## Global Constraints

- Preserve fallback behavior, but rate-limit/cooldown/timeout/context-overflow/model/bridge failures must not be recorded as positive replay/event outcome evidence.
- WebChat gateway health must require both tailnet gateway and local loopback binding to be healthy.
- `openclaw-loopback-proxy.service` must be checked as a user-level service when live checks run on Linux.
- Follow TDD: write and run failing focused tests before implementation.
- Bump package version after code/tests pass, then commit, push, deploy, and verify honxin services.

---

### Task 1: OpenClaw Terminal Failure Gate

**Files:**
- Modify: `tests/test_openclaw_outcome_hooks.py`
- Modify: `eimemory/adapters/openclaw/hooks.py`

**Interfaces:**
- Consumes: `OpenClawMemoryHooks.on_agent_end(event: dict) -> dict`
- Produces: terminal outcome classification that maps rate-limit/cooldown/timeout/context-overflow/model/bridge failures to bad/diagnostic outcomes even when an adapter marks `success=True`.

- [ ] **Step 1: Write the failing test**

```python
def test_openclaw_agent_end_rate_limit_cooldown_success_is_bad_trace(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    hooks = OpenClawMemoryHooks(runtime)
    traces: list[dict] = []

    def fake_record_outcome_trace(payload: dict, *, scope: dict) -> dict:
        traces.append(payload)
        return {"id": "trace-rate-limit"}

    monkeypatch.setattr(runtime, "record_outcome_trace", fake_record_outcome_trace, raising=False)

    result = hooks.on_agent_end(
        {
            "session_id": "sess-rate-limit",
            "agent_id": "main",
            "workspace_id": "repo-x",
            "user_id": "darrow",
            "query": "summarize current state",
            "task_context": {"task_type": "chat.reply", "bridge_status": "cooldown"},
            "outcome": {
                "success": True,
                "verified": True,
                "notes": "OpenAI rate limit cooldown active; fallback response used.",
            },
        }
    )

    assert result["outcome"]["outcome"] == "bad"
    assert result["outcome"]["failure_class"] == "rate_limit_cooldown"
    assert result["outcome"]["source_trust"] == "system_diagnostic"
    assert traces[0]["outcome"] == "bad"
    assert traces[0]["failure_class"] == "rate_limit_cooldown"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_openclaw_outcome_hooks.py::test_openclaw_agent_end_rate_limit_cooldown_success_is_bad_trace -q`
Expected: FAIL because the hook currently records the trace as `success`/`good`.

- [ ] **Step 3: Write minimal implementation**

Add a small terminal failure classifier in `hooks.py` and thread its result through terminal outcome and trace payloads.

- [ ] **Step 4: Run focused tests**

Run: `python -m pytest tests/test_openclaw_outcome_hooks.py -q`
Expected: all tests in the file pass.

### Task 2: Gateway Double-Binding Doctor Gate

**Files:**
- Modify: `scripts/test_openclaw_loop.py`
- Modify: `eimemory/ops/openclaw_loop.py`
- Modify: `scripts/openclaw_loop.py`

**Interfaces:**
- Consumes: `check_config_drift(config_path=None, run_live_checks=True) -> dict`
- Produces: config drift report that includes `openclaw_loopback_health_failed` and `openclaw_loopback_proxy_inactive`/`openclaw_loopback_proxy_not_enabled` when applicable.

- [ ] **Step 1: Write the failing tests**

```python
def test_config_drift_requires_loopback_gateway_health(self):
    config = self.root / "openclaw.json"
    config.write_text(json.dumps({"gateway": {}}), encoding="utf-8")

    def fake_http_json(url, timeout=3.0):
        if "127.0.0.1:18789" in url:
            raise TimeoutError("loopback gateway timeout")
        return {"ok": True}

    old_http_json = loop._http_json
    old_proxy_state = loop.check_openclaw_loopback_proxy_user_service
    loop._http_json = fake_http_json
    loop.check_openclaw_loopback_proxy_user_service = lambda: {"ok": True}
    try:
        result = loop.check_config_drift(config_path=config, run_live_checks=True)
    finally:
        loop._http_json = old_http_json
        loop.check_openclaw_loopback_proxy_user_service = old_proxy_state

    self.assertFalse(result["ok"])
    self.assertIn("openclaw_loopback_health_failed", result["codes"])

def test_config_drift_requires_loopback_proxy_user_service(self):
    config = self.root / "openclaw.json"
    config.write_text(json.dumps({"gateway": {}}), encoding="utf-8")

    old_http_json = loop._http_json
    old_proxy_state = loop.check_openclaw_loopback_proxy_user_service
    loop._http_json = lambda url, timeout=3.0: {"ok": True}
    loop.check_openclaw_loopback_proxy_user_service = lambda: {
        "ok": False,
        "reason": "openclaw_loopback_proxy_inactive",
        "active": "inactive",
        "enabled": "enabled",
    }
    try:
        result = loop.check_config_drift(config_path=config, run_live_checks=True)
    finally:
        loop._http_json = old_http_json
        loop.check_openclaw_loopback_proxy_user_service = old_proxy_state

    self.assertFalse(result["ok"])
    self.assertIn("openclaw_loopback_proxy_inactive", result["codes"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python scripts/test_openclaw_loop.py OpenClawLoopTests.test_config_drift_requires_loopback_gateway_health OpenClawLoopTests.test_config_drift_requires_loopback_proxy_user_service`
Expected: FAIL because loopback health and proxy service are not yet checked.

- [ ] **Step 3: Write minimal implementation**

Update both module and wrapper copy so deployed compatibility script carries the same behavior.

- [ ] **Step 4: Run focused tests**

Run: `python scripts/test_openclaw_loop.py`
Expected: all loop tests pass.

### Task 3: Version, Commit, Deploy, Verify

**Files:**
- Modify: `pyproject.toml`
- Modify: `eimemory/version.py`

**Interfaces:**
- Produces release `1.8.23`.

- [ ] **Step 1: Bump version**

Set both version files to `1.8.23`.

- [ ] **Step 2: Run verification**

Run: focused pytest files, `python -m pytest tests/test_version.py`, `python -m compileall eimemory scripts`, `git diff --check`, then the full pytest suite if time permits.

- [ ] **Step 3: Commit and deploy**

Commit only intended files, push to `origin/master`, deploy immutable release on honxin, restart/verify user services, and run OpenClaw loop doctor/deploy verification.
