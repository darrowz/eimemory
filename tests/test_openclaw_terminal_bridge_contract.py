from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

from eimemory.governance.tool_receipts import verify_tool_receipt


RECEIPT_KEY = "test-openclaw-receipt-key-with-at-least-32-characters"


def _bridge_env() -> dict[str, str]:
    env = os.environ.copy()
    env["EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY"] = RECEIPT_KEY
    return env


def test_bridge_correlates_prompt_and_successful_tool_receipt_into_agent_end(tmp_path: Path) -> None:
    capture_path = tmp_path / "captured.jsonl"
    hook_script = tmp_path / "capture-hook.js"
    hook_script.write_text(
        """
const fs = require('node:fs');
const hook = process.argv[2] || '';
const payload = JSON.parse(fs.readFileSync(0, 'utf8') || '{}');
fs.appendFileSync(process.env.EIMEMORY_CAPTURE_PATH, JSON.stringify({ hook, payload }) + '\\n');
if (hook === 'before_prompt_build') {
  process.stdout.write(JSON.stringify({
    task_context: {
      openclaw_loop_task_id: 'loop-task-1',
      task_type: 'search.discovery',
    },
    trace_context: { task_type: 'search.discovery' },
  }));
} else {
  process.stdout.write('{}');
}
""".strip(),
        encoding="utf-8",
    )
    script = """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
process.env.EIMEMORY_ENABLE_PROMPT_INJECTION = 'true';
plugin.register({
  config: { allowPromptInjection: true },
  hooks: { on(name, handler) { handlers[name] = handler; } },
});
const context = { runId: 'run-1', sessionKey: 'agent:main:feishu:direct:user-1', sessionId: 'session-1' };
Promise.resolve()
  .then(() => handlers.before_prompt_build({
    prompt: 'Search GitHub releases and compare primary sources.',
    messages: [],
    runId: 'run-1',
  }, context))
  .then(() => handlers.after_tool_call({
    toolName: 'web.search',
    toolCallId: 'tool-1',
    runId: 'run-1',
    params: { query: 'GitHub releases' },
    result: { ok: true, resultCount: 3 },
  }, context))
  .then(() => handlers.agent_end({
    runId: 'run-1',
    messages: [
      { role: 'user', content: 'Search GitHub releases and compare primary sources.' },
      { role: 'assistant', content: 'Compared three primary sources.' },
    ],
    success: true,
  }, context))
  .catch((error) => { console.error(error && error.stack ? error.stack : String(error)); process.exit(1); });
""".strip()
    env = _bridge_env()
    env["EIMEMORY_HOOK_COMMAND"] = f'node "{hook_script}"'
    env["EIMEMORY_CAPTURE_PATH"] = str(capture_path)
    result = subprocess.run(
        ["node", "-e", script],
        cwd=Path.cwd(),
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in capture_path.read_text(encoding="utf-8").splitlines()]
    terminal = next(item["payload"] for item in calls if item["hook"] == "agent_end")
    assert terminal["query"] == "Search GitHub releases and compare primary sources."
    assert terminal["task_context"]["task_type"] == "search.discovery"
    assert terminal["tools"] == ["web.search"]
    assert terminal["outcome"]["verified"] is True
    assert terminal["outcome"]["verification"].startswith("openclaw.after_tool_call:")
    assert len(terminal["verification_receipts"]) == 1
    receipt = terminal["verification_receipts"][0]
    assert receipt["source"] == "openclaw.after_tool_call"
    assert receipt["tool_name"] == "web.search"
    assert receipt["tool_call_id"] == "tool-1"
    assert receipt["passed"] is True
    assert receipt["receipt_version"] == 1
    assert receipt["attestation"] == "hmac-sha256"
    assert len(receipt["result_digest"]) == 64
    assert len(receipt["signature"]) == 64
    assert verify_tool_receipt(
        receipt,
        session_id="session-1",
        run_id="run-1",
        key=RECEIPT_KEY,
    ) is True


