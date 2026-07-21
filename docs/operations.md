# Agent Runtime Adapter Operations

eimemory exposes the additive `agent.runtime.v1` contract through the existing
authenticated RPC service. OpenClaw remains the current authority source for
its own channel. Codex and Hermes are independent authoritative long-term
memory channels under `authority_mode=per_channel`:

- OpenClaw: existing base scope, unchanged.
- Codex: `embodied::channel::codex`.
- Hermes: `embodied::channel::hermes`.

Recall, writes, terminal evidence, metrics, and L5 samples remain inside the
selected channel. There is no implicit federation or cross-channel fallback.
Only the deployment receipt identity falls back to the base release so every
channel's evidence can bind to the same deployed eimemory commit.

## RPC security

Production loads `/etc/eimemory/rpc.env`, which contains only the strong server
secret `EIMEMORY_RPC_AUTH_TOKEN`. Clients receive the same value through their
own secret manager as `EIMEMORY_RPC_TOKEN`; never print, commit, or copy the
token into plugin files.

The production service binds RPC to the honxin Tailscale address on port 8091
and exposes a separate loopback health probe. Check it without revealing auth:

```bash
systemctl --user status eimemory-rpc.service --no-pager
curl -fsS http://127.0.0.1:8091/health
```

Client profile environment:

```text
EIMEMORY_RPC_URL=http://honxin:8091/
EIMEMORY_RPC_TOKEN=<secret supplied out of band>
EIMEMORY_TENANT_ID=default
EIMEMORY_AGENT_ID=hongtu
EIMEMORY_WORKSPACE_ID=embodied
EIMEMORY_USER_ID=<stable user identity>
EIMEMORY_ADAPTER_TIMEOUT_SECONDS=0.8
```

## Codex

Install the matching eimemory Python package in the environment where the
`eimemory` executable is visible. Register the repository marketplace:

```powershell
codex plugin marketplace add E:\eimemory\integrations\codex
codex plugin marketplace list
```

Restart the desktop app, install **eimemory** from the `eimemory-adapters`
marketplace, review/trust its hooks, and start a new Codex task. The plugin
provides `SessionStart`, `UserPromptSubmit`, `PostToolUse`, and `Stop` hooks plus
`eimemory_recall`, `eimemory_remember`, `eimemory_verify_outcome`, and
`eimemory_status` MCP tools.

Use `eimemory_status` to verify `channel=codex`, `authority_mode=per_channel`,
the `embodied::channel::codex` scope, and current release identity. To disable,
turn the plugin off in the Plugins Directory. To remove its local marketplace:

```powershell
codex plugin marketplace remove eimemory-adapters
```

The plugin never reads `transcript_path`. It sends bounded prompt context and
redacted/digested tool summaries only. `Stop` always continues Codex and never
invents verification.

## Hermes Agent

Copy the standalone provider into the active Hermes home and ensure the Hermes
Python environment can import the matching eimemory package:

```bash
mkdir -p "$HERMES_HOME/plugins/eimemory"
cp -R integrations/hermes/eimemory/. "$HERMES_HOME/plugins/eimemory/"
```

Select the provider in `$HERMES_HOME/config.yaml`:

```yaml
memory:
  provider: eimemory
```

Start a new session, then call `eimemory_status` and verify `channel=hermes`,
`authority_mode=per_channel`, `embodied::channel::hermes`, and current release
identity. Disable the external provider with:

```bash
hermes memory off
```

Hermes uses one bounded background writer and one latest-wins prefetch worker.
It ignores full history arguments, mirrors only accepted bounded turns and
built-in memory writes, and skips all writes in cron, flush, and subagent
contexts.

## Fail-open and evidence rules

Both adapters are fail-open. Timeout, authentication failure, network failure,
or an open circuit returns a stable bypass result and never stops the host:

```json
{"ok": false, "bypassed": true, "error": "adapter_unavailable", "result": null}
```

The bounded failure ledger contains timestamps, method names, and error classes
only; it never contains the RPC token or full host transcripts. Fix the service
and use `eimemory_status` to confirm recovery.

Codex `Stop` and Hermes `task_end` can enter the verified-real-task pipeline
only after `eimemory_verify_outcome` supplies an explicit verification string.
Signed tool receipt sources are channel-specific. Unverified successes and all
`session_end` events remain diagnostic/lifecycle evidence and cannot raise L5.

## Live closure checklist

1. `/health` version, 40-character commit, import root, and release path agree.
2. `eimemory_status` succeeds independently in Codex and Hermes.
3. A unique memory written in one channel is recalled there and absent from the
   other two channels.
4. A verified task appears only in that channel's current-deployment metrics.
5. An unverified success and a session end do not change verified-task counts.
6. Stop the RPC briefly and confirm both hosts continue with fail-open bypass;
   restore it and confirm status recovery.
7. Re-run OpenClaw L5 readiness and confirm the existing closure remains green.
