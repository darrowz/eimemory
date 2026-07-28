from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import time

import pytest

from deploy.patch_openclaw_restart_recovery_scope import PatchError, patch_openclaw
from eimemory.ops import openclaw_watchdog as watchdog_module


RECOVERY_RUNTIME = """
import fs from "node:fs";
import path from "node:path";

const actions = [];
const stores = JSON.parse(process.env.EIMEMORY_TEST_STORES || "{}");
const quarantinePath = process.env.EIMEMORY_OPENCLAW_RECOVERY_QUARANTINE_PATH;
const claimedPath = `${quarantinePath}.in-progress`;
const terminalStatePath = process.env.EIMEMORY_TEST_TERMINAL_STATE_PATH;
const actionLogPath = process.env.EIMEMORY_TEST_ACTION_LOG_PATH;
const crashPhase = process.env.EIMEMORY_TEST_CRASH_PHASE;
const routableStorePaths = JSON.parse(
  process.env.EIMEMORY_TEST_ROUTABLE_STORE_PATHS || "{}"
);
const log = { warn() {} };
const observation = {
  liveMarkerExistsAtLoad: null,
  claimedMarkerExistsAtLoad: null,
};
let loadedStoreCount = 0;

function recordAction(action) {
  actions.push(action);
  if (actionLogPath) {
    fs.appendFileSync(actionLogPath, `${JSON.stringify(action)}\n`);
  }
}

function readTerminalSessionKeys() {
  if (!terminalStatePath || !fs.existsSync(terminalStatePath)) return new Set();
  return new Set(JSON.parse(fs.readFileSync(terminalStatePath, "utf8")));
}

function persistTerminalSessionKey(sessionKey) {
  if (!terminalStatePath) return;
  const terminal = readTerminalSessionKeys();
  terminal.add(sessionKey);
  fs.writeFileSync(terminalStatePath, JSON.stringify([...terminal].toSorted()));
}

async function callGateway(params) {
  recordAction({
    kind: "gateway",
    method: params.method,
    sessionKey: params.params?.sessionKey,
  });
}

function loadSessionStore(storePath) {
  if (crashPhase === "before_second_store" && loadedStoreCount === 1) {
    throw new Error("simulated crash before second store");
  }
  loadedStoreCount++;
  if (observation.liveMarkerExistsAtLoad === null) {
    observation.liveMarkerExistsAtLoad = fs.existsSync(quarantinePath);
    observation.claimedMarkerExistsAtLoad = fs.existsSync(claimedPath);
  }
  const terminal = readTerminalSessionKeys();
  return Object.fromEntries(
    Object.entries(stores[storePath]).map(([sessionKey, entry]) => [
      sessionKey,
      terminal.has(sessionKey) ? { ...entry, status: "failed" } : { ...entry },
    ])
  );
}

async function markSessionFailed(params) {
  persistTerminalSessionKey(params.sessionKey);
  recordAction({
    kind: "mark_failed",
    reason: params.reason,
    sessionKey: params.sessionKey,
  });
}

async function sendUnresumableSessionNotice(params) {
  await callGateway({
    method: "message.action",
    params: { sessionKey: params.sessionKey },
  });
}

async function resumeMainSession({ entry, sessionKey }) {
  await callGateway({
    method: "agent",
    params: { sessionKey },
  });
  return entry.resumeSucceeds !== false;
}

function shouldSkipMainRecovery(entry) {
  return entry.skipRecovery === true;
}

function isRoutableRecoveryStore(params) {
  return routableStorePaths[params.sessionKey] === undefined
    || routableStorePaths[params.sessionKey] === params.storePath;
}

function hasCurrentProcessOwner(params) {
  return params.entry.currentOwner === true;
}

async function recoverStore(params) {
  const result = { recovered: 0, failed: 0, skipped: 0 };
  const store = loadSessionStore(params.storePath);
  for (const [sessionKey, entry] of Object.entries(store)) {
    if (!entry || entry.status !== "running" || entry.abortedLastRun !== true) continue;
    if (shouldSkipMainRecovery(entry, sessionKey)) {
      result.skipped++;
      continue;
    }
    if (!isRoutableRecoveryStore({
      cfg: params.cfg,
      sessionKey,
      storePath: params.storePath
    })) {
      result.skipped++;
      continue;
    }
    if (hasCurrentProcessOwner({
      activeSessionIds: new Set(),
      activeSessionKeys: new Set(),
      entry,
      sessionKey
    })) {
      result.skipped++;
      continue;
    }
    const resumeDedupeKey = sessionKey;
    if (params.resumedSessionKeys.has(resumeDedupeKey)) {
      result.skipped++;
      continue;
    }
    if (entry.pendingFinalDelivery === true && entry.pendingFinalDeliveryText) {
      if (await resumeMainSession({ entry, sessionKey })) {
        params.resumedSessionKeys.add(resumeDedupeKey);
        result.recovered++;
      } else result.failed++;
      continue;
    }
    if (await resumeMainSession({ entry, sessionKey })) {
      params.resumedSessionKeys.add(resumeDedupeKey);
      result.recovered++;
    } else result.failed++;
  }
  return result;
}

async function resolveRestartRecoveryStorePaths() {
  if (crashPhase === "after_claim") {
    throw new Error("simulated crash after quarantine claim");
  }
  return Object.keys(stores);
}

async function recoverRestartAbortedMainSessions(params = {}) {
  const result = { recovered: 0, failed: 0, skipped: 0 };
  const resumedSessionKeys = params.resumedSessionKeys ?? new Set();
  for (const storePath of await resolveRestartRecoveryStorePaths(params)) {
    const storeResult = await recoverStore({ storePath, resumedSessionKeys });
    result.recovered += storeResult.recovered;
    result.failed += storeResult.failed;
    result.skipped += storeResult.skipped;
  }
  return result;
}

async function recoverStartupOrphanedMainSessions(params = {}) {
  return recoverRestartAbortedMainSessions(params);
}

const result = await recoverRestartAbortedMainSessions();
console.log(JSON.stringify({ actions, observation, result }));
""".strip()

