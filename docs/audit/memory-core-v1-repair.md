# Memory core v1 retrieval repair — local implementation audit

Date: 2026-09-10. Worktree: `/home/darrow/tmp/eimemory-core-v1-worktree`, branch `fix/memory-core-v1`.

**Result:** implementation and isolated regressions are present, uncommitted. Final focused run: **261 passed, 4 deselected**. The four deselected tests previously failed because this sandbox prohibits creating listening sockets. This is not production verification, an evaluation gate, or a claim that all four production failures are resolved.

## Evidence and boundaries

Read the acceptance checklist, `cases.json`, `baseline.json`, `semantic-review.json`, and `github-reference-review.md` in `/home/darrow/tmp/eimemory-core-v1`. Their labels remain provisional. No source snapshots were changed. No production repository, database, configuration, RPC, service, release gate, or fail-closed marker was changed. No credential files were read. No model workers were spawned and no model/provider settings were changed. No commits, pushes, deployments, or restarts were performed.

The downloaded reference review informed the emphasis on event time and source lineage. No third-party implementation was copied or installed. Existing admission, fragments, record aliases, task intent, TimeRef, L1 persistence, and authority validation were reused.

Only the supplied baseline provides production failure evidence here. It contains compact returned records, not the original candidate rows, fragment IDs, complete raw phone conversations, extraction decisions, or index contents. Tests use explicitly synthetic records and injected embedding/SQL responses. SQLite authority hydration, digest checking, fragment reconstruction, selection, RPC adapter dispatch, and compact packaging run as actual code in the integration tests.

## Root causes and repairs

### FACT-02: device model versus education

Original evidence: baseline returned an education fact for a device-model question; the main-thread review reports that matching device information exists in scoped raw conversations. Raw presence does not establish successful extraction/indexing.

Observable code defects:

* `answer_requirements.py` recognized money/task shapes but no device-model property. Person similarity could therefore admit unrelated education.
* The heuristic L1 extractor had no explicit equipment-ownership fact branch. A short synthetic statement of the user's phone model yielded zero atoms. The existing LLM route is separate, and its production result was not observed.

Changes: recognized device object/model requirements for phones, computers and routers, with Chinese/English object alternatives, unknown-property rejection and explicit named-subject checks. Added a narrowly scoped heuristic branch for first-person equipment facts, without inserting a particular user's name. Identifier-bearing statements are rejected by this new branch rather than retaining IMEI/serial data. Existing source-message IDs and derived-from links pass through unchanged.

Verified: education, different owner, unknown model, battery/state-only negatives; model and Chinese-only paraphrase positives; heuristic extraction → existing L1 persistence → authoritative candidate validation → admission, with original synthetic episode IDs retained in the stored record.

**Unresolved production stage:** the authorized original phone raw records are not supplied as a safe full snapshot. Their message shape, extraction route/outcome, atom identity, index revision, selected fragment and rank are unverified. The existing extractor's length/question gates and already-extracted markers remain intact; this change does not reprocess historical records. A generic `用户` atom is not automatically an alias for every named person in a question. Authority-owned aliases or explicit source wording are still necessary. Do not report the original phone query as fixed end-to-end.

### TEMPORAL-01: latest task versus retrieval commentary

Original evidence: baseline returned commentary about failed task retrieval. A requested version appearing in that commentary was a false positive in the prior coarse check.

Observable defects:

* Task evidence accepted any state token, including the `completed` in a generic capture title such as `completed turn`.
* Acceptance criteria, questions and discussion of returned L0/L1 results could count as task evidence.
* No project ambiguity resolution existed. Host project context did not constrain candidate selection; additionally, changing the local matching query alone would not change the immutable request sent to the candidate source.
* Lightweight admission prioritized similarity/record identity, not relevant event time, even for latest-task requests.

Changes: ignore capture-completion titles and non-assertive/metadiscussion sentences when checking task state. Resolve explicit project/named software first, then `task_context.project`, `project_name` or `project_id`; explicit global/all wins. An explicitly named task remains a narrower scope. Unresolved generic latest/last queries return `ambiguous` for host clarification. Both candidate source requests and final matching receive the resolved project. L0 RPC now carries only the three project-context keys into its fixed recall context; it does not accept arbitrary visibility overrides. Qualified latest-task candidates are ordered by `time.occurred_at`, not `updated_at`; the similarity score-gap cutoff no longer suppresses otherwise qualified latest evidence. Minimum cosine/coverage and authority checks still apply.

Verified: explicit project, inherited project, explicit software overriding inherited context, global aggregation, ambiguous Chinese/English queries, direct named task compatibility, old event re-written later versus newer event, and host rendering of ambiguity/history boundaries. Existing budget fixtures now say `全局` where their former queries assumed implicit cross-project scope; all their original result/timeout/security assertions remain. The new tests separately assert that the former unscoped requests are ambiguous.

Limit: event time is only as reliable as stored `occurred_at`; records whose ingestion defaulted event time to capture time were not repaired. Sentence checks are bounded heuristics, not a complete semantic task-state classifier. No task ledger was queried. Output explicitly says historical evidence, current execution state unverified, and instructs the host to consult its task ledger.

### TRACE-01: requested release versus gateway recovery

Original evidence: the selected baseline record was gateway-recovery content. The main-thread scoped SQL review found the requested version absent from both its summary and content_text. The baseline omits fragment/component metadata, so the original parent/fragment mismatch cannot be reconstructed here.

Observable defects: lightweight admission had no required release/entity alignment. A high cosine plus low lexical coverage could admit an unrelated fragment, including one from a parent containing the release somewhere else. Compact records and rendered host context used the beginning of the parent summary instead of the admitted span.

Changes: explicit semantic-version tokens must match whole version tokens in the actual selected fragment (`2.14.7` is not `2.14.70`); explicitly named project/software identity must also be supported, with authority-owned record aliases accepted. Semantic property questions cannot escape through exact-question-title identity lookup. Compact output reconstructs the admitted fragment from the same scope/source/record projection and fragment ID, supplying `evidence_excerpt`, `evidence_fragment_id` and projection offsets. Parent record/source IDs remain unchanged. Host loadout rendering uses that excerpt and includes the record citation.

Verified: wrong project, wrong version, prefix-version collision, correct version only elsewhere in a parent, tampered fragment checks in existing tests, selected tail fragment, and `adapter.search_l0` through real local authority validation/compact output. No records were mutated to manufacture excerpts.

Limit: offsets refer to the existing keyword projection, not raw-file byte positions. No live citation-opening or original PostgreSQL fragment row was inspected. A local L0 test returning the right parent does not prove the original production index has the right projection. Caller-assisted candidates that did not meet deterministic admission retain the existing compact summary behavior; the new excerpt path is verified for deterministically admitted fragments.

