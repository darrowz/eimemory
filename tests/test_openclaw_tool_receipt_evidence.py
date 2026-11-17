"""Exercise positive evidence admission through the registered host callbacks."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

from eimemory.governance.tool_receipts import verify_tool_receipt


ROOT = Path(__file__).resolve().parents[1]
KEY = "synthetic-openclaw-positive-evidence-test-key-32-characters"


def _text(value: str) -> dict:
    return {"content": [{"type": "text", "text": value}]}


CASES = [
    ("observed-nonzero", _text("Process exited with code 7"), False),
    ("observed-unexecuted", _text("Verification was not executed; command omitted."), False),
    ("unknown-prose", _text("The verification outcome is unknown."), False),
    ("opaque-content", _text("The checker returned an informational note."), False),
    ("opaque-string", "Informational note only.", False),
    ("opaque-array", [{"type": "text", "text": "Informational note only."}], False),
    ("boolean-only", True, False),
    ("empty-object", {}, False),
    ("empty-content", {"content": []}, False),
    ("null-result", None, False),
    ("count-without-success", {"resultCount": 3}, False),
    ("empty-count", {"ok": True, "resultCount": 0}, False),
    ("completed-without-verdict", {"status": "completed"}, False),
    ("unknown-status", {"ok": True, "status": "unknown"}, False),
    ("conflicting-details", {"ok": True, "details": {"success": False}}, False),
    ("conflicting-statuses", {"status": "success", "details": {"status": "running"}}, False),
    ("nonzero-with-success", {"success": True, "exitCode": 23}, False),
    ("conflicting-exit-aliases", {"ok": True, "exitCode": 0, "exit_code": 19}, False),
    ("null-exit", {"ok": True, "exitCode": None}, False),
    ("empty-exit", {"ok": True, "exitCode": ""}, False),
    ("boolean-exit", {"ok": True, "exitCode": False}, False),
    ("invalid-ok", {"ok": "true", "status": "success"}, False),
    ("invalid-error-flag", {"ok": True, "isError": "false"}, False),
    ("executed-false", {"ok": True, "executed": False}, False),
    ("skipped-flag", {"ok": True, "skipped": True}, False),
    ("cancelled-flag", {"exitCode": 0, "cancelled": True}, False),
    ("nested-not-run", {"ok": True, "details": {"executed": False}}, False),
    ("nonzero-text-conflict", {"ok": True, **_text("Process exited with code 41")}, False),
    ("return-code-conflict", {"exitCode": 0, **_text("Return code: 29")}, False),
    ("unexecuted-text-conflict", {"success": True, **_text("The checks were not run.")}, False),
    ("unknown-text-conflict", {"ok": True, **_text("Verification outcome: unknown")}, False),
    ("skipped-text-conflict", {"ok": True, **_text("The test execution was skipped.")}, False),
    ("queued-text-conflict", {"ok": True, **_text("The command is queued.")}, False),
    ("failed-tests-conflict", {"exitCode": 0, **_text("8 passed, 2 failed")}, False),
    ("failed-test-record", {"exitCode": 0, **_text("FAILED test_widget.py::test_size")}, False),
    ("fatal-record", {"ok": True, **_text("ERROR: verifier could not open the input")}, False),
    ("json-failed-exit", {"ok": True, **_text('{"status":"failed","exitCode":7}')}, False),
    ("json-start-error", {"ok": True, **_text('{"error":"verification could not start"}')}, False),
    ("json-unknown", {"ok": True, **_text('{"status":"unknown"}')}, False),
    ("json-not-executed", {"success": True, **_text('{"details":{"executed":false}}')}, False),
    ("json-array-cancelled", {"exitCode": 0, **_text('[{"cancelled":true}]')}, False),
    ("json-positive-without-envelope", _text('{"ok":true,"exitCode":0}'), False),
    ("json-depth-budget", {"ok": True, **_text('{"nested":' * 12 + '{}' + '}' * 12)}, False),
    ("sgr-error-record", {"ok": True, **_text("\x1b[31mERROR: verifier could not start\x1b[0m")}, False),
    ("sgr-json-failure", {"ok": True, **_text('\x1b[31m{"status":"failed","exitCode":13}\x1b[0m')}, False),
    ("sgr-does-not-strip-other-csi", "\x1b[2J12 passed, 0 failed", False),
    ("zero-tests", "0 passed, 0 failed", False),
    ("quoted-summary", 'The expected output is "12 passed, 0 failed".', False),
    ("summary-plus-unknown", "12 passed, 0 failed\nVerification outcome: unknown", False),
    ("summary-plus-opaque", "12 passed, 0 failed\nExecution details omitted.", False),
    ("structured-ok", {"ok": True}, True),
    ("structured-success", {"success": True, **_text("Read 2 source documents.")}, True),
    ("structured-exit-zero", {"exitCode": 0}, True),
    ("structured-string-exit-zero", {"exit_code": "0"}, True),
    ("structured-completed-zero", {"status": "completed", "exitCode": 0}, True),
    ("structured-passed", {"status": "passed"}, True),
    ("structured-details", {"details": {"status": "success", "exitCode": 0}}, True),
    ("structured-results", {"ok": True, "resultCount": 2}, True),
    ("error-test-name", {"exitCode": 0, **_text("test_error_handling.py::test_timeout PASSED\n5 passed")}, True),
    ("ordinary-error-prose", {"ok": True, **_text("Read the failure recovery and error handling guide.")}, True),
    ("json-ordinary-prose", {"ok": True, **_text('{"note":"Read the error handling guide."}')}, True),
    ("json-data-records", {"ok": True, **_text('{"records":[{"id":"incident-17","status":"failed","title":"Historical incident"}],"count":1}')}, True),
    ("json-quoted-example", {"ok": True, **_text('{"quoted_example":"ERROR: verifier unavailable","explanation":"This is a documentation example."}')}, True),
    ("negative-words-in-path", {"ok": True, **_text("Saved reports/failed-retry/timeout-handling.txt")}, True),
    ("executed-positive", {"success": True, "executed": True, "cancelled": False, "skipped": False}, True),
    ("existing-pytest-summary", "12 passed, 0 failed", True),
    ("sgr-pytest-summary", "\x1b[32m12 passed, 0 failed\x1b[0m", True),
    ("pytest-summary-content", _text("7 passed in 0.48s"), True),
    ("pytest-summary-array", [{"type": "text", "text": "3 passed, 0 errors in 1.25s"}], True),
]


@pytest.fixture(scope="module")
def captured_terminals(tmp_path_factory):
    state = tmp_path_factory.mktemp("openclaw-positive-evidence")
    inputs = [{"id": name, "result": result} for name, result, _ in CASES]
    inputs.extend([
        {"id": "summary-from-read", "result": "12 passed, 0 failed", "toolName": "read"},
        {"id": "summary-from-cat", "result": "12 passed, 0 failed", "command": "cat pytest-report.txt"},
        {"id": "event-status-conflict", "result": {"ok": True}, "status": "running"},
        {"id": "event-error-conflict", "result": {"ok": True}, "error": "execution cancelled"},
    ])
    (state / "inputs.json").write_text(json.dumps(inputs), encoding="utf-8")
    transport = state / "capture-hook.js"
    transport.write_text("process.stdin.pipe(process.stdout);", encoding="utf-8")
    script = r"""