AGENT_TOOLS_RUNTIME = """
function createSessionsHistoryTool(opts) {
  const gatewayCall = opts?.callGateway ?? callGateway;
  return gatewayCall({ method: "chat.history", params: {} });
}
function createSessionsListTool(opts) {
  const gatewayCall = opts?.callGateway ?? callGateway;
  return gatewayCall({ method: "sessions.list", params: {} });
}
function createSessionsSendTool(opts) {
  const gatewayCall = opts?.callGateway ?? callGateway;
  return gatewayCall({ method: "sessions.resolve", params: {} });
}
let openClawToolsDeps = { callGateway };
""".strip()

GATEWAY_RUNTIME = """
const AGENT_RUNTIME_IDENTITY_METHODS = new Set(["cron.status", "cron.run"]);
async function callGatewayTool(method, opts, params, extra) {
  const gateway = resolveGatewayOptions(opts);
  const scopes = Array.isArray(extra?.scopes)
    ? extra.scopes
    : resolveLeastPrivilegeOperatorScopesForMethod(method, params);
  const agentRuntimeIdentityToken = resolveAgentRuntimeIdentityTokenForGatewayTool({
    method,
    opts,
    target: gateway.target,
  });
  return await callGateway({
    url: gateway.url,
    token: gateway.token,
    method,
    params,
    clientName: GATEWAY_CLIENT_NAMES.GATEWAY_CLIENT,
    clientDisplayName: "agent",
    mode: GATEWAY_CLIENT_MODES.BACKEND,
    ...(agentRuntimeIdentityToken ? { agentRuntimeIdentityToken } : {}),
    scopes,
  });
}
""".strip()

CALL_RUNTIME = """
async function callGateway(opts) {
    const callerMode = opts.mode ?? GATEWAY_CLIENT_MODES.BACKEND;
    const callerName = opts.clientName ?? GATEWAY_CLIENT_NAMES.GATEWAY_CLIENT;
    if (callerMode === GATEWAY_CLIENT_MODES.CLI || callerName === GATEWAY_CLIENT_NAMES.CLI) {
        return await callGatewayCli(opts);
    }
    if (Array.isArray(opts.scopes)) {
        return await callGatewayWithScopes({
            ...opts,
            mode: callerMode,
            clientName: callerName,
        }, opts.scopes);
    }
    return await callGatewayLeastPrivilege({
        ...opts,
        mode: callerMode,
        clientName: callerName,
    });
}
""".strip()


