# Agent discovery and per-agent recall gates (1.14.87)

The recall-quality gate evaluates every agent separately. Agent names are not
hard-coded anywhere in the gate, nightly or release-closure logic.

## How agents are discovered

At run time, an agent is any `agent_id` that has natural proactive decisions
in `proactive_decisions` during the last 14 days. These are excluded:

- deploy replays (`hermes-release-*` sessions)
- acceptance-generated decisions
- scheduled sessions (`cron_*`)

This discovers whatever names a given installation uses. Each Hermes profile
writes its own agent id from its own `EIMEMORY_AGENT_ID`. Each discovered agent
is evaluated in its own base scope: the installation's bound logical tenant,
workspace and user, with that agent's id.

To override discovery, set `EIMEMORY_RECALL_GATE_AGENTS=agent-a,agent-b` in the
nightly and release-closure environment. That exact list is then evaluated.

## Rule (unchanged thresholds, no pooling)

- Each agent gets its own production-recall collection, auto-review and gate,
  using the same thresholds as before.
- The combined verdict passes only if every evaluated agent passes.
- If any agent fails on a metric, the combined verdict fails, with that agent
  and reason named.
- If an agent's only blocker is incomplete evidence, the combined verdict is an
  evidence wait. It never passes.
- With zero agents (no natural traffic yet), the verdict is an evidence wait.
  With one agent, that agent alone decides.
- The nightly `recall_quality_gate` equals the primary gate when every agent
  passes. Otherwise it is forced to `ok=false`. The per-agent layer can only
  make the gate stricter, never weaker.
- Release closure also runs the configured production gate for every other
  discovered agent. An agent that is neither accepted nor in the same
  data-accumulating bootstrap state blocks the closure (`agent:<id>:<reason>`).
  So does an agent still accumulating data while the primary scope is already
  accepted.

Nightly diagnostics report `recall_quality_per_agent`, which contains
`discovery`, per-agent `sample_count`, `ok`, `blocked_reason` and metrics, plus
`combined`.

## Scope defaults that still come from configuration

The command-line default scope (`learn live-acceptance`, `learn l5-readiness`,
`nightly`) is read from `EIMEMORY_AGENT_ID`, `EIMEMORY_WORKSPACE_ID` and
`EIMEMORY_USER_ID` (or the `--scope-*` options). When none are set, it falls
back to the legacy built-in product labels in `eimemory/identity.py`, which can
be changed with `EIMEMORY_HONGTU_AGENT_ID` and `EIMEMORY_HONGTU_WORKSPACE_ID`.

On an installation where a gateway has no `EIMEMORY_AGENT_ID`, that gateway
writes under the fallback label. Changing the fallback would move where that
gateway's data lands, so it is a deliberate configuration decision. Code does
not change it silently.

## Portability

Nothing site-specific is hard-coded in shipped code. Resolution order:

| Item | Source |
|---|---|
| Deploy scope (agent/workspace/user) | `EIMEMORY_DEPLOY_SCOPE_*` env > `settings.json` `deploy_scope.{agent_id,workspace_id,user_id}` > legacy labels in `deploy/deploy_scope_default.sh` / `eimemory.identity` (env `EIMEMORY_HONGTU_AGENT_ID`, `EIMEMORY_HONGTU_WORKSPACE_ID`) |
| Gated agents | runtime discovery, or `EIMEMORY_RECALL_GATE_AGENTS` |
| Agent gateway units | pattern discovery (`*-gateway.service`, `hermes-gateway-*.service`), not a name list |
| User / home | `$(id -un)`, `Path.home()`, `EIMEMORY_USER_ID`, `EIMEMORY_DEFAULT_OPERATOR_USER_ID` |
| RPC / health URL | `EIMEMORY_HEALTH_URL`, `EIMEMORY_RPC_URL`; loopback `127.0.0.1:8091` is only the local default |

`tests/test_no_site_identifiers.py` fails CI on any new host name, tailnet IP,
`/home/<name>`, `/dev-project` path or agent/profile name in non-test code.
Remaining baseline entries are frozen identifiers (catalog/test-plan IDs that
stored evidence references, the env-overridable legacy labels, a security
denylist path and evaluation fixtures); the baseline may only shrink.
Historical plans and incident reports under `docs/` are records and are not
rewritten.