def test_bridge_does_not_treat_failed_tool_call_as_terminal_verification(tmp_path: Path) -> None:
    capture_path = tmp_path / "captured.jsonl"
    hook_script = tmp_path / "capture-hook.js"
    hook_script.write_text(
        """
const fs = require('node:fs');
const hook = process.argv[2] || '';
const payload = JSON.parse(fs.readFileSync(0, 'utf8') || '{}');
fs.appendFileSync(process.env.EIMEMORY_CAPTURE_PATH, JSON.stringify({ hook, payload }) + '\\n');
process.stdout.write('{}');
""".strip(),
        encoding="utf-8",
    )
    script = """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
process.env.EIMEMORY_ENABLE_PROMPT_INJECTION = 'true';
plugin.register({
  config: { allowPromptInjection: true },
  hooks: { on(name, handler) { handlers[name] = handler; } },
});
const context = { runId: 'run-failed', sessionKey: 'agent:main:feishu:direct:user-1', sessionId: 'session-1' };
Promise.resolve()
  .then(() => handlers.before_prompt_build({ prompt: 'Check service health.', runId: 'run-failed' }, context))
  .then(() => handlers.after_tool_call({
    toolName: 'systemctl',
    toolCallId: 'tool-failed',
    runId: 'run-failed',
    result: { ok: false, error: 'service unavailable' },
  }, context))
  .then(() => handlers.agent_end({ runId: 'run-failed', messages: [], success: true }, context))
  .catch((error) => { console.error(error && error.stack ? error.stack : String(error)); process.exit(1); });
""".strip()
    env = _bridge_env()
    env["EIMEMORY_HOOK_COMMAND"] = f'node "{hook_script}"'
    env["EIMEMORY_CAPTURE_PATH"] = str(capture_path)
    result = subprocess.run(
        ["node", "-e", script],
        cwd=Path.cwd(),
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in capture_path.read_text(encoding="utf-8").splitlines()]
    terminal = next(item["payload"] for item in calls if item["hook"] == "agent_end")
    assert "verified" not in terminal["outcome"]
    assert terminal["outcome"]["verification"] == ""


def test_bridge_keeps_terminal_correlation_when_prompt_injection_is_disabled(tmp_path: Path) -> None:
    capture_path = tmp_path / "captured.jsonl"
    hook_script = tmp_path / "capture-hook.js"
    hook_script.write_text(
        """
const fs = require('node:fs');
const hook = process.argv[2] || '';
const payload = JSON.parse(fs.readFileSync(0, 'utf8') || '{}');
fs.appendFileSync(process.env.EIMEMORY_CAPTURE_PATH, JSON.stringify({ hook, payload }) + '\\n');
process.stdout.write('{}');
""".strip(),
        encoding="utf-8",
    )
    script = """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
process.env.EIMEMORY_ENABLE_PROMPT_INJECTION = 'false';
plugin.register({
  config: { allowPromptInjection: false },
  hooks: { on(name, handler) { handlers[name] = handler; } },
});
const context = { runId: 'run-disabled', sessionKey: 'agent:main:feishu:direct:user-1', sessionId: 'session-1' };
Promise.resolve()
  .then(() => handlers.before_prompt_build({
    prompt: 'Check gateway service health.',
    runId: 'run-disabled',
  }, context))
  .then(() => handlers.after_tool_call({
    toolName: 'systemctl',
    toolCallId: 'tool-disabled',
    runId: 'run-disabled',
    result: { status: 'active' },
  }, context))
  .then(() => handlers.agent_end({
    runId: 'run-disabled',
    messages: [],
    success: true,
  }, context))
  .catch((error) => { console.error(error && error.stack ? error.stack : String(error)); process.exit(1); });
""".strip()
    env = _bridge_env()
    env["EIMEMORY_HOOK_COMMAND"] = f'node "{hook_script}"'
    env["EIMEMORY_CAPTURE_PATH"] = str(capture_path)
    result = subprocess.run(
        ["node", "-e", script],
        cwd=Path.cwd(),
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in capture_path.read_text(encoding="utf-8").splitlines()]
    assert all(item["hook"] != "before_prompt_build" for item in calls)
    terminal = next(item["payload"] for item in calls if item["hook"] == "agent_end")
    assert terminal["query"] == "Check gateway service health."
    assert terminal["tools"] == ["systemctl"]
    assert terminal["outcome"]["verified"] is True