def _write_openclaw_fixture(
    tmp_path: Path,
    *,
    version: str = "2026.7.1-2",
) -> tuple[Path, Path]:
    root = tmp_path / "openclaw"
    dist = root / "dist"
    dist.mkdir(parents=True)
    (root / "package.json").write_text(
        json.dumps({"type": "module", "version": version}),
        encoding="utf-8",
    )
    runtime = dist / "main-session-restart-recovery-test.js"
    runtime.write_text(RECOVERY_RUNTIME + "\n", encoding="utf-8")
    (dist / "openclaw-tools-test.js").write_text(
        AGENT_TOOLS_RUNTIME + "\n",
        encoding="utf-8",
    )
    (dist / "gateway-test.js").write_text(
        GATEWAY_RUNTIME + "\n",
        encoding="utf-8",
    )
    (dist / "call-test.js").write_text(
        CALL_RUNTIME + "\n",
        encoding="utf-8",
    )
    return root, runtime


def _quarantine(
    *,
    mode: str = "targeted",
    session_ids: list[str] | None = None,
) -> dict:
    now = time.time()
    return {
        "schema": "openclaw_recovery_quarantine.v1",
        "trigger": "stuck_session",
        "created_at_ts": now - 1,
        "expires_at_ts": now + 300,
        "mode": mode,
        "session_ids": ["session-target"] if session_ids is None else session_ids,
        "consumed": False,
    }


