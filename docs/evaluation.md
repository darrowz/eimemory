# eimemory Evaluation Framework

`eimemory eval run` runs repeatable recall cases from a JSON dataset. Known-item
smoke, memory benchmarks, capability executors and formal business acceptance
retain separate evidence and verdicts. See [current status](acceptance-status.md).

The operator reports accepted deployment on **1.14.72 / `612dc458`**.
Deployment receipt `rec_fba7350aa0b4` records successful installation, binding
refresh and health checks, with active RPC and matching current/target releases.
Nightly `ref_d6eb0f6e9c6f` ran 02:15:05–02:23:40 on October 13, 2026
(Asia/Shanghai) and exited 0 with no failed steps.

Controlled memory checks passed **7 of 8** cases. The failed known-item case
returned no final records. Its completed native trace shows an available SQLite
source without fallback: the expected target was absent from the complete,
untruncated source-returned window of 14 candidates and subsequent candidate
stages. This excludes rejection of that target by the selector in this run.
It does not establish an absent index entry; query generation, source matching
and source-internal filtering remain distinct possible causes.
Collection created five cases and found 164 existing cases, with no collection
skips; all five new cases had empty candidates. Reviews total 365, with zero
pending/open reviews and **0/3 formally accepted cases**. Formal recall remains
unaccepted. Nine evolution results each have zero candidate hypotheses; an
applicable knowledge link for the current gap revision is still missing.

Version 1.14.71 repairs durable-memory input filtering, role parsing,
authoritative source binding and ordering of already related states, with
reference-only extraction diagnostics. A participant's small 1.14.69 Hermes
readback reported retained sources but two empty memory queries. Their exact
scope/source/stage binding and recovery remain unverified. Successful deployment
and nightly execution do not certify these queries, model factual truth or team
recall quality.

Version 1.14.72 adds a private, single-target native trace, now observed in the
supplied controlled readback. It records actual stage presence and target-specific
drops; missing observations remain unknown. The original query, case ID, expected
record, selector, grounding and thresholds stay unchanged, with no extra search,
record read or candidate injection. Query-generation metadata contains only
field, length and truncation. The new trace does not backfill 1.14.71 membership,
determine a historical production query type or certify recovery or formal quality.

Mainline 1.14.74 repairs the October 13 business-closure audit: trusted host
identity, recoverable Hermes capture and delivery evidence, formal acceptance
summaries, negative capability evidence and protected patch feedback, complete
archived-payload backups, and idempotent knowledge/persona processing. Review
coverage is resumable, Chinese query features retain privacy checks, and ordinary
memory evaluation follows the configured runtime. Long-query term selection now
covers the full question within existing bounds; ambiguous identity windows do
not certify uniqueness. Grounding, label trust, release authority and the v5
three-case floor remain required. These source repairs do not certify a production
deployment, recovery of the older controlled miss, or team-wide recall quality.

From 1.14.74, a formal nightly acceptance summary requires the supported formal
report, its persisted identity, current label/release verification and the existing
v5 policy. Legacy and unknown report contracts remain diagnostic. Automatic review
uses a resumable bounded batch and reports uncovered cases; a completed batch is
not proof that the entire backlog is closed. Ordinary memory evaluation uses the
configured Runtime memory/engine/source, while controlled and production samples
retain their distinct purposes.

The independent-evidence catalog's `enforce`/`shadow` modes govern explicit
independent-verification calls. Ordinary recall defaults to local admission and
post-delivery semantic review; it does not obtain a catalog proof merely because
a mode is configured. The separate REQUIRED verifier remains an additional
delivery gate, and current canonical permission/status checks still apply before
delivery. See [the repair record](audit/business-closure-remediation-2026-10-13.md).

## Evidence and verdicts

| Result | What it establishes | What it cannot replace |
| --- | --- | --- |
| Known-item smoke | Fixed expected records can be retrieved | Natural-query and colleague-wide business acceptance |
| Controlled existing-memory baseline | Actual runtime recall of authorized, current records using generated known-item queries | Natural production labels, correct no-answer, an independently labelled memory benchmark or formal business acceptance |
| Team recall checks | Recorded outcomes for the participants, real session identities and case types actually covered | Untested colleagues or the formal production gate's trusted labels and channel coverage |
| Memory CI / retrieval benchmark | Retrieval behavior on a non-empty labelled dataset | Release authorization and qualifying production labels |
| Trusted capability/catalog run | One immutable executor case for an exact revision/binding | A memory benchmark or another binding's evidence |
| Formal production recall gate | Qualifying trusted cases and release-bound evidence satisfy the complete contract | Missing prerequisites cannot be inferred from a process exit code |

### Capture, extraction and recall evidence

In memory-ingest diagnostics, L0 denotes retained conversation sources and L1
denotes extracted durable memories. A healthy write queue and an L0 source do
not establish that a fact was admitted, extracted or stored in L1, or that the
participant's actual recall path delivered it. These data stages are separate
from governance authority tiers.

Version 1.14.71 repairs mixed-statement input filtering, USER/ASSISTANT parsing,
authoritative source reloads and atomic source-time ordering. Its bounded
extraction diagnostics distinguish actual input rejection, model execution,
valid empty output, candidate filtering and store/skip/update outcomes. Preserve
non-passing conclusions; a valid schema and source reference cannot independently
certify factual truth or role-specific semantic grounding.