### ABSENCE-01: unknown project and missing contract amount

Original evidence: an explicit synthetic unknown-project query admitted another real project's procurement status as `evidence_found`. The baseline did not contain an answer-generation step; this audit does not claim a fabricated amount was generated.

Observable defects: `金额/总金额` was absent from the money-question vocabulary; explicit project identifiers were unconstrained. Similarity and entity-adjacent terms were treated as answerability.

Changes: recognize amount questions, require amount evidence, enforce explicit project identity, and require identity plus amount in the same sentence for explicit-project money questions. Version/entity/property rejection happens before score-gap selection and before candidates are eligible for optional caller assistance. Normal semantic queries are not globally forced into exact matching, and explicit record aliases are preserved.

Verified: wrong project with and without an amount, matching project without amount, another project's amount in an adjacent sentence, correct amount, Chinese alias, supply-model and correction positives. Healthy empty selection remains `no_evidence`; missing backend, stale authority/index and budget-limited incomplete search retain unavailable/partial-evidence behavior.

Limit: this is not a general relation extractor. Complex cross-sentence references or novel paraphrases can need clarification or additional semantic verification. The changes do not establish that every amount mentioned in a matching sentence is the requested contractual amount.

## Changed files

* `eimemory/retrieval/answer_requirements.py`: property/object/entity/version requirements.
* `eimemory/retrieval/lightweight_admission.py`: apply requirements before ranking/assistance, prevent question-title bypass, event-time ordering, scope-bound fragment diagnostics; policy identity becomes `lightweight-evidence-admission.v3` without changing calibrated thresholds.
* `eimemory/recall/task_queries.py`: task evidence semantics and project scope resolution.
* `eimemory/retrieval/engine.py`: ambiguity response and resolved query/context propagation to candidates.
* `eimemory/adapters/runtime/service.py`, `eimemory/adapters/eibrain/rpc.py`: bounded inherited project context for L0.
* `eimemory/models/records.py`, `eimemory/recall/loadout.py`: admitted excerpt/citation packaging, historical-state and ambiguity rendering.
* `eimemory/retrieval/diagnostics.py`: expose ambiguity status.
* `eimemory/knowledge/sediment.py`: bounded equipment-fact extraction.
* `tests/test_memory_core_v1_repair.py`: 39 synthetic regressions, including integration paths.
* `tests/test_recall_budget_reserve.py`: explicit-global task fixtures and optional synthetic fragment selector for tail-fragment integration.
* `tests/test_task_recall_repair.py`: explicit-global requests for existing cross-project routing tests; no assertions removed or weakened.

## Commands and results

Tests used the existing `/dev-project/eimemory/.venv/bin/python` interpreter, with imports from this worktree. The production repository was not edited. Every test process removed all inherited `EIMEMORY_*` variables before importing tests. The temporary launcher `/tmp/memory-core-test.py` used this exact body:

```python
import os
import sys
for key in list(os.environ):
    if key.startswith('EIMEMORY_'):
        del os.environ[key]
os.environ['PYTHONPATH'] = os.getcwd()
os.execv(sys.executable, [sys.executable, '-m', 'pytest', *sys.argv[1:]])
```

Red evidence, before corresponding fixes:

* Initial synthetic admission run: **8 failed, 6 passed** (education, amount, entity, version, meta-task, unknown property and wrong parent fragment).
* Adding context/packaging checks: **3 failed, 16 passed** (inherited project, ambiguity, missing excerpt).
* Equipment extraction returned zero atoms; deterministic event-time test failed after fixing IDs to avoid a random-order pass.
* Original-query-to-index context and host rendering tests: **3 failed, 26 passed**.
* L0 inherited context test failed while its explicit-version sibling passed.
* Original baseline replay exposed capture-title task false admission; a synthetic `completed turn` regression failed before its fix.
* Device identifier-retention regression failed before the new extraction branch rejected it.

Intermediate complete focused sets included **42 passed**, **72 passed**, **128 passed** and **82 passed**; these overlap and must not be added as independent cases.

Expanded run, before the final two regression additions: **259 passed, 4 failed**. All four failures were `PermissionError: [Errno 1] Operation not permitted` at socket creation, before exercising HTTP behavior. No assertion was changed, and no escalation was attempted.

Final command (same 14 directly affected test files, only the four known socket tests explicitly deselected):

```bash
rtk proxy /dev-project/eimemory/.venv/bin/python /tmp/memory-core-test.py -q \
  tests/test_memory_core_v1_repair.py tests/test_lightweight_evidence.py \
  tests/test_task_recall_repair.py tests/test_recall_budget_reserve.py \
  tests/test_l1_extract.py tests/test_task_evidence_scan_budget.py \
  tests/test_live_task_evidence.py tests/test_live_task_acceptance.py \
  tests/test_recall_diagnostics.py tests/test_recall_sqlite_deadline.py \
  tests/test_postgres_vector_source.py tests/test_models.py \
  tests/test_runtime_adapter_rpc.py tests/test_runtime_channel_adapter.py \
  --deselect=tests/test_postgres_vector_source.py::test_openai_embedding_does_not_forward_bearer_across_redirects \
  --deselect=tests/test_postgres_vector_source.py::test_openai_embedding_enforces_total_deadline_on_slow_drip \
  --deselect=tests/test_runtime_adapter_rpc.py::test_runtime_http_client_calls_authenticated_rpc \
  --deselect=tests/test_runtime_adapter_rpc.py::test_runtime_http_attestation_requires_separate_producer_credential_and_channel_match \
  --tb=short
```

Result: **261 passed, 4 deselected in 18.68s**. This includes source/digest validation, cross-scope checks, caller budget reserve, SQLite deadlines and directly affected adapter/model behavior. It is not the full suite. The four deselected tests require a local environment allowing loopback listeners; they remain unverified here.

`rtk proxy git diff --check` exited 0.

### Original baseline material replay

Command: `rtk proxy env PYTHONPATH=. /dev-project/eimemory/.venv/bin/python /tmp/memory-core-baseline-admission.py`.

This read-only diagnostic reconstructs each compact baseline title/summary as an isolated record, uses the original query, generates a local first-fragment hint with synthetic cosine 0.9, and invokes the real lightweight gate with an available synthetic backend. It deliberately does **not** claim to reproduce original embeddings, full content, candidate retrieval, real index readiness or original ACL proof. Only IDs/status/counts are printed; original contents are not copied into tests or this report.

| Case | Final replay status | Selected count |
| --- | --- | --- |
| FACT-01 | evidence_found | 1 |
| CORRECTION-01 | evidence_found | 1 |
| FACT-02 | no_evidence | 0 |
| TEMPORAL-01 | no_evidence | 0 |
| TRACE-01 | no_evidence | 0 |
| ABSENCE-01 | no_evidence | 0 |