def _run_runtime(
    runtime: Path,
    quarantine_path: Path,
    *,
    stores: dict,
    action_log_path: Path | None = None,
    crash_phase: str = "",
    expect_success: bool = True,
    routable_store_paths: dict[str, str] | None = None,
    terminal_state_path: Path | None = None,
) -> dict | subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "EIMEMORY_OPENCLAW_RECOVERY_QUARANTINE_PATH": str(quarantine_path),
            "EIMEMORY_TEST_STORES": json.dumps(stores),
            "EIMEMORY_TEST_CRASH_PHASE": crash_phase,
            "EIMEMORY_TEST_ROUTABLE_STORE_PATHS": json.dumps(
                routable_store_paths or {}
            ),
        }
    )
    if action_log_path is not None:
        env["EIMEMORY_TEST_ACTION_LOG_PATH"] = str(action_log_path)
    if terminal_state_path is not None:
        env["EIMEMORY_TEST_TERMINAL_STATE_PATH"] = str(terminal_state_path)
    completed = subprocess.run(
        ["node", str(runtime)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
    )
    if not expect_success:
        assert completed.returncode != 0
        return completed
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def _read_action_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def _patch_fixture(tmp_path: Path) -> tuple[Path, Path]:
    root, runtime = _write_openclaw_fixture(tmp_path)
    report = patch_openclaw(root)
    assert report["status"] == "patched"
    return root, runtime


def test_claimed_quarantine_replays_after_crash_immediately_after_rename(
    tmp_path: Path,
) -> None:
    _, runtime = _patch_fixture(tmp_path)
    marker = tmp_path / "openclaw_recovery_quarantine.json"
    claimed = tmp_path / "openclaw_recovery_quarantine.json.in-progress"
    terminal_state = tmp_path / "terminal.json"
    action_log = tmp_path / "actions.jsonl"
    marker.write_text(
        json.dumps(_quarantine(mode="all_previous_lifecycle", session_ids=[])),
        encoding="utf-8",
    )
    stores = {
        "store-a": {
            "agent:main:one": {
                "status": "running",
                "abortedLastRun": True,
                "sessionId": "session-one",
            }
        }
    }

    _run_runtime(
        runtime,
        marker,
        stores=stores,
        action_log_path=action_log,
        crash_phase="after_claim",
        expect_success=False,
        terminal_state_path=terminal_state,
    )

    assert not marker.exists()
    assert claimed.exists()
    claimed_state = json.loads(claimed.read_text(encoding="utf-8"))
    claimed_state["expires_at_ts"] = time.time() - 1
    claimed.write_text(json.dumps(claimed_state), encoding="utf-8")

    payload = _run_runtime(
        runtime,
        marker,
        stores=stores,
        action_log_path=action_log,
        terminal_state_path=terminal_state,
    )

    assert payload["result"] == {"recovered": 0, "failed": 1, "skipped": 0}
    assert not claimed.exists()
    assert not [
        action
        for action in _read_action_log(action_log)
        if action.get("method") == "agent"
    ]


def test_claimed_quarantine_replays_remaining_store_after_crash(
    tmp_path: Path,
) -> None:
    _, runtime = _patch_fixture(tmp_path)
    marker = tmp_path / "openclaw_recovery_quarantine.json"
    claimed = tmp_path / "openclaw_recovery_quarantine.json.in-progress"
    terminal_state = tmp_path / "terminal.json"
    action_log = tmp_path / "actions.jsonl"
    marker.write_text(
        json.dumps(_quarantine(mode="all_previous_lifecycle", session_ids=[])),
        encoding="utf-8",
    )
    stores = {
        "store-a": {
            "agent:main:one": {
                "status": "running",
                "abortedLastRun": True,
                "sessionId": "session-one",
            }
        },
        "store-b": {
            "agent:main:two": {
                "status": "running",
                "abortedLastRun": True,
                "sessionId": "session-two",
            }
        },
    }

    _run_runtime(
        runtime,
        marker,
        stores=stores,
        action_log_path=action_log,
        crash_phase="before_second_store",
        expect_success=False,
        terminal_state_path=terminal_state,
    )

    assert claimed.exists()
    assert json.loads(terminal_state.read_text(encoding="utf-8")) == [
        "agent:main:one"
    ]
    first_actions = _read_action_log(action_log)
    assert [action["kind"] for action in first_actions] == [
        "mark_failed",
        "gateway",
    ]

    payload = _run_runtime(
        runtime,
        marker,
        stores=stores,
        action_log_path=action_log,
        terminal_state_path=terminal_state,
    )

    assert payload["result"] == {"recovered": 0, "failed": 1, "skipped": 0}
    assert not claimed.exists()
    assert json.loads(terminal_state.read_text(encoding="utf-8")) == [
        "agent:main:one",
        "agent:main:two",
    ]
    assert not [
        action
        for action in _read_action_log(action_log)
        if action.get("method") == "agent"
    ]


def test_quarantine_respects_recovery_gates_and_deduplicates_eligible_copies(
    tmp_path: Path,
) -> None:
    _, runtime = _patch_fixture(tmp_path)
    marker = tmp_path / "openclaw_recovery_quarantine.json"
    marker.write_text(
        json.dumps(_quarantine(mode="all_previous_lifecycle", session_ids=[])),
        encoding="utf-8",
    )

    payload = _run_runtime(
        runtime,
        marker,
        stores={
            "store-a": {
                "agent:main:skip": {
                    "status": "running",
                    "abortedLastRun": True,
                    "sessionId": "session-skip",
                    "skipRecovery": True,
                },
                "agent:main:owned": {
                    "status": "running",
                    "abortedLastRun": True,
                    "sessionId": "session-owned",
                    "currentOwner": True,
                },
                "agent:main:routed": {
                    "status": "running",
                    "abortedLastRun": True,
                    "sessionId": "session-routed",
                },
            },
            "store-b": {
                "agent:main:routed": {
                    "status": "running",
                    "abortedLastRun": True,
                    "sessionId": "session-routed",
                },
                "agent:main:duplicate": {
                    "status": "running",
                    "abortedLastRun": True,
                    "sessionId": "session-duplicate",
                    "pendingFinalDelivery": True,
                    "pendingFinalDeliveryText": "captured",
                },
            },
            "store-c": {
                "agent:main:duplicate": {
                    "status": "running",
                    "abortedLastRun": True,
                    "sessionId": "session-duplicate",
                }
            },
        },
        routable_store_paths={"agent:main:routed": "store-b"},
    )

    assert payload["result"] == {"recovered": 0, "failed": 2, "skipped": 4}
    assert [
        (action["kind"], action["sessionKey"]) for action in payload["actions"]
    ] == [
        ("mark_failed", "agent:main:routed"),
        ("gateway", "agent:main:routed"),
        ("mark_failed", "agent:main:duplicate"),
        ("gateway", "agent:main:duplicate"),
    ]
    assert all(
        action.get("method") != "agent" for action in payload["actions"]
    )


def test_failed_watchdog_restart_clears_marker_before_manual_startup(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _, runtime = _patch_fixture(tmp_path)
    marker = tmp_path / "openclaw_recovery_quarantine.json"
    with monkeypatch.context() as scoped:
        scoped.setattr(
            watchdog_module,
            "read_unit_journal",
            lambda *args, **kwargs: (
                "[diagnostic] stuck session: "
                "sessionId=session-target state=processing age=150s"
            ),
        )
        scoped.setattr(
            watchdog_module,
            "resolve_unit_control_group",
            lambda *args, **kwargs: "",
        )
        scoped.setattr(
            watchdog_module.subprocess,
            "run",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                subprocess.CalledProcessError(1, args[0])
            ),
        )

        result = watchdog_module.main(
            [
                "--state-path",
                str(tmp_path / "watchdog-state.json"),
                "--quarantine-path",
                str(marker),
            ]
        )

    assert result == 2
    assert not marker.exists()

    payload = _run_runtime(
        runtime,
        marker,
        stores={
            "store-a": {
                "agent:main:manual": {
                    "status": "running",
                    "abortedLastRun": True,
                    "sessionId": "session-target",
                }
            }
        },
    )

    assert payload["result"] == {"recovered": 1, "failed": 0, "skipped": 0}
    assert payload["actions"][-1]["method"] == "agent"


def test_patch_applies_once_and_second_application_is_idempotent(tmp_path: Path) -> None:
    root, runtime = _write_openclaw_fixture(tmp_path)

    first = patch_openclaw(root)
    patched = runtime.read_text(encoding="utf-8")
    second = patch_openclaw(root)

    assert first["status"] == "patched"
    assert second["status"] == "already_patched"
    assert patched.count("function takeEimemoryRecoveryQuarantine()") == 1
    assert patched.count("function shouldQuarantineRestartRecovery(") == 1
    assert runtime.read_text(encoding="utf-8") == patched


def test_patch_is_gated_off_for_unaffected_openclaw_version(tmp_path: Path) -> None:
    root, runtime = _write_openclaw_fixture(tmp_path, version="2026.7.2")
    original = runtime.read_text(encoding="utf-8")

    report = patch_openclaw(root)

    assert report == {"status": "not_affected", "version": "2026.7.2"}
    assert runtime.read_text(encoding="utf-8") == original


def test_patch_qualifies_current_recovery_loop_from_other_store_loops(
    tmp_path: Path,
) -> None:
    root, runtime = _write_openclaw_fixture(tmp_path)
    text = runtime.read_text(encoding="utf-8").replace(
        "async function recoverStore(params) {",
        """
async function markInterruptedMainSessions(params) {
  for (const storePath of await resolveRestartRecoveryStorePaths(params)) {
    const storeResult = await applyRestartRecoveryLifecycle({ storePath });
  }
}

async function recoverStore(params) {
""".strip(),
        1,
    )
    runtime.write_text(text, encoding="utf-8")

    first = patch_openclaw(root)
    second = patch_openclaw(root)

    assert first["status"] == "patched"
    assert second["status"] == "already_patched"
    assert runtime.read_text(encoding="utf-8").count(
        "function takeEimemoryRecoveryQuarantine()"
    ) == 1


def test_targeted_quarantine_suppresses_only_exact_session_and_claims_first(
    tmp_path: Path,
) -> None:
    _, runtime = _patch_fixture(tmp_path)
    marker = tmp_path / "openclaw_recovery_quarantine.json"
    marker.write_text(json.dumps(_quarantine()), encoding="utf-8")

    payload = _run_runtime(
        runtime,
        marker,
        stores={
            "store-a": {
                "agent:main:target": {
                    "status": "running",
                    "abortedLastRun": True,
                    "sessionId": "session-target",
                },
                "agent:main:other": {
                    "status": "running",
                    "abortedLastRun": True,
                    "sessionId": "session-other",
                },
            }
        },
    )

    assert payload["result"] == {"recovered": 1, "failed": 1, "skipped": 0}
    assert payload["observation"] == {
        "liveMarkerExistsAtLoad": False,
        "claimedMarkerExistsAtLoad": True,
    }
    assert not marker.exists()
    assert [
        (action["kind"], action["sessionKey"]) for action in payload["actions"]
    ] == [
        ("mark_failed", "agent:main:target"),
        ("gateway", "agent:main:target"),
        ("gateway", "agent:main:other"),
    ]
    assert payload["actions"][1]["method"] == "message.action"
    assert payload["actions"][2]["method"] == "agent"


def test_all_previous_lifecycle_quarantine_suppresses_every_running_orphan(
    tmp_path: Path,
) -> None:
    _, runtime = _patch_fixture(tmp_path)
    marker = tmp_path / "openclaw_recovery_quarantine.json"
    marker.write_text(
        json.dumps(
            _quarantine(mode="all_previous_lifecycle", session_ids=[]),
        ),
        encoding="utf-8",
    )

    payload = _run_runtime(
        runtime,
        marker,
        stores={
            "store-a": {
                "agent:main:one": {
                    "status": "running",
                    "abortedLastRun": True,
                    "sessionId": "session-one",
                },
            },
            "store-b": {
                "agent:main:two": {
                    "status": "running",
                    "abortedLastRun": True,
                    "sessionId": "session-two",
                },
            }
        },
    )

    assert payload["result"] == {"recovered": 0, "failed": 2, "skipped": 0}
    assert not [
        action
        for action in payload["actions"]
        if action["kind"] == "gateway" and action["method"] == "agent"
    ]
    assert [action["kind"] for action in payload["actions"]].count("mark_failed") == 2


def test_expired_quarantine_does_not_suppress_normal_restart_recovery(
    tmp_path: Path,
) -> None:
    _, runtime = _patch_fixture(tmp_path)
    marker = tmp_path / "openclaw_recovery_quarantine.json"
    quarantine = _quarantine()
    quarantine["created_at_ts"] = time.time() - 600
    quarantine["expires_at_ts"] = time.time() - 300
    marker.write_text(json.dumps(quarantine), encoding="utf-8")

    payload = _run_runtime(
        runtime,
        marker,
        stores={
            "store-a": {
                "agent:main:target": {
                    "status": "running",
                    "abortedLastRun": True,
                    "sessionId": "session-target",
                }
            }
        },
    )

    assert payload["result"] == {"recovered": 1, "failed": 0, "skipped": 0}
    assert payload["actions"][-1]["method"] == "agent"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda state: state.update(schema="openclaw_recovery_quarantine.v2"),
        lambda state: state.update(created_at_ts="now"),
        lambda state: state.update(expires_at_ts=state["created_at_ts"] - 1),
        lambda state: state.update(mode="manual"),
        lambda state: state.update(session_ids=["session-target", 7]),
        lambda state: state.update(consumed=True),
        lambda state: state.update(mode="targeted", session_ids=[]),
        lambda state: state.update(
            mode="all_previous_lifecycle",
            session_ids=["session-target"],
        ),
    ],
    ids=[
        "schema",
        "timestamp_type",
        "timestamp_order",
        "mode",
        "string_array",
        "consumed",
        "targeted_without_sessions",
        "all_lifecycle_with_sessions",
    ],
)
def test_malformed_quarantine_does_not_suppress_recovery(
    tmp_path: Path,
    mutate,
) -> None:
    _, runtime = _patch_fixture(tmp_path)
    marker = tmp_path / "openclaw_recovery_quarantine.json"
    quarantine = _quarantine()
    mutate(quarantine)
    marker.write_text(json.dumps(quarantine), encoding="utf-8")

    payload = _run_runtime(
        runtime,
        marker,
        stores={
            "store-a": {
                "agent:main:target": {
                    "status": "running",
                    "abortedLastRun": True,
                    "sessionId": "session-target",
                }
            }
        },
    )

    assert payload["result"] == {"recovered": 1, "failed": 0, "skipped": 0}
    assert payload["actions"][-1]["method"] == "agent"


