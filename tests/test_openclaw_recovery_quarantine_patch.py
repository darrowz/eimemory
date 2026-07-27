from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import time

import pytest

from deploy.patch_openclaw_restart_recovery_scope import PatchError, patch_openclaw


RECOVERY_RUNTIME = """
import fs from "node:fs";
import path from "node:path";

const actions = [];
const stores = JSON.parse(process.env.EIMEMORY_TEST_STORES || "{}");
const quarantinePath = process.env.EIMEMORY_OPENCLAW_RECOVERY_QUARANTINE_PATH;
const log = { warn() {} };
const observation = {
  liveMarkerExistsAtLoad: null,
  consumedMarkerCountAtLoad: null,
};

async function callGateway(params) {
  actions.push({
    kind: "gateway",
    method: params.method,
    sessionKey: params.params?.sessionKey,
  });
}

function loadSessionStore(storePath) {
  if (observation.liveMarkerExistsAtLoad === null) {
    observation.liveMarkerExistsAtLoad = fs.existsSync(quarantinePath);
    observation.consumedMarkerCountAtLoad = fs
      .readdirSync(path.dirname(quarantinePath))
      .filter((name) =>
        name.startsWith(`${path.basename(quarantinePath)}.consumed.`)
      ).length;
  }
  return stores[storePath];
}

async function markSessionFailed(params) {
  actions.push({
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

async function recoverStore(params) {
  const result = { recovered: 0, failed: 0, skipped: 0 };
  const store = loadSessionStore(params.storePath);
  for (const [sessionKey, entry] of Object.entries(store)) {
    if (!entry || entry.status !== "running" || entry.abortedLastRun !== true) continue;
    if (await resumeMainSession({ entry, sessionKey })) result.recovered++;
  }
  return result;
}

async function resolveRestartRecoveryStorePaths() {
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
) -> dict:
    env = os.environ.copy()
    env.update(
        {
            "EIMEMORY_OPENCLAW_RECOVERY_QUARANTINE_PATH": str(quarantine_path),
            "EIMEMORY_TEST_STORES": json.dumps(stores),
        }
    )
    completed = subprocess.run(
        ["node", str(runtime)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def _patch_fixture(tmp_path: Path) -> tuple[Path, Path]:
    root, runtime = _write_openclaw_fixture(tmp_path)
    report = patch_openclaw(root)
    assert report["status"] == "patched"
    return root, runtime


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


def test_targeted_quarantine_suppresses_only_exact_session_and_consumes_first(
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
        "consumedMarkerCountAtLoad": 1,
    }
    assert not marker.exists()
    assert [
        (action["kind"], action["sessionKey"]) for action in payload["actions"]
    ] == [
        ("gateway", "agent:main:target"),
        ("mark_failed", "agent:main:target"),
        ("gateway", "agent:main:other"),
    ]
    assert payload["actions"][0]["method"] == "message.action"
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


def test_managed_gateway_environment_sets_recovery_quarantine_path() -> None:
    dropin = Path("deploy/systemd/openclaw-gateway-eimemory.conf").read_text(
        encoding="utf-8",
    )

    assert (
        "Environment=EIMEMORY_OPENCLAW_RECOVERY_QUARANTINE_PATH="
        "/var/lib/eimemory/openclaw_recovery_quarantine.json"
    ) in dropin