The initial replay still admitted TEMPORAL-01 because its capture title contained `completed`; after the regression/fix all four wrong returned materials are rejected. This is old-result admission replay, **not** successful retrieval of the missing correct production results. No labels or baseline files were rewritten.

## Main-thread reproduction and remaining acceptance

1. Review this diff, recreate the environment-scrubbing launcher above if necessary, and run the focused command. Run the four socket tests unchanged in an environment permitting local listeners.
2. Re-run all six original `cases.json` queries through the authenticated main-thread path when authorized. Supply the confirmed project through `task_context.project` for the inherited task case; separately test explicit project, global and unscoped ambiguity. `adapter.prefetch` already forwards task context; `adapter.search_l0` now forwards only its project keys. The host must actually provide that context; this worker did not modify a live host configuration or its task ledger.
3. For the phone case, export only the authorized relevant source records and extraction metadata: raw IDs, extraction status/route, atom IDs, derived-from/message IDs and event timestamps. Redact device identifiers. Trace those same IDs into index state/projection, candidate hits, hydration, selected fragment and compact output. A marked-extracted empty episode will not automatically retry after this patch; deciding on a scoped re-extraction/backfill is a separate main-thread action.
4. For the named-release case, capture the original candidate parent identity tuple (scope, source_id, record_id), authoritative projection digest, fragment ID/offsets, projection text limit and index revision. Verify requested project/version in the exact fragment, not just another part of the parent. Verify compact citation opens that same authorized original record. Neither an unrelated gateway parent nor a truncated summary is sufficient proof.
5. For latest-task evidence, verify actual event times and direct assertions. Compare history with the host's live ledger; do not infer current completion from capture time, a version token, or `evidence_found`. Confirm newer relevant evidence is indexed before measuring selection.
6. Verify the unknown-project negative, missing-property negatives, Chinese aliases and supply/correction positives on available authorized evidence. Do not fabricate production facts, natural evaluation traffic or trusted labels to fill gaps.

Independent core startup, backup/restore, production extraction/backfill, real PostgreSQL/embedding behavior, live ledger reconciliation, and production citation opening were not executed. No production acceptance percentage is asserted.

## Main-review follow-up — 2026-09-10

This section supersedes the earlier limitations where new evidence is available; the original failed runs above are preserved. Work remains uncommitted, in the same isolated worktree. No production access, credentials, model calls/workers, commits, pushes, deployments, restarts, gate edits or task-ledger edits were performed.

### Independent main-thread evidence

Main independently ran the original 14 focused files **without deselections: 265 passed in 20.97s**, recorded in `/home/darrow/tmp/eimemory-core-v1/main-review-tests.log` (final output read here). This covers the four socket tests unavailable in this sandbox for the earlier implementation. It is distinct from the initial synthetic suite and is not a verification of these follow-up changes. Known unavailable listeners were not rerun.

### Requested monetary property, not any money

The review finding was valid: the original money matcher accepted project identity plus any amount, including prepayment/budget alongside an unknown total. Ten generalized ORION cases were added first: **6 failed, 4 passed**. They cover unknown/undetermined/negated totals, installment-only amounts, both orders of total plus prepayment, currency-prefixed actual totals, explicit cross-sentence contract anaphora, and another-project negatives.

`answer_requirements.py` now binds an amount to its requested monetary role (contract amount/total, prepayment, budget, installment or unit price), requiring an affirmative currency-bearing amount in that role's clause. Both main's ALPHA examples are rejected without project-specific code. Actual totals survive adjacent prepayments, and explicit “该合同” references can inherit the immediately preceding project. This remains bounded deterministic matching; novel syntax may fail closed.

### Real-source local replay and extraction trace

Input: `/home/darrow/tmp/eimemory-core-v1/scoped-original-evidence-redacted.json`, eight records. Reproducible script: `docs/audit/memory-core-v1-source-replay.py`. Source-derived results/excerpts remain local at `/tmp/memory-core-source-replay.json`; console output is `/tmp/memory-core-source-replay.log`. No source text was copied into unit tests.

The replay creates a disposable RuntimeStore, preserves exported record/source/scope identities and stored timestamps, and computes fresh fixture/projection digests. Original hashes are provenance only. Local fragments pass through actual SQLite authority validation, candidate-source logic, admission and RPC compact packaging. **Embedding transport and SQL candidate rows are simulated**, using equal cosine 0.9 and lexical fragment ranking. Production candidates/index revisions, original image pixels and live citation opening are not verified. The disposable store is removed afterward.

Phone source: `mem_cdeb4e51ae6eac73b3ca0689603ddb82`; source `hermes.memory`, source_id `hermes`, source_event_id `20260907_144314_8210cf15:turn-2a881232e633e4f3725f96d7`; event time **2026-09-08T09:40:37Z**. The real scope is `default / hongtu / embodied::channel::hermes / darrow`.

Its exported content_text repeats User/Assistant sections. The user message is an image question followed by an attachment path, 117 characters; model fields and the named addressee occur in the assistant response. The existing question guard does **not** reject this actual representation because the path follows the question, and length is below 280. The standalone image question would be rejected. The old heuristic finds no durable user assertion and yields no atom. The other seven user portions are 8–31 characters, pass the guard, and still yield no heuristic atoms.

All eight exported l1_metadata objects are empty; their meta-key lists contain no extraction marker. A recorded l1_extracted_at gate does not explain this fixture. Precise missing production fields are the original structured content map, full runtime/business metadata, extraction queue job/outcome/route, existing child IDs and index revision. content_text is a projection, not proof of the exact original content.text value; replay supplies that representation to the actual record-extraction API. No image file or model was accessed.

The new bounded image branch in sediment.py retains an **attributed episodic observation**, requiring an explicit addressee-owned phone and labeled model fields. It does not infer every generic user is a named person, or promote recommendations into ownership facts. It retains only model fields, not image paths or unrelated identifiers. Assistant parsing now stops at the next User section, preventing repeated-projection duplication.

Actual local extract_l1_from_l0_record → persist_l1_atoms writes one atom. Its evidence and derived_from link preserve the source record ID. l1_pipeline.py preserves the authoritative parent occurred_at instead of substituting extraction time. A second extraction of the marked record returns empty. Named-phone local recall selects the derived atom; the replay asserts the displayed excerpt belongs to that atom's selected fragment. Generated atom IDs vary by disposable replay and are recorded in the JSON. This is historical attributed image evidence, not independent verification of current ownership.

### Source selection results and remaining project gap

Evidence IDs:

- **D**: `mem_df7ad26536de2082cf1e68f94634bec8`, deployment report, **2026-09-09T17:25:35Z**, source event `20260909_130344_0b595aed:turn-aecb3574cf9d0a0048039431`.
- **R1**: `mem_7e1514eaa9e89cc5bf3b75742b88b98a`, review/acceptance discussion, **2026-09-09T17:51:03Z**.
- **R2**: `mem_0382214236059cf33d04e75cdf386fa7`, retrieval review, **2026-09-09T17:28:50Z**.

| Local RPC L0 request | Result | Selected |
| --- | --- | --- |
| eimemory 1.13.9 部署 验收 | no_evidence | none |
| 最近任务进展, inherited project=eimemory | no_evidence | none |
| 1.13.9 部署 验收 | evidence_found | D |
| 全局最近任务进展 | evidence_found | D, despite newer R1/R2 |

D's supporting fragment is `e0874e03928a2d261553ea6964ad73b8bbdfbed2cb821e559c11b38fe2f5f74e`. Both successful L0 requests assert displayed excerpt membership in this exact fragment and matching parent/source citations. JSON retains excerpts and fresh fixture digests. An independent all-fragment admission matrix verifies **no R1/R2 fragment passes any of the four queries**, rather than hiding bad candidates behind limit=1.

Initial replay exposed version-only false admission and an R1 tail fragment containing acceptance criteria that passed task checks. Two generalized regressions failed before correction. Release deployment requests now require a direct deployment assertion containing the requested version/entity. Task fragments consisting of acceptance criteria without direct execution assertions are rejected. D remains accepted.

**Named/inherited eimemory selection remains unresolved.** D's exported summary/content contains the release and execution report but no eimemory identity; limited metadata has no authority-owned project association/alias. Neither a channel scope nor requested project context proves every channel record belongs to that project. The precise missing evidence is a source-grounded project association or linked context for D, not repetition of deployment facts already present. No alias was invented. Version-only/global successes are separate diagnostic requests, not substitutes for passing the original named/inherited cases.

Project behavior stays current context when known, explicit global aggregation, otherwise clarification. A live host must supply context or contextualize its query; local adapter support is not live integration. No ledger edits were made.

### Follow-up validation

Every test command used the existing environment-scrubbing launcher. No full suite or known unavailable socket listener was run.

- Money red: `-q tests/test_memory_core_v1_repair.py -k monetary_role --tb=short` → **6 failed, 4 passed, 39 deselected**.
- Image red plus money: `-k "monetary_role or captured_image or image_device"` → **1 failed, 13 passed, 39 deselected**, zero atoms.
- Duplicate capture red: `-k "attributed_device or projection_repetition"` → **1 failed, 1 passed, 53 deselected**, duplicate model fields.
- Review red: `-k review_discussion` → **2 failed, 55 deselected**.
- `rtk proxy /dev-project/eimemory/.venv/bin/python /tmp/memory-core-test.py -q tests/test_memory_core_v1_repair.py tests/test_l1_extract.py tests/test_lightweight_evidence.py tests/test_task_recall_repair.py tests/test_recall_budget_reserve.py --tb=short` → **136 passed in 7.08s**, log `/tmp/memory-core-followup-tests.log`.
- Same launcher, `-q tests/test_tencent_alignment.py --tb=short` → **5 passed in 2.34s**.
- `rtk proxy /dev-project/eimemory/.venv/bin/python docs/audit/memory-core-v1-source-replay.py` → exit 0, real-source local results above.
- `rtk proxy git diff --check` → exit 0 before documentation; repeated after final documentation.

Counts overlap earlier runs and are **not summed**. A documentation append initially failed from shell quoting; the truncated paragraph was repaired with apply_patch, without changing prior audit evidence. Main must independently recheck this follow-up. Remaining acceptance includes source project linkage, original capture/queue route, scoped backfill decisions, real index readiness, live-host context and current ledger reconciliation.

## Third pass — source-grounded same-turn project linkage

### Result and independent prior evidence

The named release and inherited-project gap is now resolved **in the local original-source replay using both exports**. This supersedes the second-pass `no_evidence` limitation, not the production acceptance boundary. Main's prior run was independently recorded as **288 passed in 22.57s**, including sockets, in `main-followup-tests.log`; its final lines were read here. That run applies to the second patch. This pass's focused run is **235 passed, 2 deselected in 11.81s**. All changes remain uncommitted. No other workers, model changes, production reads/writes, credentials, RPC calls to production, commits, pushes, deployments, restarts, ACL changes or gate changes occurred.

### Root cause and concrete change

`HermesMemoryProviderCore.sync_turn` accepted `messages` but discarded them. `AgentRuntimeMemoryService.sync_turn` persisted only user/final-assistant text. Neither its inline L1 path nor the record-based heuristic extraction could recover a project absent from that pair. Existing `RecordEnvelope.evidence`, `links`, `provenance`, scope/source partitions, event times, record projections and compact admission already provide the necessary downstream contracts; a project registry or new retrieval backend is unnecessary.

Third-pass changes:

- `adapters/hermes/provider_core.py`: preserve a bounded latest-user-turn transcript when the host supplies durable numeric message IDs, the exact final answer and actual tool observations. Optional explicit `project_context` is forwarded with its supporting message ID. ID-less histories are not assigned invented provenance.
- `adapters/eibrain/rpc.py`, `adapters/runtime/service.py`: accept the optional `supporting_turn` capture field, bind it to the actual newly captured parent, and invoke contextual extraction/persistence. The host scope is normalized through the existing channel-scope function. Invalid support leaves ordinary capture available and creates no association.
- New `knowledge/turn_context.py`: validate and extract one release/project association from the bound transcript, append the original supporting tool contents as `raw_chunk` records, and append a separate derived L0 conversation. It uses the existing store's atomic record/outbox transaction. It does not rewrite the captured parent, add aliases, promote a deployment report to an L1 durable/current-state fact, or change L1 extraction markers. The phone L1 path remains the earlier implementation.
- `models/records.py`: compact items preserve parent/support record IDs and explicit project-context provenance alongside the existing admitted fragment/excerpt. Candidate retrieval and admission need no third-pass exceptions: the derived record has source-grounded project text in its persisted projection before any query is run.
- New `tests/test_same_turn_project_context.py`: 31 synthetic cases; no actual user transcript or production record identities copied into tests.
- `docs/audit/memory-core-v1-source-replay.py`: import both original exports, call the same contextual extraction/persistence code, retain fresh input/projection digests, verify raw support contents against the export, and assert positive named/inherited results using the unchanged queries.