def test_bridge_isolates_runs_fails_closed_without_result_and_prefers_terminal_context() -> None:
    script = """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
process.env.EIMEMORY_ENABLE_PROMPT_INJECTION = 'false';
plugin.register({ hooks: { on(name, handler) { handlers[name] = handler; } } });
(async () => {
  const shared = { sessionKey: 'same', sessionId: 'same' };
  await handlers.before_prompt_build({ prompt: 'Run A', runId: 'run-A' }, shared);
  await handlers.before_prompt_build({ prompt: 'Run B', runId: 'run-B' }, shared);
  await handlers.after_tool_call({
    toolName: 'tool-from-A',
    toolCallId: 'call-A',
    runId: 'run-A',
    result: { ok: true },
  }, shared);
  const crossRun = await handlers.agent_end({ runId: 'run-B', success: true, messages: [] }, shared);

  const missing = { sessionKey: 'missing', sessionId: 'missing' };
  await handlers.before_prompt_build({ prompt: 'Missing result', runId: 'run-missing' }, missing);
  await handlers.after_tool_call({
    toolName: 'no-result-tool',
    toolCallId: 'call-missing',
    runId: 'run-missing',
  }, missing);
  const missingResult = await handlers.agent_end({
    runId: 'run-missing',
    success: true,
    messages: [],
  }, missing);

  const unsupportedContext = { sessionKey: 'unsupported', sessionId: 'unsupported' };
  await handlers.before_prompt_build({
    prompt: 'Internal reasoning only',
    runId: 'run-unsupported',
  }, unsupportedContext);
  await handlers.after_tool_call({
    toolName: 'internal.reasoning',
    toolCallId: 'call-unsupported',
    runId: 'run-unsupported',
    result: { ok: true },
  }, unsupportedContext);
  const unsupported = await handlers.agent_end({
    runId: 'run-unsupported',
    success: true,
    messages: [],
  }, unsupportedContext);

  const precedence = { sessionKey: 'precedence', sessionId: 'precedence' };
  await handlers.before_prompt_build({
    prompt: 'Precedence',
    runId: 'run-precedence',
    task_context: { task_type: 'cached.type' },
  }, precedence);
  const explicit = await handlers.agent_end({
    runId: 'run-precedence',
    success: false,
    task_context: { task_type: 'terminal.explicit' },
    messages: [],
  }, precedence);

  const boundedContext = { sessionKey: 'bounded', sessionId: 'bounded' };
  await handlers.before_prompt_build({
    prompt: 'Bounded context',
    runId: 'run-bounded',
    task_context: { task_type: 'bounded.type', oversized: 'x'.repeat(100000) },
  }, boundedContext);
  const bounded = await handlers.agent_end({
    runId: 'run-bounded',
    success: false,
    messages: [],
  }, boundedContext);
  console.log(JSON.stringify({ crossRun, missingResult, unsupported, explicit, bounded }));
})().catch((error) => { console.error(error && error.stack ? error.stack : String(error)); process.exit(1); });
""".strip()
    env = _bridge_env()
    env["EIMEMORY_HOOK_COMMAND"] = 'node -e "process.stdin.pipe(process.stdout)"'
    result = subprocess.run(
        ["node", "-e", script],
        cwd=Path.cwd(),
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.splitlines()[-1])
    assert "tool-from-A" not in payload["crossRun"]["tools"]
    assert "verified" not in payload["crossRun"]["outcome"]
    assert "verified" not in payload["missingResult"]["outcome"]
    assert payload["missingResult"]["outcome"]["verification"] == ""
    assert "verified" not in payload["unsupported"]["outcome"]
    assert payload["explicit"]["task_context"]["task_type"] == "terminal.explicit"
    assert len(json.dumps(payload["bounded"]["task_context"])) < 10_000
    assert len(payload["bounded"]["task_context"]["oversized"]) <= 2_048


