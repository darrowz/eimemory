# Agent Runtime Adapter Operations

This runbook configures independent host channels for the local-first memory
and evidence-gated learning runtime. The host names and profile values below
are maintained deployment examples; use your own configured identities.
Operational verification is separate from business recall acceptance. See
[receipt-based status](acceptance-status.md) for the latest reported run.

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

## Read persisted nightly evidence

After a managed run, inspect the stored nightly, deployment-association, recall
quality and memory-evaluation receipts. Record release identity, execution
verdict, formal gate verdict, accepted-case counts and each waiting/blocking
reason. Exit 0 or `execution_ok=true` alone cannot certify recall.

Known-item smoke is diagnostic. `recall_quality_evidence_incomplete`,
`memory_eval_dataset_empty`, `hypothesis_missing_or_ambiguous` and an unavailable
model reviewer leave their corresponding evidence gaps open. Mainline workflow
repairs began in 1.14.58; mainline 1.14.59 also persists safe research-review
diagnostics in supervisor receipts. The earlier 1.14.58 / `534bb2f0` run failed at
`research_closure_review`, with `research_review_llm_unconfigured` in detailed
records. The latest 1.14.64 / `f61487e2` attempt switched the release link but
failed deployment health acceptance: `ok=false`, `health_exit_code=1`,
`TimeoutError`. Its closure was blocked at `deployment_receipt` with
`health_fetch_failed`, exit 1. The handoff denied nightly. Current service
health and the specific timeout cause remain unverified.
The last completed 1.14.63 / `4fbcbbc9` deployment receipt passed,
and the October 11 nightly exited 0 with `execution_ok=true` and no failed steps.
Smoke passed 10/10, diagnostic error rate was 0.00, and hit@1 / hit@5 were 1.00.
The formal gate remains false with `recall_quality_evidence_incomplete`:
accepted cases remain 0/15; all 321 reviews produced zero passes and zero
pending conclusions. The memory benchmark has no retrieval cases; all nine
evolution candidate counts are zero, with no applicable knowledge link for the
gap revision. Formal recall and evolution remain unaccepted. See [evaluation](evaluation.md) and
[remaining acceptance work](acceptance-status.md#work-required-to-close-acceptance).

Before diagnosing a colleague's empty recall, verify their actual Feishu
session identity, host adapter, effective client configuration, RPC destination
and authorized scope/source policy. Prior controlled checks with incomplete
identity verification do not establish a current fault. Service smoke does not
substitute for this path. Use the [team recall checklist](team-recall-acceptance.md)
for participant-level natural questions, no-answer near neighbors, permission
boundaries and source readback.

Mainline 1.14.64 introduces policy v5 with a three-case / three-label floor in
the dataset's existing exact scope. This explicitly requested count change
retains label authority, release authorization and quality/isolation checks.
The deployed 1.14.63 receipt above keeps its historical 0/15 result. Use
[existing authorized memories](team-recall-acceptance.md#using-existing-memory-for-four-checks)
for controlled targets and contrasts without relabelling them as natural gold.

Mainline 1.14.65 reports bounded health snapshots: `store.migration_status`
distinguishes `available`, `timeout`, `unavailable` and liveness-only
`not_checked`; `retrieval.health_status` describes the optional candidate
snapshot. Storage probe timeout/error refuses readiness. An optional retrieval
timeout reports the source unavailable, keeping local storage readiness separate. Repeated
requests reuse one in-flight read per fragment; an expired healthy value is
not substituted for a blocked refresh. `/livez` skips both readers and cannot
replace authenticated deployment health acceptance. A timed-out worker is not
cancelled; it may finish and supply a later fresh snapshot. Production recovery
still needs a new release-bound deployment receipt.

From 1.14.60, research review automatically discovers the service user's Hermes
installation when no explicit feature/shared command is selected. It follows
current Hermes model/provider configuration through an isolated SDK completion.
Ordinary installations need no extra review route. Put any nonstandard launcher,
source-root or profile-home override in protected `governance.env`, then use
the configuration-only preflight. See [automatic review](evaluation.md#automatic-hermes-review).

From 1.14.61, the semantic judge uses the same supported installation discovery,
retaining tool-free/read-only execution. Each scanned automatic label review
persists pass or fail with reasons; unavailable transport or insufficient signals
concludes fail without creating a relevance label. Later evidence can trigger
another review. Inspect persisted conclusion IDs and nightly diagnostic counts;
old receipts remain historical. Version 1.14.62's reason-code repair is confirmed
in that release's durable summary: invalid capture boundary 10, missing matching
decision 186, semantic items not delivered 6. These review codes do not identify
the smoke retrieval failure. Mainline 1.14.63 fixes digest deduplication that
merged different dates or content sharing a summary prefix. Native local checks
retain the target without changing query, expectation or gate. It also preserves expected/returned record
IDs and explicit error-rate counts in newly persisted quality reports, while
excluding query and memory bodies. The later 1.14.63 readback confirms aggregate
smoke recovery; it does not provide a historical stage trace or certify formal
business recall.

## Session deduplication provenance

Since 1.14.66, new proactive decisions retain
`retrieval_diagnostics.session_dedupe` (`proactive_session_dedupe.v1`). Use the
evaluation case's `capture_ref` to read its exact authorized decision, then
inspect this object alongside `post_selection.session_deduped_count`:

| Field | Meaning |
| --- | --- |
| `observed_at` | UTC timestamp at the ledger read; not an injection timestamp |
| `input_count`, `removed_count` | Unique voluntary candidates entering and removed by session deduplication |
| `recorded_count`, `truncated`, `missing_match_count` | Recorded witnesses, whether the 64-witness cap omitted candidates, and any missing witness within that window |
| `matches[].record_id`, `source_id` | Exact removed candidate identity |
| `prior_decision_id`, `prior_citation` | One qualifying historical decision/item per removed candidate |
| `prior_state`, `prior_ever_injected`, `prior_terminal` | Historical item/decision state observed in the same SQLite snapshot as the dedupe set |
| `prior_decision_created_at`, `prior_item_updated_at` | Historical creation and last-update timestamps; last update may be a terminal transition |
| `basis` | `ever_injected`, `host_used`, `host_rejected`, `injected` or `in_flight` |

The witness is the newest qualifying historical item by decision creation time,
decision ID, item order and citation. It uses the same channel, exact scope,
source-set key, session and record/source identity as the original predicate.
It does not list every historical occurrence. The diagnostic cap does not
limit deduplication; an omitted witness cannot be called individually verified.
The snapshot records eligibility at the read even if another process closes an
in-flight decision immediately afterwards. Historical ledger retention may
later remove a referenced decision; its observed fields remain in this audit.

`ever_injected=true` with `prior_state=not_used` means the model was given the
item, without recorded actual use. `in_flight` describes an open reservation,
not confirmed delivery. A closed item that was never injected and has no
host `used/rejected` feedback stays eligible. Mandatory policy context bypasses
session deduplication. Query, title and memory body are excluded from the audit.

For an empty current decision, consult this audit to distinguish session
suppression from no matching evidence. Historical witnesses are never inserted
into current `candidate_refs`; an automatic `no_candidate_refs` review closes
as not evaluable and does not certify correct no-answer. Old count-only decisions
cannot gain exact candidate/history links retroactively. Keep the old four
audits separate from any newly observed cases; see the
[operator readback](acceptance-status.md#session-deduplication-readback).

## RPC security

Production loads `/etc/eimemory/rpc.env`, which contains the strong server
secret `EIMEMORY_RPC_AUTH_TOKEN` plus, when enabled, only non-secret paths and
profile markers for attestation. Clients receive the runtime secret through
their own secret manager as `EIMEMORY_RPC_TOKEN`; never print, commit, or copy
the token into plugin files.

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
EIMEMORY_ADAPTER_TIMEOUT_SECONDS=<bounded host-specific timeout>
```

The maintained production settings use a 5-second Hermes adapter budget and a
3.5-second managed OpenClaw hook budget. Do not restore the former sub-second
default: production recall commonly needs several seconds, and too-small host
deadlines create false bypasses rather than useful safety.

### Optional fail-closed task attestation

Codex/Hermes task receipts require an operator-separated producer boundary;
same-UID file permissions alone do not make a producer model-unforgeable. The
RPC service reads producer credentials only from a private JSON file shaped as
`{"codex":"<token>","hermes":"<token>"}`. Its environment contains only the
file path and explicit profile marker:

```text
EIMEMORY_ATTESTATION_HOST_PROFILE=operator-separated-v1
EIMEMORY_ATTESTATION_TOKENS_FILE=/etc/eimemory/attestation-producers.json
```

The producer tokens must be strong, distinct from each other and from
`EIMEMORY_RPC_AUTH_TOKEN`, and the file must be a regular non-symlink private
file (mode `0600` on POSIX). Codex and Hermes host integrations receive only
their channel-specific private token-file path. Do not put a producer token or
token-file path in the ordinary model/tool environment. For Codex, enforce the
corresponding `shell_environment_policy` stripping `KEY`, `SECRET`, and
`TOKEN` variables from model-launched commands.

The local receipt handoff file contains receipt IDs only and is not a trust
boundary. The runtime database joins and atomically claims the exact pending
receipt set. Check `eimemory_status.attestation_available` and
`attestation_reason`; when the profile is absent or malformed, receipt issuance
is unavailable and those tasks cannot count toward L5, while memory/recall
remain fail-open.

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
mkdir -p "$HERMES_HOME/plugins/eimemory_hook"
cp -R integrations/hermes/eimemory_hook/. "$HERMES_HOME/plugins/eimemory_hook/"
```

Select the provider in `$HERMES_HOME/config.yaml`:

```yaml
memory:
  provider: eimemory
plugins:
  enabled:
    - eimemory-hook
```

Or enable the host callbacks explicitly:

```bash
hermes plugins enable eimemory-hook
```

Start a new session, then call `eimemory_status` and verify `channel=hermes`,
`authority_mode=per_channel`, `embodied::channel::hermes`, and current release
identity. A production immutable deployment also provisions the separated
Hermes producer token, restarts `hermes-gateway.service`, and runs
`deploy/verify_hermes_integration.py`. That replay must prove the official
loader/provider binding, all three callbacks, authoritative remember/recall,
an actual pytest tool result, terminal receipt consumption, and completed-turn
sync. Disable the integration with:

```bash
hermes memory off
hermes plugins disable eimemory-hook
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
only when the terminal transaction consumes the exact protected set of
eligible, channel-specific v2 receipts joined from the runtime database. The
verification text is diagnostic only; the service may replace it with a
receipt label, but prose is never the trust gate. Unverified successes and all
`session_end` events remain diagnostic/lifecycle evidence and cannot raise L5.

## Live closure checklist

1. `/health` version, 40-character commit, import root, and release path agree.
2. The installed `eimemory.capability_catalog.bootstrap.v1` entry point resolves
   from the intended trusted distribution and the catalog seals successfully.
3. The selected profile resolves each active provider binding to its own catalog
   case; no ambiguous or cross-provider case is accepted.
4. `eimemory_status` succeeds independently in Codex and Hermes.
5. A unique memory written in one channel is recalled there and absent from the
   other two channels.
6. A verified task appears only in that channel's current-deployment metrics.
7. An unverified success and a session end do not change verified-task counts.
8. Capability acceptance produces distinct runs and observations for Hermes and
   OpenClaw; each binding reaches its profile threshold independently.
9. `learn l5-readiness --reader-mode v3 --profile l5.default` reports all four
   axes separately; do not infer readiness from `/health`.
10. Stop the RPC briefly and confirm both hosts continue with fail-open bypass;
   restore it and confirm status recovery.
11. Re-run readiness and confirm the existing dynamic closure remains green.
