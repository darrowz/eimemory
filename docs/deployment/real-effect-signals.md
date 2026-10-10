# Real-effect signals and bounded data learning (1.14.53–1.14.57)

## Routing and diagnostic repair (1.14.57)

Hermes prefetch, explicit recall, daily reports, attribution and policy issuance
now share one source resolver. Native writes use `hermes`. A configured
`EIMEMORY_SOURCE_IDS=project-a,default` selects exactly `project-a,default,hermes`;
report/cycle commands must run with the same gateway configuration, or receive
the complete allowlist through repeated `--source-id` options. Existing signed
`default` grants are never rewritten or broadened. Review and issue a new grant
for the actual native namespace when activation is authorized.

Run the colleague check from the immutable release with the corresponding
gateway's actual Python and fully loaded environment. Specify the real Feishu
user identity and both the general preference and business queries:

```bash
/opt/eimemory/current/.venv/bin/python -B /opt/eimemory/current/deploy/check_hermes_recall_identity.py \
  --tenant-id default --agent-id AGENT --workspace-id WORKSPACE --user-id FEISHU_USER \
  --source-id hermes --query 'language and reporting preferences' --query 'BUSINESS_QUERY'
```

For configured additional sources, repeat `--source-id` for every actual source,
including `hermes`. Expected owner/source mismatches stop before recall; a wrong
RPC scope/channel is rejected. Reports contain counts, the effective owner,
source list, imported version and actual provider module path, without queries
or recalled content. Counts include items, persona, rules and reflections;
preferences returned in the persona partition are not mistaken for an empty
recall. Empty results give a nonzero exit status, even when the RPC
and routing checks succeed. This is still an independent process: it does not
verify the four running gateway processes or a live Feishu conversation.
`eimemory_status.adapter_local.identity` exposes equivalent routing/import-path
diagnostics when invoked from the actual host session. Deployment environment
identity retains precedence over cosmetic host profile labels.

The nightly capability hypothesis producer now revalidates missing-link gaps
against the live registry/profile and trusted executable catalog. If the exact
revision and binding have registered cases, it runs independent acceptance and
archives a `capability_gap_diagnostic` reflection with durable specs, runs,
traces and the subsequent precise-time capability projection. `passed` refers
to the cases; `gap_closed` refers to the resulting profile projection. Missing,
ambiguous, untrusted or stale targets remain visibly blocked. Cases that fail
retain the gap. A diagnostic is not a reviewed knowledge link, is never a
behavior-authorizing capability hypothesis, and cannot invoke a code proposer
or grant code-change authority. Real-effect recall hypotheses continue through
their separately signed data-trial controller; they are not relabeled as code
capability hypotheses. Neither kind certifies improvement or L5.

## Daily reports (1.14.54)

`eimemory learn effect-report --agent-id AGENT --workspace-id WORKSPACE --user-id USER --date YYYY-MM-DD --persist`
reads one exact channel/source namespace. In 1.14.57, omitted sources use the
provider's actual defaults: `--channel hermes` selects `hermes`; configured
`EIMEMORY_SOURCE_IDS` are normalized with the native `hermes` source appended.
Explicit `--source-id` values remain exact and do not broaden a signed grant. Nightly runs this report for the previous Asia/Shanghai day; disable
only reporting with `EIMEMORY_REAL_EFFECT_REPORT_ENABLED=0`. Explicit `--timezone`
is available for another business calendar. Decisions, not event rows, are the
rate denominator. Each rate reports its own known denominator, unknown count and
coverage. Missing labels and conflicting votes remain unknown. Tool execution
success remains separate from task success. A/B differences are observational
and stratified by release/policy; reports never certify causal improvement or L5.
Decision-time anchors keep late feedback on its original day. Old unanchored
signals are counted separately and cannot become guessed historical samples.
Report snapshots are archived and idempotent for the same evidence, outside recall.

## Attribution candidates (1.14.55)

`eimemory learn effect-hypotheses --agent-id AGENT --workspace-id WORKSPACE --user-id USER --persist`
and nightly analyze the last seven days. Each candidate binds exact source,
scope, injected envelope version, release, unique decisions and signal ids.
Ten observed decisions across at least three sessions and a negative rate of
at least 40% make a candidate eligible for a bounded experiment, not a proven
cause. Explicit task failures/ratings and suspected correction/reask metrics
remain separate. Offered-only memories, absent historical version snapshots,
changed/deleted records, mandatory context and rules are excluded from automatic
changes. Unattributed failures and protected/stale targets remain visible.
Hypotheses are archived reflection artifacts; no learning grant or L5 evidence
can be manufactured by a candidate's content.

Phases 1.14.53–1.14.55 collect evidence; 1.14.56 consumes an explicitly scoped
data grant in real recall. These observations do not certify L5. Deploy the
RPC server, memory provider and Hermes hook together. The hook implementation
digest changes: obtain fresh provider binding/catalog activation and release
evidence rather than reusing the 1.14.52 qualification.

## Automatic data trials (1.14.56)