def test_no_quarantine_preserves_normal_restart_recovery(tmp_path: Path) -> None:
    _, runtime = _patch_fixture(tmp_path)
    marker = tmp_path / "openclaw_recovery_quarantine.json"

    payload = _run_runtime(
        runtime,
        marker,
        stores={
            "store-a": {
                "agent:main:normal": {
                    "status": "running",
                    "abortedLastRun": True,
                    "sessionId": "session-normal",
                }
            }
        },
    )

    assert payload["result"] == {"recovered": 1, "failed": 0, "skipped": 0}
    assert payload["actions"][-1] == {
        "kind": "gateway",
        "method": "agent",
        "sessionKey": "agent:main:normal",
    }


def test_patch_fails_closed_when_current_recovery_anchor_does_not_match(
    tmp_path: Path,
) -> None:
    root, runtime = _write_openclaw_fixture(tmp_path)
    mismatched = runtime.read_text(encoding="utf-8").replace(
        'entry.abortedLastRun !== true',
        'entry.wasAborted !== true',
    )
    runtime.write_text(mismatched, encoding="utf-8")

    with pytest.raises(PatchError, match="recovery guard anchor"):
        patch_openclaw(root)

    assert runtime.read_text(encoding="utf-8") == mismatched


def test_patch_fails_closed_when_affected_runtime_has_no_recovery_entrypoints(
    tmp_path: Path,
) -> None:
    root, runtime = _write_openclaw_fixture(tmp_path)
    mismatched = runtime.read_text(encoding="utf-8").replace(
        "recoverStore",
        "changedRecoverStore",
    ).replace(
        "recoverRestartAbortedMainSessions",
        "changedRecoverRestartAbortedMainSessions",
    )
    runtime.write_text(mismatched, encoding="utf-8")

    with pytest.raises(PatchError, match="recovery entrypoint anchors"):
        patch_openclaw(root)

    assert runtime.read_text(encoding="utf-8") == mismatched