Validation requires exact parent record/source/event/scope agreement; ordered source IDs strictly between the preceding user boundary and bound assistant; exact assistant-content membership in the captured parent; tool roles and call IDs; a document title naming a project/version; a terminal identity containing matching service, version, full commit and release path; and the same version plus at least seven commit digits in a direct deployment assertion in the final answer. Conflicting project observations for the relevant release fail closed. Unrelated tool observations cannot supply a project and are not cited. An explicit host project must name one of the validated supporting messages. Strings inside tool payloads cannot set metadata, identity, authority, visibility or ACLs.

Only the release assertion receives a generated project prefix; the complete exact final answer follows it in the derived content, preserving incomplete acceptance and other caveats. Original parent/raw text is immutable. Derived IDs and provenance are reproducible from scope, source, source message IDs, content hashes and binding. The existing transaction covers supporting records, derived record and durable outbox entries together; retries reuse the same records. Inactive parents and inactive ID collisions are rejected.

This is bounded deterministic release-context extraction, not arbitrary project/topic inference. Supported source syntax is a `read_file` JSON content response and a `terminal` JSON output containing a one-line service identity and `/opt/<project>/releases/<commit>`. Other schemas, missing IDs, oversized/truncated evidence, ambiguous releases and unsupported project-name syntax fail closed. Host transcript provenance is an authenticated-host/export contract, not independently authenticated by parsing the tool contents.

### Original-source replay evidence

Inputs are the unchanged `scoped-original-evidence-redacted.json` and `same-turn-project-evidence.json` in `/home/darrow/tmp/eimemory-core-v1`. Their SHA-256 values and fresh record projection digests are in `/tmp/memory-core-third-source-replay.json`. Prior `/tmp/memory-core-source-replay.json` and main's replay are preserved.

The original parent remains `mem_df7ad26536de2082cf1e68f94634bec8`, scope `default / hongtu / embodied::channel::hermes / darrow`, source partition `hermes`, event `20260909_130344_0b595aed:turn-aecb3574cf9d0a0048039431`. The binding connects assistant **64754** to that parent's exact content, after preceding user boundary **64747**. Actual tool messages **64751** and **64753** are persisted locally as:

- `raw_5724aed2520e5453bdc4bf12d8317e3b` — original read-file result, source message 64751.
- `raw_1a3a21bcee78701688e17d72b0ecf1c1` — original terminal result, source message 64753.

Both raw records retain the exact exported tool content and original tool-call/message IDs. Their record timestamps inherit the bound parent event time; the export's individual tool timestamps remain in the input export and binding digest, not falsely represented as independent current observations.

The derived record is `mem_86bcc3d4ff344cd2d32faeb2e3344fa0`. It cites the original parent and both support records. Its projection digest is `2c1cd06a7a0f7b589b43faf5e6cdd2fa830a15dc181de0862aee22212ae5c8fc`.

| Unchanged request | Local result | Selected record |
| --- | --- | --- |
| `adapter.search_l0`: `eimemory 1.13.9 部署 验收` | evidence_found | derived record above |
| `adapter.search_l0`: `最近任务进展`, project=eimemory | evidence_found | derived record above |
| Original `adapter.prefetch`: `最近 已授权 任务 进展 未完成 验收 最新 状态`, project=eimemory | evidence_found | derived record above |
| Diagnostic `1.13.9 部署 验收` | evidence_found | derived record above |
| Diagnostic `全局最近任务进展` | evidence_found | original parent |

For all three project-qualified positives, selected fragment is `d2eadc4b33cd46334ad5cd94ff2a17a152db108ae2ae358ec19a56639da1d2b7`, projection span `[0, 457]`. **The complete exact selected excerpt** is saved under each corresponding `queries[].items[0].evidence_excerpt` in `/tmp/memory-core-third-source-replay.json`; it is not a reconstruction from a summary. Its generated contextual assertion is:

> 项目 eimemory：- **部署已完成**：生产为 `1.13.9 / 2f46707`，发布单元已退出，退出码 **0**。

The same selected excerpt also contains this exact source statement:

> **正式业务闭环仍未通过**：报告显示缺少 **5 个合格 Codex 真实用例及对应可信标签**，后续完整验收和观察期未完成。

Replay asserts excerpt membership in the admitted fragment, source/parent/support citations, and preservation of both the blocked-business-acceptance statement and `budget_exhausted` caveat. All fragments of the two newer review-discussion records still fail the four prior admission checks. The phone extraction/recall/excerpt assertions still pass.

The replay exercises the real local RuntimeStore transaction, contextual extraction, authority hydration/digest validation, candidate-source implementation, task routing, deterministic admission, RPC adapters and compact output. Equal cosine 0.9 and lexical-ranked local SQL fragment rows are still simulated. No production embeddings, PostgreSQL index synchronization, live host messages, citation opening or current execution state were verified. A separate synthetic test exercises **Hermes provider → queued write → local RPC bridge → channel-scoped capture → context persistence**, both with durable IDs and with missing IDs. No live host was changed.

### Red/green commands and focused checks

All pytest invocations use the prior environment-scrubbing `/tmp/memory-core-test.py` launcher and `/dev-project/eimemory/.venv/bin/python`, importing this worktree. Commands are prefixed by `rtk proxy`.

- Initial `-q tests/test_same_turn_project_context.py --tb=short`: **1 failed, 16 passed**, valid same-turn association returned no derived record. After contextual implementation: **17 passed**.
- Host integration plus Hermes: **1 failed, 64 passed**, due to base workspace versus channel-qualified binding scope. After reusing `resolve_channel_scope`: **65 passed**.
- `-q tests/test_same_turn_project_context.py -k atomic --tb=short`: **1 failed, 29 deselected**, injected derived-write failure left two raw records. Fixed using `mutate_records_atomically`.
- `-q tests/test_same_turn_project_context.py -k removed_parent --tb=short`: **1 failed, 30 deselected**, removed parent could derive an active record. Fixed using the existing inactive/superseded classifier.
- Final regression command below: **235 passed, 2 deselected in 11.81s**, saved `/tmp/memory-core-third-tests.log`.
- After adding an additional assertion that a different queried project cannot relabel valid persisted evidence, the new focused file alone: **31 passed in 3.63s**. No implementation changed after the 235-test run.
- `rtk proxy /dev-project/eimemory/.venv/bin/python docs/audit/memory-core-v1-source-replay.py`: exit 0 with the positive results above, saved `/tmp/memory-core-third-source-replay.log` and `.json`.

```bash
rtk proxy /dev-project/eimemory/.venv/bin/python /tmp/memory-core-test.py -q \
  tests/test_same_turn_project_context.py tests/test_memory_core_v1_repair.py \
  tests/test_hermes_adapter.py tests/test_runtime_channel_adapter.py \
  tests/test_l1_extract.py tests/test_lightweight_evidence.py \
  tests/test_recall_budget_reserve.py tests/test_models.py \
  tests/test_runtime_adapter_rpc.py \
  --deselect=tests/test_runtime_adapter_rpc.py::test_runtime_http_client_calls_authenticated_rpc \
  --deselect=tests/test_runtime_adapter_rpc.py::test_runtime_http_attestation_requires_separate_producer_credential_and_channel_match \
  --tb=short
```