The administrator CLI issues a private (0600), HMAC-signed grant using the
existing evidence-receipt signing key/keyring. Configure that signing material
privately under the production runtime configuration; never put it in a prompt,
command argument, repository or report. Grants cannot be issued over the adapter
RPC. They bind an exact tenant/agent/workspace/user, channel, source list and
storage-root digest, expire within 30 days, and permit only two built-in actions:
lower an optional memory's injection weight by at most 0.15 (floor 0.5), or raise
the injection threshold by at most 0.05 (ceiling 0.9). Rule promotion and code
evolution are outside this initial controller's authority. Mandatory context,
safety memories and rules always retain the existing delivery protection.

Use the actual Hermes profile owner and source partition, not demonstration
values. With the production root/config selected:

```bash
owner=(--channel hermes --tenant-id default --agent-id AGENT --workspace-id WORKSPACE --user-id USER --source-id hermes)
eimemory learn effect-policy-issue "${owner[@]}" --days 30 --daily-limit 3 --canary-percent 25 --min-trial-samples 20
eimemory learn effect-cycle "${owner[@]}"          # Preview; no adaptive state write.
eimemory learn effect-cycle "${owner[@]}" --apply
eimemory effect-tick --dry-run                     # Preview all private owner grants.
systemctl --user status eimemory-real-effect.timer
eimemory learn effect-stop                        # Immediate recall fallback; controller signs rollback.
```

The immutable installer installs/enables `eimemory-real-effect.timer` with a
fifteen-minute cadence, shared runtime identity and storage-writer release guard.
Without an exact scoped grant it waits without changing recall. Nightly also
runs the configured Hermes owner's cycle alongside reports and hypotheses.
The runner scans at most 16 signed owner grants. Expired/invalid signatures,
unsafe file permissions or broken state/receipt chains block adaptive actions
and are visible in command output; no implicit global grant is minted.

One trial runs per grant namespace. Start and adopt consume its daily cap (at
most three, counted by the Asia/Shanghai day); rollback always bypasses that cap.
Canary assignment is stable within a session and uses a separate hash domain
from proactive suppression cohorts. Candidate and baseline share the same
release/policy revision. Policy revisions invalidate recall caches. Test and
acceptance-generated decisions never supply trial evidence or receive adaptation.
Full version references prevent weights from leaking into a changed memory.
A threshold hypothesis needs negative observations across at least three distinct
optional memory versions; a single-memory hypothesis is tried first when eligible.

Adoption needs at least 20 known independent sessions in **each** arm by default,
at least 60% known-label coverage, at most 15 percentage points of coverage skew,
non-overlapping 95% Wilson intervals showing the selected negative metric fell,
adequate latency coverage without an excessive p95 increase, and enough candidate
sessions whose delivered optional-memory set or order actually changed. A score
change that leaves delivery unchanged cannot authorize adoption. Repeated turns
in one session count once, using any negative label in that session. Original
proactive control cohorts are excluded from the data-policy comparison.
Switching release identity or base recall policy during a trial/observation
window triggers rollback instead of mixing versions into the evidence. Report
`experiment_strata` separately shows the actual baseline and candidate arms.
Task/rating/correction/reask guards can trigger early rollback after five known
sessions per arm; strong latency regression also rolls back. Conflicting/unknown
labels never become positive evidence. Suspected corrections are still heuristic
outcomes; a reduction does not prove better task success or general intelligence.

Trials expire within seven days and revert if improvement was not established.
After adoption a seven-day observation window compares current effects against
the accepted candidate's evidence and reverses significant metric/latency drift.
This follow-up is a drift guard, not a second randomized causal claim. During
that window no new trial starts in the namespace. Adopted targets cool down for
30 days; reverted targets for seven. The controller alters derived policy data,
not original memory envelopes, model weights, adapter code or governance gates.
State, receipt chain, archived audit record and export outbox commit together;
crashes/retries/concurrent runs cannot half-apply a candidate.

The stop file is `<EIMEMORY_ROOT>/state/real-effect.stop`; the environment switch
`EIMEMORY_REAL_EFFECT_STOP=1` also stops adaptation immediately. Missing/expired
grants, bad signatures and the stop switch make recall use its original policy.
The next controller tick writes a signed safety rollback for an active trial or
observation window. Administrator removal of the stop file (and environment
switch, if set) resumes grant evaluation; issue a fresh grant after expiry.
Do not call deployment complete until live Hermes signals, actual canary arm
decisions, a qualified adoption/reversion receipt and restart recovery have been
observed on production. This workspace cannot deploy or certify production L5.

## Signal contract

`adapter.proactive_signal` requires the separate Hermes host producer credential
already used for host attestation. An ordinary adapter bearer cannot write it;
another channel's producer cannot write Hermes observations. The original
decision must match channel, all four scope fields, source ids, session and
decision turn. Acceptance-generated decisions are rejected. Its persisted
release, policy, pair/cohort and actually delivered memory references are captured
by the server, not supplied by the caller.