def test_bridge_requires_independent_post_mutation_verification_and_positive_status() -> None:
    script = """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
process.env.EIMEMORY_ENABLE_PROMPT_INJECTION = 'false';
plugin.register({ hooks: { on(name, handler) { handlers[name] = handler; } } });
(async () => {
  const mutationOnly = { runId: 'mutation-only', sessionKey: 'mutation-only', sessionId: 'mutation-only' };
  await handlers.before_prompt_build({ prompt: 'Patch the module.', runId: 'mutation-only' }, mutationOnly);
  await handlers.after_tool_call({
    toolName: 'apply_patch', toolCallId: 'patch-1', runId: 'mutation-only', result: { ok: true },
  }, mutationOnly);
  const mutationEnd = await handlers.agent_end({ runId: 'mutation-only', success: true, messages: [] }, mutationOnly);

  const checked = { runId: 'checked', sessionKey: 'checked', sessionId: 'checked' };
  await handlers.before_prompt_build({ prompt: 'Patch and test the module.', runId: 'checked' }, checked);
  await handlers.after_tool_call({
    toolName: 'apply_patch', toolCallId: 'patch-2', runId: 'checked', result: { ok: true },
  }, checked);
  await handlers.after_tool_call({
    toolName: 'exec', toolCallId: 'test-2', runId: 'checked',
    params: { command: 'pytest -q tests/test_example.py' }, result: { status: 'completed', exitCode: 0 },
  }, checked);
  const checkedEnd = await handlers.agent_end({ runId: 'checked', success: true, messages: [] }, checked);

  const pending = { runId: 'pending', sessionKey: 'pending', sessionId: 'pending' };
  await handlers.before_prompt_build({ prompt: 'Check service status.', runId: 'pending' }, pending);
  await handlers.after_tool_call({
    toolName: 'systemctl', toolCallId: 'pending-1', runId: 'pending', result: { status: 'pending' },
  }, pending);
  const pendingEnd = await handlers.agent_end({ runId: 'pending', success: true, messages: [] }, pending);

  const nonzero = { runId: 'nonzero', sessionKey: 'nonzero', sessionId: 'nonzero' };
  await handlers.before_prompt_build({ prompt: 'Run the tests.', runId: 'nonzero' }, nonzero);
  await handlers.after_tool_call({
    toolName: 'exec', toolCallId: 'nonzero-1', runId: 'nonzero',
    params: { command: 'pytest -q' }, result: { status: 'completed', exitCode: 1 },
  }, nonzero);
  const nonzeroEnd = await handlers.agent_end({ runId: 'nonzero', success: true, messages: [] }, nonzero);

  const pendingText = { runId: 'pending-text', sessionKey: 'pending-text', sessionId: 'pending-text' };
  await handlers.before_prompt_build({ prompt: 'Check pending text.', runId: 'pending-text' }, pendingText);
  await handlers.after_tool_call({
    toolName: 'web.search', toolCallId: 'pending-text-1', runId: 'pending-text', result: 'pending',
  }, pendingText);
  const pendingTextEnd = await handlers.agent_end({ runId: 'pending-text', success: true, messages: [] }, pendingText);

  const pendingStatusText = { runId: 'pending-status-text', sessionKey: 'pending-status-text', sessionId: 'pending-status-text' };
  await handlers.before_prompt_build({ prompt: 'Check status text.', runId: 'pending-status-text' }, pendingStatusText);
  await handlers.after_tool_call({
    toolName: 'systemctl', toolCallId: 'pending-status-text-1', runId: 'pending-status-text',
    result: 'status: pending',
  }, pendingStatusText);
  const pendingStatusTextEnd = await handlers.agent_end({ runId: 'pending-status-text', success: true, messages: [] }, pendingStatusText);

  const failedArray = { runId: 'failed-array', sessionKey: 'failed-array', sessionId: 'failed-array' };
  await handlers.before_prompt_build({ prompt: 'Read results.', runId: 'failed-array' }, failedArray);
  await handlers.after_tool_call({
    toolName: 'read', toolCallId: 'failed-array-1', runId: 'failed-array', result: [{ error: 'failed' }],
  }, failedArray);
  const failedArrayEnd = await handlers.agent_end({ runId: 'failed-array', success: true, messages: [] }, failedArray);

  const failedContent = { runId: 'failed-content', sessionKey: 'failed-content', sessionId: 'failed-content' };
  await handlers.before_prompt_build({ prompt: 'Run the tests.', runId: 'failed-content' }, failedContent);
  await handlers.after_tool_call({
    toolName: 'exec', toolCallId: 'failed-content-1', runId: 'failed-content',
    params: { command: 'pytest -q' },
    result: { status: 'completed', exitCode: 0, content: [{ type: 'text', text: '5 failed' }] },
  }, failedContent);
  const failedContentEnd = await handlers.agent_end({ runId: 'failed-content', success: true, messages: [] }, failedContent);

  const zeroResults = { runId: 'zero-results', sessionKey: 'zero-results', sessionId: 'zero-results' };
  await handlers.before_prompt_build({ prompt: 'Search primary sources.', runId: 'zero-results' }, zeroResults);
  await handlers.after_tool_call({
    toolName: 'web.search', toolCallId: 'zero-results-1', runId: 'zero-results',
    result: { ok: true, resultCount: 0 },
  }, zeroResults);
  const zeroResultsEnd = await handlers.agent_end({ runId: 'zero-results', success: true, messages: [] }, zeroResults);

  const zeroFailed = { runId: 'zero-failed', sessionKey: 'zero-failed', sessionId: 'zero-failed' };
  await handlers.before_prompt_build({ prompt: 'Run the tests.', runId: 'zero-failed' }, zeroFailed);
  await handlers.after_tool_call({
    toolName: 'exec', toolCallId: 'zero-failed-1', runId: 'zero-failed',
    params: { command: 'pytest -q' }, result: '12 passed, 0 failed',
  }, zeroFailed);
  const zeroFailedEnd = await handlers.agent_end({ runId: 'zero-failed', success: true, messages: [] }, zeroFailed);

  const curlPost = { runId: 'curl-post', sessionKey: 'curl-post', sessionId: 'curl-post' };
  await handlers.before_prompt_build({ prompt: 'Post the update.', runId: 'curl-post' }, curlPost);
  await handlers.after_tool_call({
    toolName: 'exec', toolCallId: 'curl-post-1', runId: 'curl-post',
    params: { command: 'curl -X POST https://example.invalid/items -d value=1' },
    result: { status: 'completed', exitCode: 0 },
  }, curlPost);
  const curlPostEnd = await handlers.agent_end({ runId: 'curl-post', success: true, messages: [] }, curlPost);

  const curlEquals = { runId: 'curl-equals', sessionKey: 'curl-equals', sessionId: 'curl-equals' };
  await handlers.before_prompt_build({ prompt: 'Post the update.', runId: 'curl-equals' }, curlEquals);
  await handlers.after_tool_call({
    toolName: 'exec', toolCallId: 'curl-equals-1', runId: 'curl-equals',
    params: { command: 'curl --request=POST https://example.invalid/items --data=x' },
    result: { status: 'completed', exitCode: 0 },
  }, curlEquals);
  const curlEqualsEnd = await handlers.agent_end({ runId: 'curl-equals', success: true, messages: [] }, curlEquals);

  const curlForm = { runId: 'curl-form', sessionKey: 'curl-form', sessionId: 'curl-form' };
  await handlers.before_prompt_build({ prompt: 'Upload the form.', runId: 'curl-form' }, curlForm);
  await handlers.after_tool_call({
    toolName: 'exec', toolCallId: 'curl-form-1', runId: 'curl-form',
    params: { command: 'curl -Ffile=@artifact.zip https://example.invalid/upload' },
    result: { status: 'completed', exitCode: 0 },
  }, curlForm);
  const curlFormEnd = await handlers.agent_end({ runId: 'curl-form', success: true, messages: [] }, curlForm);
  console.log(JSON.stringify({
    mutationEnd, checkedEnd, pendingEnd, nonzeroEnd, pendingTextEnd, pendingStatusTextEnd,
    failedArrayEnd, failedContentEnd, zeroResultsEnd, zeroFailedEnd, curlPostEnd,
    curlEqualsEnd, curlFormEnd,
  }));
})().catch((error) => { console.error(error && error.stack ? error.stack : String(error)); process.exit(1); });
""".strip()
    env = _bridge_env()
    env["EIMEMORY_HOOK_COMMAND"] = 'node -e "process.stdin.pipe(process.stdout)"'

    result = subprocess.run(
        ["node", "-e", script], cwd=Path.cwd(), env=env,
        capture_output=True, text=True, timeout=20, check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.splitlines()[-1])
    assert "verified" not in payload["mutationEnd"]["outcome"]
    assert payload["checkedEnd"]["outcome"]["verified"] is True
    assert [item["tool_name"] for item in payload["checkedEnd"]["verification_receipts"]] == ["exec"]
    assert "verified" not in payload["pendingEnd"]["outcome"]
    assert "verified" not in payload["nonzeroEnd"]["outcome"]
    assert "verified" not in payload["pendingTextEnd"]["outcome"]
    assert "verified" not in payload["pendingStatusTextEnd"]["outcome"]
    assert "verified" not in payload["failedArrayEnd"]["outcome"]
    assert "verified" not in payload["failedContentEnd"]["outcome"]
    assert "verified" not in payload["zeroResultsEnd"]["outcome"]
    assert payload["zeroFailedEnd"]["outcome"]["verified"] is True
    assert "verified" not in payload["curlPostEnd"]["outcome"]
    assert "verified" not in payload["curlEqualsEnd"]["outcome"]
    assert "verified" not in payload["curlFormEnd"]["outcome"]