def test_patch_does_not_use_same_shaped_loop_from_wrong_function(
    tmp_path: Path,
) -> None:
    root, runtime = _write_openclaw_fixture(tmp_path)
    text = runtime.read_text(encoding="utf-8")
    outer_start = text.index("async function recoverRestartAbortedMainSessions")
    outer_end = text.index("async function recoverStartupOrphanedMainSessions", outer_start)
    replacement = """
async function decoyRestartRecovery(params = {}) {
  const result = { recovered: 0, failed: 0, skipped: 0 };
  const resumedSessionKeys = new Set();
  for (const storePath of await resolveRestartRecoveryStorePaths(params)) {
    const storeResult = await recoverStore({ storePath, resumedSessionKeys });
    result.recovered += storeResult.recovered;
    result.failed += storeResult.failed;
    result.skipped += storeResult.skipped;
  }
  return result;
}

async function recoverRestartAbortedMainSessions(params = {}) {
  const changedResult = { recovered: 0, failed: 0, skipped: 0 };
  for (const storePath of await resolveChangedStorePaths(params)) {
    const changedStoreResult = await changedRecoverStore({ storePath });
    changedResult.recovered += changedStoreResult.recovered;
  }
  return changedResult;
}

""".lstrip()
    mismatched = text[:outer_start] + replacement + text[outer_end:]
    runtime.write_text(mismatched, encoding="utf-8")

    with pytest.raises(PatchError, match="recovery store loop anchor"):
        patch_openclaw(root)

    assert runtime.read_text(encoding="utf-8") == mismatched