| Phase | Labels | Interpretation |
| --- | --- | --- |
| `turn_completed` | `tool_chain`: succeeded/failed/unknown/not_run | Explicit tool exit code, boolean success/error fields, or execution exception. Tool success does not imply task success. Free-form tool prose is unknown. |
| `turn_completed` | `task_success`: succeeded/failed/unknown | Optional explicit host `task_success` boolean; default unknown. This remains an observation, not a signed verified outcome. |
| `turn_completed` | optional finite `latency_ms` | Pre-call to completed-call elapsed time; omitted if the pre-call was missing. |
| `next_user` | `correction`, `reask`: suspected/none/unknown | Correction prefix or same normalized question within 300 seconds, attributed to the previous completed decision in the same session. Heuristics are not verified dissatisfaction. |
| `explicit_rating` | `rating`: positive/negative | Explicit host feedback for a named completed turn; silence is not a positive vote. |

The supported Hermes hook contract has no native thumbs-up/down event. Hosts may
call `provider.on_user_feedback(session_id=..., turn_id=..., rating=..., event_id=...)`
or forward `feedback_rating`, `feedback_turn_id`, `feedback_event_id` through the
next `pre_llm_call`. `post_llm_call` accepts optional host `task_success`. Without
these host fields ratings are absent and task success is unknown; do not present
zero observed ratings as zero negative feedback. No new model-facing tool may
self-report verified success.

Telemetry stores fixed labels, latency and routing/evidence identities in
`proactive_effect_signals`. It rejects user text, tool payloads and arbitrary
summaries. Correction detection never traverses conversation history. Repeat
matching uses an ephemeral keyed fingerprint kept only in bounded process state;
no query text or query fingerprint is saved in the signal ledger. Ordinary memory
capture/turn-context features retain their existing separate contracts.

When Hermes supplies its profile home, the provider persists unsent label payloads with mode 0600 in
`<hermes_home>/logs/eimemory-effect-signals-<session-digest>.json`, with atomic
replacement and fsync. One worker sends signals outside the host callback path;
the default observation client has a one-second timeout. Up to four pending
entries retry on the next callback or shutdown; at most 64 entries are retained.
Same event/same payload is idempotent; same event/conflicting payload is rejected.
Queue overflow is counted, never silently reported as complete delivery.

Inspect `eimemory_status.adapter_local.effect_signals`: `sent`, `pending`,
`dropped`, `error`, `worker_running`, `durable_queue_configured`, `unbound_tool_callbacks`,
`unbound_completed_turns`, and `native_reaction_hook=false`. Missing host
credentials or `durable_queue_configured=false` must be fixed before claiming production coverage. Missing host turn
ids use process-local turn identities for observations only; they cannot produce
verified host receipts. After restart the durable unsent queue is recovered;
previous-turn heuristic matching and rating targets are process-local, so delayed
feedback across restarts is unavailable in this release.

## Quarantine old test rules

Run with the production runtime configuration that selects the intended storage
root, and explicitly provide the exact tenant/agent/workspace/user and source partition. Take the normal
storage snapshot first. Example commands (preview is the default):

```bash
owner=(--tenant-id default --agent-id AGENT --workspace-id WORKSPACE --user-id USER --source-id default)
eimemory storage quarantine-test-rules "${owner[@]}"
eimemory storage quarantine-test-rules "${owner[@]}" --manifest /path/reviewed-test-rules.json
eimemory storage quarantine-test-rules "${owner[@]}" --manifest /path/reviewed-test-rules.json --apply
eimemory storage quarantine-test-rules "${owner[@]}" --revert
eimemory storage quarantine-test-rules "${owner[@]}" --revert --apply
```

Explicit `acceptance_generated=true` or `test_generated=true` in rule metadata,
content or provenance proves a test rule. New marked rules are archived on write
and cannot be promoted. Historical marked active rules can be quarantined.
Deployment/test producers must mark their fixtures at creation, for example
`runtime.evolution.store_rule(..., acceptance_generated=True)`; marked promotion
candidates are blocked before creating live rules. Unmarked new fixture
ids cannot be identified safely from duplicate text alone.
Unmarked rules appear in `unclassified` with id/version digest. Similarity or
duplicate text never proves test provenance. Confirm old deployment-test rules
from their production creation/promotion evidence, then copy only those selected
id/digest pairs into the empty `manifest_template.rules` list. The tool rejects
wrong-scope, missing, duplicate or stale manifest entries and incomplete scans.
An empty reviewed manifest selects no rules; omitting the manifest selects only
rules with explicit test provenance.

Applying writes `deprecated` rule envelopes, retains the original snapshot and
commits archived audit receipts plus export outbox in one transaction. Real rules
remain active. An ordinary rewrite or promotion cannot reactivate the same
quarantined id. Restore refuses any envelope changed after quarantine and keeps
explicitly marked test rules inactive; unmarked reviewed rules restore their
original envelope and status. Use a fresh preview/version manifest to select a
subset for restore, or omit it to restore all quarantined rules in that exact
scope/source. This tool covers `kind=rule` records, not `intent_patterns`.

Production's reported 49 test rules are an investigation target, not a built-in
count or a deletion selector. This workspace has no production database; no claim
of cleaning those 49 rules or backfilling the 515 old decisions is made. Verify
new real turns produce label rows, negative tools/corrections remain negative or
suspected, and quarantined rules stay out of the active rule search before
claiming that any phase improved production behavior.