def test_bridge_rejects_ambiguous_weak_tool_receipt_and_strictly_bounds_nested_context() -> None:
    script = """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
process.env.EIMEMORY_ENABLE_PROMPT_INJECTION = 'false';
plugin.register({ hooks: { on(name, handler) { handlers[name] = handler; } } });
(async () => {
  const shared = { sessionKey: 'shared', sessionId: 'shared' };
  await handlers.before_prompt_build({ prompt: 'Run A', runId: 'run-A' }, shared);
  await handlers.before_prompt_build({ prompt: 'Run B', runId: 'run-B' }, shared);
  await handlers.after_tool_call({
    toolName: 'web.search', toolCallId: 'weak-call', result: { ok: true, resultCount: 1 },
  }, shared);
  const runB = await handlers.agent_end({ runId: 'run-B', success: true, messages: [] }, shared);

  const orphan = { sessionKey: 'orphan', sessionId: 'orphan' };
  await handlers.after_tool_call({
    toolName: 'web.search', toolCallId: 'orphan-call', result: { ok: true, resultCount: 1 },
  }, orphan);
  const orphanEnd = await handlers.agent_end({ runId: 'victim-run', success: true, messages: [] }, orphan);

  const conflictContext = { sessionKey: 'conflict', sessionId: 'conflict', taskId: 'shared-task' };
  await handlers.before_prompt_build({ prompt: 'Run conflict A', runId: 'conflict-A', taskId: 'shared-task' }, conflictContext);
  await handlers.after_tool_call({
    toolName: 'web.search', toolCallId: 'conflict-call', runId: 'conflict-A', taskId: 'shared-task',
    result: { ok: true, resultCount: 1 },
  }, conflictContext);
  const conflictEnd = await handlers.agent_end({
    runId: 'conflict-B', taskId: 'shared-task', success: true, messages: [],
  }, conflictContext);

  const huge = {};
  for (let i = 0; i < 48; i += 1) {
    huge['k' + i] = Array.from({ length: 32 }, () => Array.from({ length: 32 }, () => true));
  }
  const boundedContext = { runId: 'bounded-nested', sessionKey: 'bounded-nested', sessionId: 'bounded-nested' };
  await handlers.before_prompt_build({
    prompt: 'Bound nested context', runId: 'bounded-nested',
    task_context: { task_type: 'ops.health', interpreted_intent: 'bound', huge },
  }, boundedContext);
  const bounded = await handlers.agent_end({ runId: 'bounded-nested', success: false, messages: [] }, boundedContext);

  const maliciousContext = { runId: 'malicious', sessionKey: 'malicious', sessionId: 'malicious' };
  await handlers.before_prompt_build({
    prompt: 'Malicious context', runId: 'malicious', task_context: { task_type: huge },
  }, maliciousContext);
  const malicious = await handlers.agent_end({ runId: 'malicious', success: false, messages: [] }, maliciousContext);
  console.log(JSON.stringify({
    runB, orphanEnd, conflictEnd, bounded, boundedLength: JSON.stringify(bounded.task_context).length, malicious,
  }));
})().catch((error) => { console.error(error && error.stack ? error.stack : String(error)); process.exit(1); });
""".strip()
    env = _bridge_env()
    env["EIMEMORY_HOOK_COMMAND"] = 'node -e "process.stdin.pipe(process.stdout)"'

    result = subprocess.run(
        ["node", "-e", script], cwd=Path.cwd(), env=env,
        capture_output=True, text=True, timeout=20, check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.splitlines()[-1])
    assert "web.search" not in payload["runB"]["tools"]
    assert "verified" not in payload["runB"]["outcome"]
    assert "web.search" not in payload["orphanEnd"]["tools"]
    assert "verified" not in payload["orphanEnd"]["outcome"]
    assert "web.search" not in payload["conflictEnd"]["tools"]
    assert "verified" not in payload["conflictEnd"]["outcome"]
    assert payload["boundedLength"] <= 8_192
    assert payload["bounded"]["task_context"]["task_type"] == "ops.health"
    assert len(json.dumps(payload["malicious"]["task_context"])) <= 8_192
    assert "task_type" not in payload["malicious"]["task_context"]