@pytest.mark.parametrize(
    ("original", "replacement"),
    [
        (
            "return quarantine.session_ids.includes(entry.sessionId) "
            "|| quarantine.session_ids.includes(sessionKey);",
            "return false;",
        ),
        (
            'quarantine?.schema === "openclaw_recovery_quarantine.v1"',
            'quarantine?.schema === "openclaw_recovery_quarantine.v2"',
        ),
        (
            "fs.renameSync(EIMEMORY_RECOVERY_QUARANTINE_PATH, "
            "EIMEMORY_RECOVERY_QUARANTINE_CLAIM_PATH);",
            "void EIMEMORY_RECOVERY_QUARANTINE_CLAIM_PATH;",
        ),
        (
            "await markSessionFailed({",
            "await Promise.resolve({",
        ),
        (
            "await sendUnresumableSessionNotice({",
            "await Promise.resolve({",
        ),
        (
            "const resumeDedupeKey = sessionKey;",
            "const changedResumeDedupeKey = sessionKey;",
        ),
    ],
    ids=[
        "matcher",
        "schema",
        "atomic_claim",
        "failure_write",
        "notice",
        "dedupe_gate",
    ],
)
def test_patch_fails_closed_for_partial_existing_quarantine_patch(
    tmp_path: Path,
    original: str,
    replacement: str,
) -> None:
    root, runtime = _write_openclaw_fixture(tmp_path)
    assert patch_openclaw(root)["status"] == "patched"
    partial = runtime.read_text(encoding="utf-8").replace(
        original,
        replacement,
        1,
    )
    runtime.write_text(partial, encoding="utf-8")

    with pytest.raises(PatchError, match="incomplete recovery quarantine patch"):
        patch_openclaw(root)

    assert runtime.read_text(encoding="utf-8") == partial


def test_managed_gateway_environment_sets_recovery_quarantine_path() -> None:
    dropin = Path("deploy/systemd/openclaw-gateway-eimemory.conf").read_text(
        encoding="utf-8",
    )

    assert (
        "Environment=EIMEMORY_OPENCLAW_RECOVERY_QUARANTINE_PATH="
        "/var/lib/eimemory/openclaw_recovery_quarantine.json"
    ) in dropin