The two deselections are the previously established unavailable listener tests in this file; no socket restriction was relaxed or retry attempted. Main's prior socket results do not verify this patch. Counts overlap and are not summed. A local self-review was performed under the user's no-other-workers boundary.

### Precise scoped backfill procedure — documented, not executed

1. Use an authorized maintenance identity and the actual captured parent fetched by **exact scope and source partition**, never a search result or every record in the session/channel. Preserve the existing production parent; do not import the redacted replay fixture over it. Verify active status, source event/session, and assistant 64754 exact-content binding again against the authorized original source export. Verify that 64747 is the immediately preceding user boundary and that 64751/64753 precede 64754 within that turn. If parent content differs, resolve the binding discrepancy rather than edit raw history to fit.
2. Supply the scoped export binding with `source_record_id`, `source_event_id`, `scope`, `source_id`, `session_id`, `turn_start_user_message_id`, `bound_assistant_message_id` and the original ordered tool/final message objects. For this export, `source_id` is obtained from the bound parent; no project alias is supplied. Review the deterministic result in a disposable snapshot first using the same `persist_same_turn_context(memory_api, parent, binding)` function exercised by the replay. A valid result appends two raw support records and one derived L0 record; invalid evidence returns `[]`.
3. Only when production backfill is separately authorized, call that function for this one parent on the existing authorized MemoryAPI/store. It uses the existing atomic transaction/outbox, retains the parent's event time and never changes raw content, aliases, extraction markers, ACLs or release gates. Record the returned ID and all provenance. A retry with the same binding must return the same ID without new records. Do not reset global L1 markers or re-extract unrelated sessions.
4. Let the existing index synchronization path process the committed outbox; inspect the three exact source/scope IDs and the derived authoritative projection digest/index readiness through the existing index tooling. No manually fabricated embeddings, query-time aliases or bypass of digest/readiness validation. Production digests must be computed from the real persisted records, not copied from redacted fixture hashes.
5. Re-run the unchanged named release and original inherited-project requests through the authenticated host. Verify parent/tool citations open the same authorized originals, the selected excerpt includes the blocked acceptance status, and current execution state remains explicitly unverified. Check the unrelated-project negative and scope/source denial. Future capture requires the live host to actually pass durable IDs and same-turn tool observations into the updated provider; that live wiring is not established by the synthetic host test or this offline backfill.

There is no remaining need for new project evidence for this exported case. Remaining work outside this authorization is live host integration, scoped production backfill/indexing and production acceptance; none was performed here.


## Fourth pass — credential admission before association persistence

### Security root cause and bounded fix

The independent synthetic probe established that valid source linkage did not make raw tool content safe to copy: `persist_same_turn_context` bypassed the existing intake/knowledge secret predicate and directly entered the atomic record/outbox mutation. The atomic transaction prevented partial writes, but admitted sensitive content as a complete transaction.

This pass reuses `eimemory.intake.loop._looks_like_secret` on the complete selected tool contents and the complete final assistant text, before constructing any support/derived records or entering `mutate_records_atomically`. A positive finding returns an empty result without logging source content. Tool payload claims of safety, scope or permission cannot override this decision. No redacted derivative is created, so accepted source text and digests still describe the exact originals. Ordinary capture and existing user history are not deleted, rewritten or newly filtered by this guard; an already captured parent remains unchanged on rejection.

The new regressions also exposed a shared-policy format gap: the existing patterns did not recognize quoted JSON credential-field names or nested serialized strings. The narrow shared fix decodes JSON string literals into a temporary screening view and reapplies the existing patterns, including when JSON is embedded in prose/terminal output. Unicode escapes in serialized field names are covered. It does not introduce a separate credential vocabulary or change the existing value-length thresholds. Decoding stops when unchanged; excessive serialization beyond 16 changing layers fails closed. This view is never persisted or reported. Existing intake and knowledge safety consumers reuse the same correction.

### Fourth-pass red/green evidence

All cases use explicitly synthetic non-credential values. Test IDs and failure assertions report synthetic labels/booleans, not sensitive values. Added regressions cover Authorization/Bearer, standalone Bearer, API-key/password/token fields, nested serialization and escaped field names in read-file support, terminal support and newly copied final answers. Each rejection asserts no mutation call, no new scoped records, no new outbox rows and an unchanged captured parent. Payload safety/permission/scope assertions are present in the rejected support. Clean support, benign authorization-policy documentation, source/same-turn isolation and idempotency remain covered.

- Red: `rtk proxy /dev-project/eimemory/.venv/bin/python /tmp/memory-core-test.py -q tests/test_same_turn_project_context.py -k 'sensitive_copied or shared_secret' --tb=no` — **26 failed, 2 passed, 32 deselected in 3.82s**, `/tmp/memory-core-fourth-red.log`. All 21 persistence regressions failed; five shared-policy JSON cases failed; the two plain header/bearer predicate cases already passed.
- Green, focused file: **60 passed in 5.33s**, `/tmp/memory-core-fourth-green.log`.
- Final impacted run: `rtk proxy /dev-project/eimemory/.venv/bin/python /tmp/memory-core-test.py -q tests/test_same_turn_project_context.py tests/test_intake_loop_core.py tests/test_promotion_safety.py tests/test_hermes_adapter.py --tb=short` — **114 passed in 8.33s**, `/tmp/memory-core-fourth-tests.log`. Counts overlap and are not summed. The earlier independent 355-test run is third-pass evidence, not verification of this patch. No full suite was run.
- Independent probe rerun: `/tmp/memory-core-fourth-sensitive-probe.py` is the supplied `main-sensitive-probe.py` with only its report destination changed to preserve the original evidence. Exit 0; `/tmp/memory-core-fourth-sensitive-probe.json`: `derived=false`, `synthetic_sensitive_line_persisted=false`.
- Clean original-source replay: `/tmp/memory-core-fourth-source-replay.py` is the existing audit replay with only its report destination changed. Exit 0; `/tmp/memory-core-fourth-source-replay.json` and `.log`. `named_release`, `inherited_project` and `original_inherited_project` all remain positive and select the source-linked record. Existing exact-support-text, original-parent, citation/excerpt and phone assertions pass. No new source excerpts are copied into this audit section.
- `rtk git diff --check` passed. Local review confirmed the guard precedes record construction and the mutation, and the shared predicate emits no source-bearing diagnostics.

### Host durable-ID prerequisite — bounded read-only source evidence