def test_bridge_receipt_digest_binds_actual_tool_result_content() -> None:
    script = """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
process.env.EIMEMORY_ENABLE_PROMPT_INJECTION = 'false';
plugin.register({ hooks: { on(name, handler) { handlers[name] = handler; } } });
(async () => {
  async function run(runId, result) {
    const context = { runId, sessionKey: runId, sessionId: runId };
    await handlers.before_prompt_build({ prompt: 'Read evidence.', runId }, context);
    await handlers.after_tool_call({ toolName: 'read', toolCallId: runId + '-call', runId, result }, context);
    return handlers.agent_end({ runId, success: true, messages: [] }, context);
  }
  console.log(JSON.stringify({ a: await run('digest-a', 'AAAAAA'), b: await run('digest-b', 'BBBBBB') }));
})().catch((error) => { console.error(error && error.stack ? error.stack : String(error)); process.exit(1); });
""".strip()
    env = _bridge_env()
    env["EIMEMORY_HOOK_COMMAND"] = 'node -e "process.stdin.pipe(process.stdout)"'

    result = subprocess.run(
        ["node", "-e", script], cwd=Path.cwd(), env=env,
        capture_output=True, text=True, timeout=20, check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.splitlines()[-1])
    receipt_a = payload["a"]["verification_receipts"][0]
    receipt_b = payload["b"]["verification_receipts"][0]
    assert receipt_a["result_digest"] != receipt_b["result_digest"]
    assert receipt_a["signature"] != receipt_b["signature"]