The small 1.14.69 participant readback lacks exact scope/source/stage binding.
It cannot establish which of these paths caused its two empty queries, nor
certify teammate-wide accuracy. New diagnostics cannot reconstruct historical
`NoMemory` decisions or turn controlled probes into natural production gold. See
[the diagnostic contract](operations.md#conversation-capture-and-memory-extraction).

An empty memory dataset is not a pass. The reported 1.14.57 nightly skipped it
with `memory_eval_dataset_empty`. Mainline 1.14.58 repairs persist a not-run
receipt, dispatch catalog cases through capability executors, and securely select
`EIMEMORY_MEMORY_EVAL_DATASET`, then
`<EIMEMORY_ROOT>/evaluation/memory_eval.json`, then generated replay. Dataset
generation or secure loading failures stay failures. See
[acceptance status](acceptance-status.md#mainline-workflow-repairs).

Team-level evaluation must verify each participant's actual conversation
identity and environment before interpreting empty or successful results.
Cover known-memory natural questions, no-answer near neighbors, cross-member
permission boundaries and authorized source readback. Feishu is the message
entry point; derive the host channel from the actual adapter. Neither the
historical 10/10 smoke nor the current formal 0/3 count establishes participant-level acceptance. See
[team recall acceptance](team-recall-acceptance.md).

### Existing path security, new automatic entry

Version 1.14.58 added automatic discovery of the conventional memory dataset;
it did not introduce the loader's ancestor ownership or writable-directory
rules. Those checks already existed in 1.14.57 and originated in
[`3f3365e3`](https://github.com/darrowz/eimemory/commit/3f3365e3b7c1a147d2a1d9036cfe55a735bb1741).
On POSIX, ancestor owners must be root or the process's effective user, and
group/world-writable ancestors require sticky protection. Dataset files must
also satisfy the existing ownership, mode, no-symlink and open-time checks.

The new regression tests first exercised this automatic entry and exposed
host ancestor permissions that did not meet the existing contract: **old rules,
new entry, newly encountered host restriction**. Positive-path tests use the
existing `trusted_dataset_path_ancestors` fixture to model trusted host
ancestors. Dataset directories and files remain subject to the real checks;
separate rejection cases cover foreign owners and unsafe write modes. This
test isolation changes neither production validation nor gate thresholds.

For an actual deployment, select a dataset path whose entire parent chain
satisfies the contract. Changing only the file or `evaluation/` directory mode
cannot repair an untrusted ancestor. Secure loading failures remain execution
failures and do not fall back to generated replay or count as a benchmark pass.

For a separate memory CI dataset, use the existing CLI:

```bash
eimemory eval ci memory-dataset.json --output memory-ci-report.json
```

Use independently labelled retrieval cases and record the scope, dataset identity
and release. Sample fixture cases are useful smoke checks, not production gold.

### Controlled baseline from existing memory (1.14.69)

When no configured dataset is selected and replay generation explicitly succeeds
with zero retrieval cases, nightly can derive a controlled baseline from existing
authorized records. A configured dataset error, replay failure or missing replay
success evidence cannot activate this fallback. Independent configured datasets
retain precedence, secure loading and their original verdicts; catalog execution
also retains its separate result.

The baseline uses the caller's exact channel-qualified scope and source allowlist.
An unqualified caller needs one unambiguous matching private release binding;
binding-backed host-to-logical-user translation preserves that channel. Installed
clients or records found in the store cannot establish permission. Missing or
ambiguous bindings block the controlled path with an explicit reason.

Active, current, non-operational memory and knowledge records provide positive
known-item targets. The generated query uses an existing title or text prefix;
it is marked `controlled_source_prefix`, not a natural production question.
Nightly binds the source inventory to its existing start cutoff, excluding
records created later in the same run. An invalid cutoff blocks generation;
invalid source creation timestamps exclude those records. The cutoff does not
prove every older record was indexed. Generation reports distinguish scan
truncation, selection truncation and cutoff skips.
The baseline creates no memory records, incident evidence, no-answer expectations
or trusted relevance labels. `EIMEMORY_CONTROLLED_MEMORY_BENCHMARK_CASES` sets
the case budget, default 10 and bounded to 1–50.

From 1.14.73, positive-target generation also reuses the existing recall source
policy. Records with `quality.capture_decision=reject`, shared raw episodes and
default evidence-only material cannot become positive
known-item expectations. Normal eligible L1 episodic memories remain supported;
the exclusion is not a blanket ban on episodic memory. Generation retains explicit
policy skip reasons: `source_record_capture_rejected`,
`source_record_episode_evidence` and `source_record_evidence_only`.
Each existing controlled case is revalidated before recall; an ineligible source
is not evaluable and cannot pass. Generation with no eligible material remains
not-run with zero samples and an unknown pass rate; revalidating an existing
dataset cannot turn all ineligible cases into a pass. Eligibility
does not depend on a prior retrieval failure, selector decision or grounding score.
These rules do not reclassify the earlier 1.14.69 or 1.14.72 failed samples as
passes or establish recovery of their original retrieval misses.

Each case binds its exact record/source/scope and payload digest. The evaluator
calls the configured runtime's real `memory.recall`, reads the source before and
after retrieval, and rejects missing, changed, expired or unauthorized sources.
A source that changes while recall runs cannot certify a successful expectation.
The persisted `memory_eval_ci` report identifies `dataset_source=controlled_records`
and `dataset_kind=controlled_memory_benchmark`; it retains references, ranks,
counts, failure reasons and latency without query or memory bodies.

`passed_threshold` and `controlled_benchmark_passed_threshold` can be true when
all controlled cases succeed. The same report always has
`memory_benchmark_accepted=false`, `formal_business_evidence=false`,
`certifies_natural_queries=false` and `certifies_no_answer=false`. Empty eligible
material remains `not_run` with `controlled_memory_benchmark_empty`, zero samples
and an unknown pass rate. These checks make an empty replay diagnosable without
turning generated questions into the three formal cases or accepted labels.

The 1.14.69 readback reports 10 controlled samples, nine passing and one
`known_item_missing` with zero returned records. An empty known-item miss is
neither a correct-no-answer certificate nor sufficient evidence of an algorithm
defect. Version 1.14.70 retains bounded retrieval-stage metadata for subsequent
controlled cases so eligibility, filtering and source availability can be
examined without copying query or memory bodies. Absent historical stage
fields cannot be reconstructed from a later successful lookup. See
[the historical release-bound readback](acceptance-status.md#earlier-reported-deployment-and-nightly-11469).

The earlier 1.14.71 readback ran nine controlled samples: eight passed and the
same known-item case ID returned no final results. Native measurements report
an available source, no fallback, 14 selector inputs, zero selected inputs and
14 `grounding` drops, with `empty_result_cause=selector_rejected_all`.
These fields establish rejection of this run's selector inputs; they do not
establish whether the expected target was in that set. Stage counts are not
per-candidate traces, do not reconstruct the 1.14.69 miss and do not justify
relaxing grounding or quality requirements. The result remains outside formal
business memory acceptance. See
[the historical receipt](acceptance-status.md#earlier-reported-deployment-and-nightly-11471).

The latest 1.14.72 readback ran eight controlled samples: seven passed and the
same case failed. Its completed, untruncated native target trace finds expected
record `mem_c2c5b75407f1f262177f4586400db98f` absent from the available SQLite
source's 14-candidate returned window, with no fallback. The target is also absent
at bounded, hydrated, filtered, fusion and selector-input stages; selector and
final outputs are empty. This excludes the target entering the selector and
being rejected for grounding in this run. It does not prove an absent index entry:
query generation, source matching and source-internal filtering remain unresolved.
The old 1.14.71 membership is not backfilled. See
[the latest receipt](acceptance-status.md#latest-reported-deployment-and-nightly-11472).

### Controlled target stage trace (1.14.72)

The controlled evaluator privately observes one authorized expected target while
the original `memory.recall` runs. `retrieval_diagnostics.target_trace` uses
`controlled_target_stage_trace.v1`; no trace switch is added to the public query
contract. The observer binds to the native engine/store, exact scope, complete
source allowlist and private query digest for one thread/task invocation,
with nested and concurrent invocations isolated.
It inspects existing candidate windows without another search, record read,
candidate injection or change to selection. The query, case ID, expected record,
grounding, thresholds and formal acceptance rules remain unchanged.

Eight fixed stages are observed: `provider_returned`, `provider_bounded`,
`hydrated`, `filtered`, `fusion_input`, `selector_input`, `selector_output` and
`final_items`. Each stage reports `target_presence` as `present`,
`absent_in_observed_window` or `unknown`, with `observed_count`, `inspected_count`,
`window_complete`, `truncated` and allowlisted `drop_reasons`. Actual target
grounding measurements retain `grounding_score` and `grounding_threshold` when
observed; absent measurements stay unknown. Trace status distinguishes
`not_observed`, `binding_mismatch`, `completed` and `aborted`, and
`does_not_certify_answer=true` remains explicit. Absence in one bounded window
does not establish absence from the full corpus or factual answer quality.
The observer inspects at most 5,000 existing references per stage; a bounded
over-limit diagnostic window does not consume another search or source read.

`query_generation` records only `material_field` (`title`, `summary`,
`content_text` or `unknown`), `material_chars`, `query_chars` and `truncated`.
`query_chars` measures the generated canonical caller query, before recall's
existing cleaning. It describes generation without changing it; the query, memory
text, identity, raw scope and private binding values are not added to the trace.
Nightly's bounded failed-case references retain these new measurements when
present. Older receipts stay unchanged: the 1.14.71 target's membership remains
unknown, and a synthetic local case cannot determine that production query's
material type or prove recovery. The supplied 1.14.72 readback verifies the trace
on one failed controlled case, without certifying recovery or formal quality.

### SQLite source target trace (1.14.73)

Subsequent native SQLite controlled runs can retain
`retrieval_diagnostics.target_trace.source_trace` with schema
`sqlite_target_source_trace.v1`. The active engine/store/source/request and
borrowed reader must match the private invocation, including source allowlist,
scope, query and thread/task ownership. Nested invocations cannot borrow the
frame. Existing SQL projections provide native identity observations; their
`WHERE`, ordering and `LIMIT` remain unchanged. No additional target lookup,
SQL/read/model call or candidate is introduced.

Ten fixed stages retain actual windows: `identity_index_rows`,
`identity_verified`, `hybrid_collected`, `hybrid_capped`, `hybrid_joined`,
`scored`, `ranked_selected`, `score_ref_binding`, `merged_hits` and `returned_hits`.
Stage `target_presence` uses the existing three states, alongside
`observed_count`, `inspected_count`, `window_complete`, `truncated` and
allowlisted `drop_reasons`. `source_score`, `source_rank` and `binding_status`
(`bound`, `not_bound`, `unknown`) describe only actual native observations;
unobserved numerical values remain unknown. They do not grant read or label
authority. The bounded `collection_arms` are `fts`, `anchor`, `lane_seed` and
`recent`, each `completed`, `incomplete` or `unknown`; an unrun arm is not evidence
of an empty search. Status keeps the existing four states, and
`does_not_certify_answer=true` remains explicit.

An absent target means absence in the observed SQL `WHERE` + `LIMIT` window.
It does not establish absence from the index or rejection by one particular SQL
predicate. Actual per-target drop codes are recorded only where the native branch
observed that target. Missing stages, partial windows and legacy/custom paths
remain unknown. The projection retains no database path, storage key, query/hash,
memory text or record-ID list. The scheduler's bounded failed-case projection
preserves this optional trace when present. The 1.14.72 receipt has no source
trace and stays unchanged; no 1.14.73 deployment or recovered miss is certified.

## Dataset Format

```json
{
  "name": "memory-smoke",
  "scope": {"agent_id": "<agent-id>", "workspace_id": "<workspace-id>", "user_id": "<user-id>"},
  "task_type": "brain.respond",
  "profile": "balanced",
  "seed": [
    {
      "title": "Official channel",
      "text": "Feishu is the official communication channel.",
      "memory_type": "decision"
    }
  ],
  "cases": [
    {
      "id": "official-channel",
      "query": "official communication channel",
      "expect_any_title": ["Official channel"],
      "limit": 3
    }
  ]
}
```

Expected fields can be mixed:

- `expect_any_title`
- `expect_any_record_id`
- `expect_any_kind`
- `expect_any_text`

## Run

```bash
eimemory eval run dataset.json --output report.json
```

Use `--no-seed` to run against an existing production store without inserting
dataset seed records.

## Metrics

The report includes:

- `pass_rate`
- `mrr`
- `precision_at_k`
- per-case returned ids, titles, confidence, retrieval mode, and vector hits
- misses with expected versus returned records

For memory CI reports, phase-level MRR, recall, and precision are macro-averages
of the corresponding per-case metrics. Successful expected-empty cases retain
their per-case scores. Pass thresholds use the unrounded pass-count ratio;
three-decimal rounding is for report presentation only.

The LoCoMo and LongMemEval `ndcg_at_5` metric uses binary relevance over evidence
IDs. Its ideal ranking uses the requested cutoff, even when fewer results are
returned, and a repeated evidence ID earns gain only at its first occurrence.

This framework evaluates recall behavior first. Broader source-intake,
daily-brief, and skill replay suites should reuse this report shape.

## Research model review configuration (since 1.14.60)

Research closure uses `EIMEMORY_RESEARCH_REVIEW_LLM_COMMAND`, a JSON argv array,
or the shared `EIMEMORY_LLM_COMMAND` when the feature-specific route is unset.
When neither command is set, it automatically discovers the service user's
Hermes installation. A normal working Hermes installation needs no additional
review command, provider or model setting.
The configured bridge receives `system_prompt`, `user_prompt` and `json_mode`
as JSON on stdin and returns `text`, `provider_id` and `model_id` as JSON on
stdout. No implicit `codex` executable is required by the repaired default path.

Review text must itself be a JSON object with exactly four string fields:
`verdict` (`approve`, `reject`, or `needs_followup`), `rationale`,
`required_followup`, and `risk`. Rationale and risk cannot be blank.
Duplicate JSON keys, invalid structure, command failure and missing configuration
leave the review unavailable. There is no fallback after a command fails.

The feature timeout is `EIMEMORY_RESEARCH_REVIEW_LLM_TIMEOUT_SECONDS`, falling
back to `EIMEMORY_LLM_TIMEOUT_SECONDS`. The existing
`EIMEMORY_ALLOWED_REVIEW_MODELS` policy checks the actual responding
model. The persisted receipt records the actual model/provider; unavailable
reviews are retried through the existing nightly retry queue.
Verify the selected route's receipts after deployment. Model review cannot replace accepted production labels,
release authority or an independent retrieval dataset.

### Automatic Hermes review

Discovery honors an explicit `EIMEMORY_HERMES_BIN`, then an explicit
`EIMEMORY_HERMES_AGENT_ROOT`, then the actual service PATH, published user
launchers and Hermes home/source installations. `EIMEMORY_HERMES_HOME` or
`HERMES_HOME` selects a profile when configured. Installation-bound launchers
supply their interpreter, source root, home and current dependency bootstrap;
legacy source virtual environments and installed Python console entrypoints
are also supported. Discovery does not search arbitrary checkouts or import
through the caller's working directory.

The isolated child uses Hermes' own configuration normalization, credential
loading, runtime provider resolver and SDK transport adapters. It reads the
current configuration on each invocation, including reasoning effort, custom endpoints and wire
protocols, without pinning a provider/model or copying credentials into the
parent environment. Pending artifacts use the `configured` selector; only a
validated response populates `review_model_used` and `review_provider_used`.
The responding model must match the resolved SDK model and the existing
model allowlist still applies. Agent/tool conversations and external agent
runtime modes cannot supply a tool-free review.

The current operator-selected model is **`gpt-6.1-sol` with `low` reasoning**;
`gpt-5.5` is retired. In the active Hermes profile, the corresponding configuration
is `model.default: gpt-6.1-sol` and `agent.reasoning_effort: low`. Review follows
that profile rather than embedding the model in release code. Hermes' own
per-model reasoning overrides and provider request builder determine the wire
parameters. Local compatibility tests verify this model/effort request; they do
not establish that a production profile has already been changed.

An incompatible installation reports `research_review_hermes_runtime_unavailable`;
an unusable model configuration reports `research_review_hermes_configuration_invalid`
in preflight. Missing installations remain `research_review_llm_unconfigured`.
Selected command or SDK failures never trigger another provider or turn into an
approval. The JSON review schema and business evidence thresholds are unchanged.

### Durable review failure diagnostics (1.14.59)

Managed deployments check the protected `governance.env` before stopping
storage writers or switching `current`. Version 1.14.60 also accepts a discovered
Hermes runtime with a usable model configuration. A missing or invalid route
blocks the switch. A command or Hermes override present only in the deployment
controller's environment cannot satisfy this check. Discovery uses the service
user's account home and protected governance overrides. The automatic check
imports the installed SDK and reads configuration; it invokes no model and
cannot establish provider readiness or acceptance.

Deployment regressions isolate the OS account lookup and default system PATH
inside the test subprocess; changing the controller's HOME alone cannot hide
an installation in the service account's real home. Separate contracts require
`research_review_llm_unconfigured` when that service has no Hermes installation,
and automatic discovery when Hermes exists in its account home or system PATH.
The latter re-reads the current profile, rejects invalid model configuration and
invokes no provider. Neither contract skips failures or changes discovery rules.

For an explicitly selected bridge, configure `EIMEMORY_RESEARCH_REVIEW_LLM_COMMAND`
(or the shared `EIMEMORY_LLM_COMMAND`) in the managed governance file using a
JSON argv array with the protocol described above. Hermes defaults require
neither command. Nonstandard installation/profile overrides belong in the
same protected file so preflight and nightly use the same selection.
The [governance example](../deploy/governance.env.example) documents both routes.
As the service user, configuration can be checked without rerunning nightly:

```bash
/opt/eimemory/current/.venv/bin/python -I -B \
  /opt/eimemory/current/deploy/run_with_governance_env.py \
  --env-file /etc/eimemory/governance.env --check-research-review
```

This check requires the 1.14.59 helper for explicit commands and 1.14.60 for
automatic Hermes discovery; the 1.14.58 live helper does not yet have
the option. Protect the file with the existing ownership/mode requirements.
No provider/model is invented and no failed review is converted into a pass.

The full review report exposes `error`, `error_counts` and
`unavailable_records`. Each unavailable record stores a fixed `review_error`
and structured `review_failure` in both content and metadata. Diagnostics
distinguish missing/invalid configuration, missing executables, permission
errors, timeouts, failed commands, invalid bridge responses, disallowed models
and invalid review text. A command bridge's validated failure category and
measured timings are retained when available. Generic exceptions remain
`research_review_execution_failed`; no provider/authentication cause is guessed.

The persisted supervisor receipt includes
`nightly_diagnostics.research_closure_review.reason_counts` and up to 20
sanitized `unavailable_records` with internal references. Reason counts cover
at most 500 rows; truncation is explicit. Raw prompts, stdout/stderr, argv and
arbitrary exception text do not enter this projection. Legacy missing reasons
remain `reason_not_reported` or `reason_not_allowlisted`.

Read these fields from storage along with the deployed version/full commit
before attributing a failed run. Retrying a review clears its current failure
metadata; it does not clear the prior supervisor receipt's diagnosis. The
existing supervisor contract still keeps the latest run rather than an
immutable history of every nightly. These diagnostics do not approve reviews,
create accepted recall cases or relax any quality gate.

## Dynamic Capability Catalog Evaluation

L5 v3 capability evaluation is separate from dataset recall benchmarks.
Installed Python entry points in
`eimemory.capability_catalog.bootstrap.v1` register trusted executors and typed
cases, after which the catalog is sealed. A dynamic case resolves one exact
capability revision and provider binding through the selected profile.

```bash
eimemory learn capability-acceptance \
  --profile l5.default --capability-scope global --json
eimemory learn l5-v3 \
  --profile l5.default --capability-scope global --persist --json
eimemory learn l5-readiness \
  --reader-mode v3 --profile l5.default --json
```

EvaluationSpec derivation is stable across repeated runs; the same immutable
case/revision pair retains one spec digest while each execution produces a
distinct run and observation. Governed probes preserve the executor verdict in
the persisted run. The Hongtu catalog returns aggregate recall checks only and
uses exact binding selectors so Hermes and OpenClaw accumulate independent
evidence. Recalled payloads are not evaluator output.

## Production recall gate

`eimemory eval production-recall DATASET.json` opens the dataset through the
trusted scheduler loader. The evaluator receives open-time
`secure_dataset_fingerprint.v1` evidence; symlinks, unsafe file modes, and
untrusted path components fail closed. The current policy is
`production_recall_gate_policy.v6` (temporary absolute ranking floors
mrr ≥ **0.50**, ndcg@5 ≥ **0.55**; pollution/leak hard constraints unchanged;
G2c forces only agents with natural traffic; G3 semantic judge `max_new`
remains **8**). Historical `production_recall_gate_policy.v5` digests retain
mrr/ndcg@5 ≥ **0.80**. Evidence floor from 1.14.64: at least **3 cases and
3 accepted labels**
in total, with **no fixed per-host quota**. Required channels are those observed
in the dataset; `coverage_scope=dataset_only_not_all_installed_channels`.
A Hermes-only dataset can satisfy coverage without OpenClaw or Codex samples.
This does not certify an enabled client or participant missing from the dataset.
An older policy-v1 report cannot qualify as the predecessor baseline. The current
production dataset is incomplete and no accepted production gate is claimed
here. The earlier 1.14.64 / `f61487e2` deployment attempt failed health
acceptance with `TimeoutError`; nightly did not start, so no new formal result
exists. At that failed attempt, service health was unverified. An earlier nightly
reported formal recall unaccepted at
0/15 cases in 1.14.63 / `4fbcbbc9`, with 321 automatic reviews, zero passes and
zero pending conclusions. Deployment and nightly passed; smoke passed 10/10,
with error rate 0.00 and hit@1 / hit@5 both 1.00. The formal gate remains false
because of `recall_quality_evidence_incomplete`. Current formal standards also include noise ≤0.40 and
precision@3 ≥0.60; missing evidence blocks formal scoring. Fixed-item smoke
results do not certify natural queries, no-answer quality or every colleague.

The reported 1.14.63 production result still uses its original v4 contract and
15-case floor. The explicitly requested v5 count change does not rewrite older
receipts, create accepted labels or establish production acceptance of 1.14.64.
Historical policy payloads and digests retain their original floors. Current
acceptance requires the current policy and bound evidence for the exact release.

### What the case and label minimum establishes

The count floor prevents a small known-item set from becoming formal acceptance
on its own. The repository originally introduced 15 cases alongside three
five-case client quotas; later policies narrowed coverage to observed hosts and
removed those quotas while retaining the total floor. Version 1.14.64 lowers
the floor to **3** at the operator's explicit request. Both counts are engineering
policy choices, not statistical derivations of accuracy.

A case is a query observation; an accepted label identifies a relevant record,
its grade and trusted evidence for that query. These are different counts, not
six independent observations. Because every eligible case already requires at
least one accepted label, three eligible cases imply at least three labels under the
current schema. Aggregate label sufficiency still cannot cover an unlabelled
case, and three labels on one case cannot replace three cases. Trusted label evidence
remains essential even though the total label floor is logically implied by
the case floor for otherwise valid datasets.

Three cases provide a minimal scoped check. Passing them does not statistically
establish accuracy for all queries, all enabled hosts or all colleagues.

Report the dataset's exact scope and observed host counts separately from the
team's actual enabled-host/member coverage and cross-client product coverage.
Actual-use acceptance needs participant-level identity and case coverage;
a dataset count alone cannot establish it. See
[team acceptance scope](team-recall-acceptance.md#acceptance-scope-and-evidence-counts).
Existing authorized memories can supply controlled targets and companion probes;
see [the four-check procedure](team-recall-acceptance.md#using-existing-memory-for-four-checks).

The diagnostic `false_recall_rate` retains its existing mixed definition:
known-item cases with results returned but no expected match, plus no-answer
cases with results returned, divided by all cases. The earlier 1.14.62 report's
numerator is one known-item miss and zero no-answer returns, denominator ten.
That measured violation blocked the gate before evidence sufficiency. The
later 1.14.63 report records rate 0.00; its remaining block is insufficient
formal evidence. Since 1.14.63, newly persisted diagnostic reports retain
`false_recall_count`, `false_recall_denominator`, `false_recall_breakdown` and
sample `expected_record_ids` / `returned_record_ids` alongside the existing
match/rank/empty flags. Query text, titles and returned bodies stay excluded.
These fields improve diagnosis without changing the metric or ≤0.05 threshold;
they do not supply trusted labels or repair older receipts.

Version 1.14.63 also uses full payload identity for digest/synthesis page
deduplication. The prior summary-prefix key could lose a different dated digest
while returning another page. Native local regression tests reproduce this with
an older known target and a newer same-prefix digest from the same 256-source
set, keeping query, expected ID and stable cutoff unchanged. Full-payload exact
duplicates still collapse, and per-source limits and default digest suppression
remain in force. The operator's 1.14.63 readback reports deployment, nightly and
smoke success. Aggregate smoke recovery does not establish that the identical
historical case was rerun or prove the historical loss stage, and does not
certify formal recall quality.

Labels must satisfy the current trusted operator or evidence-bound authorized
`auto_review` path. Historical delegated labels cannot be counted as accepted
gold. Release authorization must be verified independently; neither smoke nor
successful deployment grants it.

### Completed automatic label reviews (1.14.61)

Every scanned case now receives `review_complete=true` and `review_verdict=pass`
or `fail`, with a persisted receipt and reasons. Missing semantic transport,
unknown semantic judgment or insufficient independent signal agreement produces
a completed **fail** without certifying answer quality. Structural evidence loss
remains not evaluable; its review conclusion is non-passing. Approval still needs
validated semantic relevance AND independently verified proof or explicit host
use, signed label evidence and the existing exact capture/source/scope checks.

Original capture records remain active. Another automatic review can pass after
qualifying evidence arrives; previous failure receipts remain immutable, and
unchanged failed inputs reuse their receipt. Disabled approval or absent signing
keys cannot create accepted labels. This behavior applies to cases processed by
the bounded review batch, not historical receipts that were never reevaluated.

Semantic and research reviewers share supported Hermes installation discovery;
the semantic child keeps tool execution and filesystem writes disabled. Runtime
discovery does not prove model endpoint readiness. Nightly diagnostics expose
bounded pass/fail counts and allowlisted non-passing reasons, memory dataset
source/count/skip reasons, hypothesis-producer skips and reported candidate counts.
Absent fields remain unknown; an evolution result count is not a hypothesis count.

Since 1.14.62, review diagnosis uses a versioned catalog of exact producer-owned
reason codes. Capture boundary failures, absent matching decisions, original-query
failures, low-signal features, candidate loss and undelivered semantic inputs
remain distinguishable in persisted nightly summaries. Unknown strings still
map to `reason_not_allowlisted`; oversized maps set `not_passed_reasons_truncated`.
Reason counts are per-case deduplicated occurrences and may overlap across a
case. The diagnostic catalog does not admit a label, certify a true no-answer,
reconstruct missing capture evidence or create benchmark cases/hypotheses.

```bash
eimemory eval production-recall production-dataset.json \
  --no-seed --persist-report --output production-recall-report.json
```

Use a qualifying securely loaded dataset and inspect the persisted gate verdict,
identity, label trust and blocking reasons after execution. See
[receipt references and remaining work](acceptance-status.md).

### New production cases and repeated review scans

A review scan can include previously captured cases. Its total does not measure
new case arrival, and repeated unchanged evidence cannot repair missing capture
authority or create trusted labels. Semantic-observation counts also describe a
different producer and cannot substitute for new production-case counts.

Since 1.14.68, `nightly_diagnostics.production_recall_collection` persists the
native new/existing/decision/empty counters, separate new non-empty/empty counts,
and up to 50 exact new pending IDs, prioritizing non-empty new cases. Each
`new_case_reviews` entry joins only those IDs to the same run's reviewer output.
Native review/acceptance references, allowlisted reasons, signal counts and
semantic status/verdict support tracing an actual new case to its conclusion.
Missing signals and legacy collection fields remain unknown; duplicate matches
remain ambiguous, and truncation is disclosed. See
[fields and bounds](operations.md#nightly-production-case-collection-diagnostics).

The 1.14.67 report `ref_5df3562505ca` lacks the collection section, despite
`recall_semantic_relevance.new_count=9`. It cannot identify a newly collected
case or establish progress from zero to one trusted case. The diagnostic
repair does not populate benchmark data, authorize labels, fill missing
history or change review eligibility and formal quality thresholds.

### Session deduplication and empty evaluation candidates

Since 1.14.66, a collected case's `capture_ref` leads to the decision's
`retrieval_diagnostics.session_dedupe`: removed record/source IDs and one exact
qualifying historical decision/citation with the state observed at deduplication.
This distinguishes a session-suppressed candidate from an unexplained empty
result. See [fields and bounds](operations.md#session-deduplication-provenance).

The collector continues to use this decision's final persisted items, rather
than historical delivered candidates. If all voluntary candidates were deduped,
`candidate_refs` stays empty. Its automatic review completes as not evaluable
with `no_candidate_refs` / `no_recall`; this creates neither a positive label
nor proof of correct no-answer. Independent no-answer evidence still requires
its own complete acceptance path. Historical count-only audits cannot be
backfilled; the operator's four earlier decisions remain individually unverified.
See [the completed terminal receipt](acceptance-status.md#session-deduplication-readback).

## LongMemEval Raw Evidence

`eimemory eval longmem` runs a LongMemEval-style retrieval benchmark without
LLM calls. It ingests each case's haystack as `raw_chunk` records, retrieves
raw evidence, and reports retrieval metrics only. It does not score generated
answers or QA accuracy.

```bash
eimemory eval longmem examples/evaluation/longmemeval_smoke.json \
  --mode raw \
  --granularity session \
  --limit 10 \
  --output tmp/longmemeval-report.json
```

Options:

- `--mode raw` searches raw evidence directly.
- `--mode hybrid` asks memory recall for raw-hybrid evidence first, then falls
  back to raw retrieval.
- `--granularity session|turn|chunk` selects which evidence id type is scored.
- `--persist-report` writes a `reflection` report with
  `source="eimemory.longmemeval"` and `meta.report_type="longmemeval_eval"`.
  Governance snapshots surface the latest report under `longmemeval`.

Dataset cases accept LongMemEval-like aliases:

- question fields: `question` or `query`
- answer fields: `answer` or `expected_answer`
- haystack fields: `haystack_sessions`, `sessions`, or `haystack`
- text fields inside sessions/turns/messages: `content`, `text`, or `message`
- evidence fields: `evidence_session_ids`, `evidence_turn_ids`, and
  `evidence_chunk_ids`

Report metrics include `retrieval_recall_at_1/5/10`, `recall_any_at_k`,
`recall_all_at_k`, `ndcg_at_5`, `mrr`, latency average/p95,
`by_question_type`, and per-sample returned evidence ids.

For `--granularity turn`, LongMemEval keeps session chunks intact and stores
per-turn text metadata. Retrieved sessions are then locally ordered by turn
text against the query before scoring, which avoids penalizing correct session
retrieval simply because the evidence turn appears late in the session.

The full benchmark helper `scripts/run_full_eval.py` accepts these environment
knobs for production probes:

- `EIMEMORY_RUN_ONLY=all|lme|locomo` to run both suites or one targeted suite.
- `EIMEMORY_WORKERS=<n>` to set worker count.
- `EIMEMORY_LME_LIMIT` and `EIMEMORY_LOCOMO_LIMIT` to cap per-worker retrieval.
- `EIMEMORY_RERANKER=auto|deterministic|llm` to control raw reranking.

Aggregated LME reports include `failure_count`, `rank_histogram`, and
`failure_examples` so low-score runs show whether misses are rank placement,
missing evidence, or retrieval failures.

## Public Benchmark Harness

`eimemory eval public-benchmark` runs public benchmark adapters with an
isolated temporary runtime. It is the preferred entry point for full
LongMemEval or LoCoMo datasets because it never writes to the production DB at
`/var/lib/eimemory/state/eimemory.sqlite`.

```bash
eimemory eval public-benchmark examples/evaluation/longmemeval_smoke.json \
  --suite longmemeval \
  --mode raw \
  --granularity session \
  --output tmp/public-longmemeval-report.json

eimemory eval public-benchmark examples/evaluation/locomo_smoke.json \
  --suite locomo \
  --mode raw \
  --granularity turn \
  --output tmp/public-locomo-report.json
```

The report's `metrics` object includes normalized `r_at_1`, `r_at_5`, `mrr`,
`ndcg_at_5`, latency, and failure samples. The adapter-specific report is
under `report`.

`eimemory eval locomo` is also available for adapter-level smoke runs. It
accepts LoCoMo-like `conversation`, `messages`, `turns`, `sessions`, or
`conversation_sessions` fields and scores `session`, `turn`, or `chunk`
evidence ids.

## Real Task Replay

`eimemory eval task-replay` runs `real_task_replay.v1` cases for OpenClaw,
UUMit, and eimemory history-derived tasks. Seeded runs use temporary state so
smoke datasets do not contaminate production memory.

```bash
eimemory eval task-replay examples/evaluation/real_task_replay_smoke.json \
  --output tmp/real-task-replay-report.json
```

The replay schema supports:

- `source_system`: `openclaw`, `uumit`, `eimemory`, or another source label.
- `query` / `input` / `prompt`: the replayed user or system request.
- `task_type` and `task_context`: routing context for recall.
- `expected_text`: terms that should be recalled.
- `negative_expected_text`: terms that must not be recalled.

To build a larger replay set from local outcome traces, corrections, and
previous replay suggestions:

```bash
eimemory learn replay-dataset --limit 100
```

To add the curated regression set from real operator feedback and incidents
(wrong version answers, missing evidence checks, long-task silence, field
mapping mistakes, and unsupported evaluation claims), pass:

```bash
eimemory learn replay-dataset --limit 100 --include-built-in-regressions
```

The generated dataset is marked `schema_version: real_task_replay.v1` and
includes source-system labels without storing secrets or private tokens.

## Actionable Memory Evaluation

`eimemory eval actionable` runs a compact smoke suite for recall + posture +
contamination checks.

```bash
eimemory eval actionable examples/evaluation/actionable_memory_smoke.json \
  --output tmp/actionable-memory-report.json
```

Cases support:

- `case_type: recall` for mixed recall checks.
- `case_type: posture` for posture-profile checks.
- `query_type` for intent-aware recall (`project`, `research`, `chat`, etc.).
- recall assertions: `expect_any_title`, `expect_any_record_id`,
  `expect_any_kind`, `expect_any_text`.
- contamination assertions: `forbid_any_title`, `forbid_any_kind`.
- posture assertions: `expect_profile_non_empty`, `expected_constraints`.

The report includes:

- `ok`
- `report_type` (always `actionable_memory_eval`)
- `sample_count`
- `pass_count`
- `pass_rate`
- `recall_topk_pass_rate`
- `posture_pass_rate`
- `contamination_rate`
- `project_query_contamination_rate`
- `samples`

Persisted reports are written as `kind="reflection"` with
`source="eimemory.actionable_memory"` and
`meta.report_type="actionable_memory_eval"`.

Governance snapshots now include:

- `actionable_memory.posture_profile_count`
- `actionable_memory.posture_coverage`
- `actionable_memory.project_query_contamination_rate`

## Living Memory Evaluation

`eimemory eval living` runs a deterministic LivingMemEval smoke suite against
`record.meta["living_memory_v1"]`. Seed records are enriched before scoring
when living metadata is absent, using the local living-memory helper if it is
available.

```bash
eimemory eval living examples/evaluation/living_memory_smoke.json \
  --output tmp/living-memory-report.json
```

Dataset cases bind to seed records by `seed_id` or `seed_index` and can assert:

- `expect_temporal`
- `expect_motive`
- `expect_affective`
- `expect_repair_needed`
- `expect_stale`
- `expect_posture`

Reports include `sample_count`, `pass_rate`, `temporal_accuracy`,
`motive_accuracy`, `affective_grounding`, `repair_recall`,
`stale_label_avoidance`, and `posture_accuracy`.

Operator-facing living-memory commands emit JSON:

```bash
eimemory living enrich --limit 100
eimemory living timeline
eimemory living posture "repair before proceeding"
```

Governance snapshots include a `living_memory` section with enriched counts,
repair-needed counts, open future-intent counts, life-phase counts, and average
ripeness.

`action_posture.recommended` uses the canonical posture values `act`, `nudge`,
`wait`, and `let_go`; explanatory fields such as friction, urgency, trust risk,
and ripeness describe why that posture was chosen.