Only relevant source under `/home/darrow/.hermes/hermes-agent/agent/` was read; no host state/database, credentials or live service were accessed, and no host files were edited.

- `memory_provider.py:107–111`: `MemoryProvider.sync_turn(..., session_id=..., messages=...)` documents `messages` as the OpenAI-style list so far. It does not promise numeric `id`, durable commit success, or an immutable same-turn snapshot.
- `memory_manager.py:480–500`: `MemoryManager.sync_all` forwards the supplied `messages` to providers that accept that keyword, through a background closure. It neither resolves durable IDs nor converts `_row_id` to `id`; the shown handoff retains the supplied list reference.
- `turn_finalizer.py:475–486`: the finalizer shapes the transcript and invokes `_persist_session(messages, conversation_history)` inside guarded cleanup. At `turn_finalizer.py:597–600`, it later calls `agent._sync_external_memory_for_turn(..., messages=messages)`. The definition of that helper was not found within the permitted `agent/` source subtree, so this bounded lookup does not prove its exact copying/filtering behavior. Persistence cleanup is guarded; reaching the later call alone is not proof that persistence succeeded.
- `message_metadata.py:29–38`: appending a live message stamps a timestamp, not a durable numeric `id`.
- `session_persistence.py:204–214`: `_db_flush_write` invokes `append_messages_batch` and then `sync_flushed_message_markers`. `transcript_repair.py:79–86` documents and performs post-commit stamping of the persisted marker and canonical `_row_id` onto live dictionaries, optionally replacing content with canonical content. Thus an internal durable-row handoff exists, but its field is `_row_id`, not `id`.
- Worktree `eimemory/adapters/hermes/provider_core.py:373–403` passes the received list into `capture_turn_binding`; it has no durable-row resolver. `eimemory/knowledge/turn_context.py:36–38` requires strictly ordered integer `id` values. Merely receiving the host's internal `_row_id` shape does not satisfy that contract.

Conclusion: the existing documented provider contract does **not** establish verified durable same-turn IDs for this plugin. The internal post-commit `_row_id` mechanism is a concrete possible source for a future explicit host handoff, but the provider still needs a verified successful-persistence, canonical-content, session-bound turn snapshot mapped into the capture contract. This pass does not silently treat timestamps, Python object IDs, platform IDs or message positions as durable IDs, and does not blindly rename `_row_id`. Live host wiring remains unproved and is not deployed or claimed complete.

### Retained limitations and scope

The shared policy remains a deterministic pattern detector, not a guarantee against every possible secret encoding or credential format. Its existing thresholds/vocabulary remain in force. Rejection only prevents this new association from copying sensitive content; it does not sanitize previously stored history or erase earlier records. Clean replay still uses simulated embedding scores/SQL candidates with actual local authority, persistence and RPC code; it is not production verification. Live host integration, separately authorized backfill/indexing and production acceptance remain prerequisites.

Same isolated worktree; no workers, model changes, commits, pushes, deployment, production data reads/writes, or broadened architecture. Only the permitted bounded host source lookup was added. All patch changes remain uncommitted.

## Fifth pass — verified Hermes durable-message handoff (2026-09-10)

This pass adds a working plugin-only path for a successfully persisted, complete current Hermes turn. It supersedes the fourth-pass statement that live host wiring was unproved **only for this locally exercised supported shape**. It does not establish that every new capture can associate, and is not production acceptance.

### Actual host contract and modules

Read-only source inspection in `/home/darrow/.hermes/hermes-agent` traced `run_agent.py:867-894` (`_sync_external_memory_for_turn`) to `agent/memory_manager.py:480` (`sync_all`). The caller passes the same messages object, with session_id; the manager forwards it to the provider on its existing background executor. `agent/memory_provider.py:80` documents profile-scoped `hermes_home`, supplied by `MemoryManager.initialize_all` using `hermes_constants.get_hermes_home`.

Real test imports use `hermes_state.SessionDB`, `agent.session_persistence._db_flush_row` and `_db_flush_write`, and `agent.memory_manager.MemoryManager`. The persistence helper calls the real `SessionDB.append_messages_batch` in `hermes_state_messages.py`, using `agent.transcript_repair.resolve_and_repair_transcript_batch` and `sync_flushed_message_markers`. Successful commit stamps `_row_id` and `_db_persisted` (marker defined in `agent.context_compressor`). `hermes_state_sessions.py` defines profile ownership stamping; `hermes_state_messages.py` defines insertion-order reads and string/content serialization. `hermes_constants` provides current-profile/root resolution. No host file was edited. Tests do not instantiate AIAgent or run an LLM conversation; they exercise the actual persistence and manager/provider modules with synthetic records.

The eimemory interpreter could not import SessionDB because PyYAML was unavailable. Rather than skipping, the integration ran using the existing `/home/darrow/.hermes/hermes-agent/venv/bin/python`, which imports both host and this worktree. `HERMES_SOURCE` selects external source only in tests. Each host fixture sets a temporary HERMES_HOME before host imports and constructs SessionDB with an explicit temporary path. No live SessionDB was opened in this pass.

### Plugin implementation and trust boundaries

Only fifth-pass implementation files are `eimemory/adapters/hermes/durable_handoff.py` (new) and additions to `provider_core.py`; tests are `tests/test_hermes_durable_handoff.py` (new). This audit is the fourth changed file for this pass; prior worktree changes are retained.

At provider entry, select and copy only the latest bounded user turn and evidence fields before the plugin queue. No IDs are written into host API/prompt/cache messages. Require successful-persistence markers, strictly increasing positive integer canonical row IDs, the initialized session, active-profile home equality, and a matching durable session profile stamp. No supplied message can select the database or profile. Open the initialized profile's `state.db` using SQLite `mode=ro`, `query_only`, a short timeout, and one read transaction; no SessionDB constructor/schema initialization is used by the plugin. The public SessionDB reads do not offer exact-ID batch reads with these boundary checks, so the plugin uses a narrow schema-specific read and fails closed on incompatible schemas.

Read message bodies only for the exact supplied IDs in the bound active session. Compare committed content, role, tool name, tool_call_id, full tool_calls and order. Check tool request/result pairing. Read at most 129 ID/role metadata rows starting at the supplied user boundary to require completeness through the current active tail and reject a later user turn. No other session/chat bodies, whole histories, inferred identities, generated message IDs or invented timestamps are used. Canonical `id` exists only in the isolated validated evidence snapshot. Failed durable validation never falls back to the explicit id-shaped export path. The existing authenticated scoped-export contract remains available unchanged.

The existing RPC, source/user/scope authorization, pre-persistence sensitive-data guard and immutable raw-parent association flow remain in place. `supporting_context_status` and debug logging expose only reason codes (validated durable turn, unsupported shape, profile mismatch, unavailable context, etc.), never transcript contents. Unsupported context still permits normal memory capture.