const fs = require('node:fs');
const plugin = require(process.argv[1]).default;
const handlers = {};
plugin.register({hooks:{on(name, handler){handlers[name] = handler;}}});
(async () => {
  const outputs = {};
  for (const item of JSON.parse(fs.readFileSync(process.argv[2], 'utf8'))) {
    const context = {userId:'synthetic-user', runId:item.id, sessionId:'session-' + item.id,
      sessionKey:'key-' + item.id};
    await handlers.before_prompt_build({prompt:'Run the verification.',runId:item.id},context);
    await handlers.after_tool_call({toolName:item.toolName || 'exec',toolCallId:'call-' + item.id,
      runId:item.id,params:{command:item.command || 'python -m pytest -q'},result:item.result,
      ...(item.status ? {status:item.status}:{}),...(item.error ? {error:item.error}:{})},context);
    outputs[item.id] = await handlers.agent_end({runId:item.id,success:true,
      messages:[{role:'assistant',content:'Verification complete.'}]},context);
  }
  console.log(JSON.stringify(outputs));
})().catch(error => { console.error(error.stack);process.exitCode=1; });
"""
    env = os.environ.copy()
    for name in ("EIMEMORY_RPC_URL", "EIMEMORY_RPC_AUTH_TOKEN", "EIMEMORY_RPC_TOKEN"):
        env.pop(name, None)
    env.update({
        "EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY": KEY,
        "EIMEMORY_ROOT": str(state),
        "OPENCLAW_STATE_DIR": str(state),
        "OPENCLAW_CONFIG_PATH": str(state / "openclaw.json"),
        "EIMEMORY_ENABLE_PROMPT_INJECTION": "false",
        "EIMEMORY_REPLY_DELIVERY_STATE_PATH": str(state / "reply.json"),
        "EIMEMORY_REPLY_DELIVERY_ATTEMPTS_PATH": str(state / "attempts.json"),
        "EIMEMORY_RELEASE_CLOSURE_SIGNAL_PATH": str(state / "release-signal.json"),
        "EIMEMORY_ADAPTER_FAILURE_LEDGER": str(state / "failure.jsonl"),
        "EIMEMORY_HOOK_COMMAND": f'node "{transport}"',
    })
    process = subprocess.run(
        ["node", "-e", script, str(ROOT / "integrations/openclaw/eimemory-bridge/index.js"),
         str(state / "inputs.json")],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert process.returncode == 0, process.stderr
    return json.loads(process.stdout.splitlines()[-1])


@pytest.mark.parametrize("name,result,accepted", CASES, ids=[case[0] for case in CASES])
def test_registered_callbacks_require_positive_evidence(captured_terminals, name, result, accepted):
    terminal = captured_terminals[name]
    receipts = terminal.get("verification_receipts", [])
    assert len(receipts) == int(accepted), (name, result, terminal)
    assert (terminal["outcome"].get("verified") is True) is accepted
    if accepted:
        assert terminal["query"] == "Run the verification."
        receipt = receipts[0]
        assert receipt["tool_call_id"] == "call-" + name
        assert verify_tool_receipt(receipt, session_id="session-" + name, run_id=name, key=KEY)
        assert not verify_tool_receipt(receipt, session_id="another-session", run_id=name, key=KEY)
        assert not verify_tool_receipt(receipt, session_id="session-" + name, run_id="another-run", key=KEY)
    else:
        assert terminal["outcome"]["verification"] == ""


@pytest.mark.parametrize("name", ["summary-from-read", "summary-from-cat", "event-status-conflict", "event-error-conflict"])
def test_summary_requires_test_execution_and_no_host_conflict(captured_terminals, name):
    terminal = captured_terminals[name]
    assert not terminal.get("verification_receipts")
    assert terminal["outcome"].get("verified") is not True


def test_sgr_comparison_preserves_original_result_digest(captured_terminals):
    plain = captured_terminals["existing-pytest-summary"]["verification_receipts"][0]
    colored = captured_terminals["sgr-pytest-summary"]["verification_receipts"][0]
    assert plain["result_digest"] != colored["result_digest"]
