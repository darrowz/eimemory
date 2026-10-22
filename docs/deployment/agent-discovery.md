# Agent discovery and per-agent recall gates

## Native multiplex gateways

Hermes 0.22 defaults to one messaging gateway serving multiple profiles. The
physical runtime inventory is the eiMemory RPC service and the native main
gateway. Logical profiles, their homes, and their exact memory identities are
declared separately in the user's protected `hermes-topology.v1.json`. The
operator registration binds that declaration to a typed topology intent and
preserves the original deployment capture in private history.

The integration follows the native request's runtime profile and secret
ContextVars. A process-level profile environment cannot override another
profile's identity. Background callbacks retain their request's context; a
session ID alone cannot select a provider belonging to another profile. Shared
RPC credentials are used only for the shared RPC endpoint.

Sampling coverage compares the complete tenant, agent, workspace, user, channel
and source tuple. A configured profile with no natural traffic remains visible
with zero counts. Explicit searches, controlled historical business evidence
and natural observations retain their respective evidence classes. A served
profile without a trusted user binding is reported as unbound, never counted
as a qualified business sample.

Operational stability v2 verifies the live gateway's process lifetime, native
served-profile roster, each profile's memory configuration and plugin metadata,
in addition to the existing storage, projection and runtime checks. Historical
v1 receipts retain their original runtime and sampling contracts.

An explicit operator topology migration can supersede the restoration of
retired per-profile gateways. Recovery retains the original activity and
enablement facts and executes a separately authenticated restoration plan. It
checks that retired units remain absent or inactive; it never recreates,
enables, starts, masks, or adds restrictions to them. Gateway integration
publishes eiMemory environment metadata only. This migration is an operator
action and grants no autonomous repair or L5 credit.

Upstream references: [Hermes 0.22 release](https://github.com/NousResearch/hermes-agent/releases/tag/v0.22.0)
and [multiplexing gateway design](https://hermes-agent.nousresearch.com/docs/developer-guide/multiplexing-gateway).

The recall-quality gate evaluates every agent separately. Agent names are not
hard-coded anywhere in the gate, nightly or release-closure logic.

## How agents are discovered

At run time, an agent is any `agent_id` that has natural proactive decisions
in `proactive_decisions` during the last 14 days. These are excluded:

- deploy replays (`hermes-release-*` sessions)
- acceptance-generated decisions
- scheduled sessions (`cron_*`)

For a registered native topology, the protected logical roster supplies the
expected scopes, including profiles with zero traffic. Every exact user and
workspace remains independent. For legacy installations without a registered
topology, the historical discovery and environment-based identity rules below
remain in effect; they cannot override a registered profile binding.

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