### Fresh evidence and commands

All pytest commands use the existing environment-scrubbing `/tmp/memory-core-test.py` launcher, current worktree imports, and `rtk proxy`. The prefix for host test runs was:

`HERMES_SOURCE=/home/darrow/.hermes/hermes-agent /home/darrow/.hermes/hermes-agent/venv/bin/python /tmp/memory-core-test.py`

- Red: prefix plus `-q tests/test_hermes_durable_handoff.py --tb=short`: **2 failed, 14 passed in 4.79s**, `/tmp/memory-core-fifth-red.log`. Both real-persistence and plugin-queue snapshot positive assertions lacked associations before implementation.
- Initial green: same command: **16 passed in 4.73s**, `/tmp/memory-core-fifth-green.log`. Expanded integration then had **19 passed in 4.84s**, `/tmp/memory-core-fifth-integration.log`; the final run includes the later missing-DB case and final bounded-query edits.
- First affected run, seven files including socket tests: **171 passed, 2 failed in 12.71s**, `/tmp/memory-core-fifth-tests.log`. Both failures were sandbox `PermissionError: [Errno 1] Operation not permitted` at socket creation. Adding the credential review-gap file exposed four additional socket-dependent tests with the same failure (intermediate tool output: 222 passed, 4 failed, 2 deselected). These are environment-blocked, not passing tests; escalation is unavailable under this session's permission policy.
- Final command: prefix plus `-q -s tests/test_hermes_durable_handoff.py tests/test_same_turn_project_context.py tests/test_hermes_adapter.py tests/test_hermes_plugin_package.py tests/test_runtime_adapter_rpc.py tests/test_intake_loop_core.py tests/test_promotion_safety.py tests/test_adapter_receipt_review_gaps.py -k "not test_runtime_http_client_calls_authenticated_rpc and not test_runtime_http_attestation_requires_separate_producer_credential_and_channel_match and not test_rpc_server_attestation_profile_is_private_file_only and not test_codex_post_tool_and_stop_separate_processes_preserve_exact_receipts and not test_hermes_official_terminal_lifecycle_binds_verified_host_turn and not test_status_reports_fail_closed_attestation_profile" --tb=short`: **223 passed, 6 deselected in 17.15s**, `/tmp/memory-core-fifth-final-tests.log`. No full suite was run. The log contains a deliberately exercised Codex-hook failure diagnostic; pytest passed.
- The successful real manager → provider → existing queued write → local EIBrain RPC bridge → authority/association test reports actual committed row IDs `[1, 2, 3, 4, 5]`, evidence IDs `[3, 4, 5]`, and association count `1`. These IDs come from SessionDB commits and are compared to SessionDB's read API, not fixture-assigned IDs. The local RPC dispatch is in-process; a socket transport cannot be exercised in this sandbox.
- Negatives cover a real failed batch transaction with rollback/no markers, false marker, missing/wrong IDs, wrong session, wrong profile stamp/current home, changed content, role/call identity changes, missing rows/boundary, bad ordering, a later persisted turn, missing DB, and asynchronous list mutation. Named-profile persistence also passes. Successful capture preserves the host message list and nested tool-call dictionaries; post-provider-entry mutation cannot change queued evidence.
- Safe replay: `rtk proxy /dev-project/eimemory/.venv/bin/python /tmp/memory-core-fifth-source-replay.py`, exit 0. The script differs from fourth pass only in report destination. `/tmp/memory-core-fifth-source-replay.json` and `.log`: `named_release`, `inherited_project`, and `original_inherited_project` remain `evidence_found`; existing source/citation/immutability assertions pass. Simulated retrieval scores remain a limitation.
- Sensitive probe: `rtk proxy /dev-project/eimemory/.venv/bin/python /tmp/memory-core-fifth-sensitive-probe.py`, exit 0; report destination only changed from fourth pass. `/tmp/memory-core-fifth-sensitive-probe.json`: `derived=false`, `synthetic_sensitive_line_persisted=false`.

### Remaining host boundary and separate proposal

**Not every new capture has a verified association path.** The complete, plain-text, current-tail persistence handoff now does, without Hermes core changes. Multimodal/rewritten API content, missing legacy profile stamps, compressed or partial turns, unavailable DB/context, and delayed turns that are no longer the active tail remain safely unsupported. Ordinary capture continues.

Executable negative `test_host_manager_does_not_snapshot_before_its_queue` calls the real `MemoryManager.sync_all`, holds its submitted callback, clears the supplied list, then executes the callback. The real manager hands the plugin the cleared list; normal capture succeeds without supporting evidence. The plugin cannot snapshot before it is invoked and cannot safely recover the lost boundary by matching question text.

Separate minimal host proposal, **not implemented here**: snapshot bounded current-turn evidence synchronously in `MemoryManager.sync_all` before `_submit_background`, retaining committed `_row_id`/marker and the host session/profile binding without modifying the API transcript. Supporting delayed historical tails additionally needs an explicit host-bound completed-turn boundary contract; this plugin intentionally does not relax its current-tail check. No monkeypatch or separate-repo edit was shipped to bypass this boundary.

No commits, push, deployment, production writes/restarts/backfill, credential changes, new scheduler/worker/config defaults, or model/effort changes. No production acceptance or historical backfill is claimed.


## Host snapshot follow-up — 2026-09-10

The separate host proposal above is now implemented in the authorized isolated `/home/darrow/tmp/hermes-memory-sync-snapshot` worktree. This supersedes the mutable-manager-list blocker for opted-in providers. `HermesMemoryProviderCore.sync_turn_snapshot_version = 1` requests the concrete immutable host `CompletedTurnSnapshot` created before manager enqueue. Its session/current-home/payload must match durable validation. Historical completion is allowed only with that host provenance, verified exact active DB rows/content/tool calls and a next active user boundary; arbitrary flags/plain lists do not enable the allowance. This is trusted in-process host provenance, not protection against malicious Python plugins. Existing ACL/source/RPC and sensitive-data checks remain unchanged.

The former negative host-manager test now asserts positive evidence after list clearing, nested mutation and a later persisted user. Two queued complete turns retain their distinct committed IDs. Expanded host-to-local-RPC negatives and sensitive-support rejection pass. Exact commands, red/green results, files and limitations are in `/home/darrow/tmp/hermes-memory-sync-snapshot/docs/memory-sync-snapshot-audit.md`: 128 affected integration tests plus one subsequently added two-turn test passed; focused host evidence is 73 existing provider tests and six snapshot/interrupt tests. No actual host import was skipped. No production verification, commit, push, deploy, restart, credentials or production writes by this worker. Main owns review and deployment acceptance.
