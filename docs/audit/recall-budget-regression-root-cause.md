# Recall budget regression: root cause and local repair

Date: 2026-09-09. Worktree: `/home/darrow/tmp/eimemory-recall-budget-fix`.
Baseline: `e4ad72b53795ca396f9731e03e722e111784b718` (1.13.5).
The recall implementation changes are in `47acd6a`; `e4ad72b` only adjusts
Hermes manifest versions. Main-thread readback verified production rollback to `a379e22`.

## Main-thread independent verification (1.13.6 candidate)

- Independent rerun including both previously sandbox-blocked HTTP tests, task evidence scan tests and plugin packaging: **210 passed in 15.87s**. No tests deselected in this rerun.
- Added short-circuit task evidence field scanning: a matching summary no longer causes repeated full-transcript regex scans. Detail-only state evidence remains supported; two tests cover this equivalence and bounded work.
- Real-data replay uses a consistent read-only SQLite backup and copied payload segments under `/home/darrow/tmp/eimemory-budget-snapshot`, the production Python dependency environment, unchanged production retrieval configuration, and PostgreSQL `default_transaction_read_only=on`. No Runtime was opened on production storage.
- Final saved replay `/home/darrow/tmp/eimemory-isolated-fixed-final.json`: four queries, three sequential rounds, **12 evidence_found with nonempty relevant records**. These cover native-parameter recent task L1, task-history L0, Fujian supply facts, and named eimemory deployment task evidence. Reviewed task results are historical conversations, not an assertion that the most recent operational state is fully reconstructed.
- Intermediate named-query cold runs still encountered legitimate collection/admission deadlines; these are retained in `/home/darrow/tmp/eimemory-isolated-fixed-phases.json` and other replay artifacts. Final sample success is not a guarantee of every cold/slow-backend query or L5. No time budget or authority gate was relaxed.
- Current candidate version is 1.13.6 across pyproject, Python version and both Hermes manifests. Production deployment/acceptance is separate and remains pending at this document's commit.


## Evidence and causal chain

The production symptom supplied with the task was six unavailable native calls,
lasting 2556, 1343, 1126, 1290, 1175 and 1085 ms, with source health
`available=false`, `circuit=half_open`, `last_error=recall_budget_exhausted`.
Those observations alone do not establish where the budget was consumed.

The main thread subsequently supplied `MAIN-REPLAY-EVIDENCE.txt` and
`/home/darrow/tmp/eimemory-isolated-bad-production-deps.json`. I read the saved
JSON, without running the replay or opening its SQLite snapshot. Its first case
provides direct evidence for the cross-scope defect:

| Stage | Actual saved replay evidence |
| --- | --- |
| First populated scope | 1978.60 ms, PostgreSQL `state=available`, 48 candidates; index verified, query valid |
| Empty scope | 0.29 ms, `sqlite_authority`; no remote work |
| Later populated scope | 276.51 ms, `recall_budget_exhausted`; index still verified, query validity cleared |
| Admission | `fragment_index_unavailable`, final `unavailable`, total 2645.82 ms |

Both circuits were **closed** in that saved failing case. Thus circuit opening
is not required for this production-snapshot failure. The main thread also
reports successful subsequent Fujian queries in that replay. This does not
reproduce every original production failure or prove its half-open history.

`GovernedRecallEngine` gives scope searches one collection deadline, preserving
part of the final request budget for hydration and admission. On a later scope's
budget exception, `PostgresVectorCandidateSource.search` clears global
`_last_query_valid`. Admission previously consulted that global last-search
state, discarding fragments that an earlier scope in the **same request** had
already retrieved and verified. Two deterministic integration reproductions
(project fact and task conversation) fail on baseline and pass with the repair.
The control cases with no timeout pass on baseline; index-watermark and authority
failures continue to return no results after the repair.

Three additional defects in the same chain are independently reproduced:

1. **Inner embedding circuit poisoning.** The source classifies a request
   timeout as budget exhaustion, but the real `OpenAICompatibleEmbeddingProvider`
   used to increment its own circuit on every timeout. Three 0.2-second caller
   timeouts open that circuit despite a configured 1-second service timeout.
   Later requests then fail without contacting the embedding transport. The
   regression checks also prove that actual configured service timeouts still
   open the circuit. A six-call synthetic sequence with three shortened-budget
   timeouts followed by three successful responses fails on baseline and
   recovers with the fix. This sequence establishes the mechanism, not the exact
   timing or transport history of the six production calls.
2. **Stranded half-open source probe.** After a real circuit failure and cooldown,
   `allow()` claims the probe. Budget exhaustion neither recorded a failure nor
   released the claim. Health reports `half_open`, but subsequent `allow()` calls
   remain false indefinitely. The reproduction preserves the failure count and
   asserts that another probe becomes eligible after cancellation.
3. **Timeout followed by blocking cleanup.** `pending.result(timeout=...)` was
   inside a `ThreadPoolExecutor` context, whose exit joins the worker. A synthetic
   80 ms cutoff returned after 1002.96 ms with a one-second embedding. The
   non-fragment synchronous branch also returned after approximately 1001 ms.
   Both now return within the test's 400 ms scheduling tolerance, retaining the
   worker's bounded gate until it finishes. The test releases its worker in
   `finally` and verifies that no repository query ran after expiration.

## Repair and safeguards

- Embedding timeouts caused by a caller limit shorter than the configured
  transport limit are inconclusive circuit probes. They propagate
  `recall_budget_exhausted`; other errors and full service timeouts still count.
  Cancellation releases a half-open probe without resetting its failure history.
- Deadline-bearing embedding calls use a bounded worker. Timeout cleanup does
  not join it. A completion callback releases its slot; SQLite, repository
  search, source-cache updates and result publication remain on the caller
  thread. Calls with no deadline retain the existing join behavior. Python
  cannot forcibly stop an arbitrary injected provider: an uncooperative worker
  can retain a slot until it exits, but cannot cause unbounded worker creation.
- Admission receives this recall's source reports. A successful report must
  match the current committed watermark and authority revision, and the index
  must remain verified. Only a subsequent scheduling-budget cancellation can
  preserve that earlier availability; index or authority failures do not.
  Missing/invalid fragments, stale digests, unauthorized references, inactive
  records and final-deadline failures remain rejected. No SQLite-only semantic
  fallback was enabled.
- Task evidence filtering now reuses `PERSONA_TYPES` from the existing loadout.
  `operator_preference`, `instruction`, `persona` and `user_profile` previously
  survived task-history filtering when their text contained an agreement cue.
  Four failing reproductions now exclude them while keeping the actual task.
  Existing audit/diagnostic/partition restrictions remain in place.

## Downstream and budget checks

The tests exercise real SQLite authority, the PostgreSQL candidate source,
fragment reconstruction/digests, engine filtering, lightweight admission and
native Hermes RPC/loadout. Only transport responses and repository rows are
synthetic. Native L1 task status and L0 task-history queries return the intended
conversation with lightweight admission both disabled and enabled. This does
not establish generic legacy L0 behavior against production data.

All scopes retain the same absolute collection cutoff: the deterministic test
starts at 10.0, has a 13.0 final deadline and a 12.25 collection cutoff; the later
scope fails at 12.3, leaving time for required validation. No deadline or reserve
was increased. The source's injected clock controls circuit/cache age; scheduling
uses `monotonic`, while the engine/SQLite/admission use `perf_counter`. On the
specified local CPython 3.14 interpreter, both report
`clock_gettime(CLOCK_MONOTONIC)`, nonadjustable, resolution `1e-09`. An epoch
mismatch is not evidenced here; deterministic tests patch both clock domains.

Candidate-result cache identity already excluded the absolute collection cutoff
in 1.13.5. A new test verifies reuse with a new cutoff, two index-state reads on
each cached search, and a cache miss for another exact scope. Existing tests
cover source, query, release/policy, result-limit and top-K partitioning. Repeated
embedding across distinct scopes can still consume the shared budget; no unsafe
cross-scope candidate cache or additional embedding cache was introduced.

## Reproduction commands and results

All local tests used `/dev-project/eimemory/.venv/bin/python`, removed inherited
`EIMEMORY_*` variables, and set `PYTHONPATH` to this worktree. No test instantiated
Runtime against `/var/lib/eimemory`; all Runtime/SQLite writes used pytest
temporary directories. The local interpreter is not the production PostgreSQL
dependency baseline. The main thread explicitly identified an earlier replay
without the production psycopg dependency as invalid evidence, not a root cause.

The local runner `/tmp/eimemory-local-tests.py` is reproducible with:

```python
import os, sys
for key in list(os.environ):
    if key.startswith('EIMEMORY_'):
        del os.environ[key]
os.environ['PYTHONPATH'] = '/home/darrow/tmp/eimemory-recall-budget-fix'
os.chdir(os.environ['PYTHONPATH'])
os.execv(sys.executable, [sys.executable, '-m', 'pytest', *sys.argv[1:]])
```

Commands (shell invocations were prefixed with `rtk proxy`):

```bash
/dev-project/eimemory/.venv/bin/python /tmp/eimemory-local-tests.py -q \
  tests/test_recall_budget_reserve.py tests/test_task_recall_repair.py
# Original tests before adding reproductions: 19 passed in 2.76s.

/dev-project/eimemory/.venv/bin/python /tmp/eimemory-local-tests.py -q \
  tests/test_recall_budget_reserve.py tests/test_task_recall_repair.py \
  tests/test_postgres_vector_source.py tests/test_lightweight_evidence.py \
  tests/test_recall_local_work.py tests/test_recall_engine.py \
  tests/test_recall_intent.py tests/test_recall_views.py \
  -k 'not does_not_forward_bearer_across_redirects and not enforces_total_deadline_on_slow_drip' \
  --tb=short
```

Final focused result after the complete code change: **200 passed, 2 deselected
in 13.25s**. The deselections are the two sandbox-blocked HTTP tests described
below, not missing test failures silently converted to skips.

Before repairs, the first circuit/worker reproduction run was **3 failed,
1 passed**. After completing the reproductions, a baseline differential run
using the three original production-code modules from `git show HEAD:<path>`
was **11 failed, 7 passed, 71 deselected in 5.04s**:

```bash
/dev-project/eimemory/.venv/bin/python /tmp/eimemory-baseline-tests.py -q \
  tests/test_recall_budget_reserve.py tests/test_postgres_vector_source.py \
  tests/test_task_recall_repair.py \
  -k 'verified_fragments or request_budget_does_not_poison or exhausted_half_open or timeout_returns_without_join or six_sequential or persona_variants' \
  --tb=no
```

`/tmp/eimemory-baseline-tests.py` keeps the same clean environment and worktree
imports, but installs an import finder mapping only `eimemory.retrieval.engine`,
`eimemory.retrieval.postgres_vector` and `eimemory.recall.task_queries` to their
`git show HEAD` copies in `/tmp`. It does not replace any worktree files. The
eleven failures are the two cross-scope budget cases, inner circuit, half-open
probe, both worker branches, six-call recovery, and four persona types.

The broader run including `tests/test_postgres_runtime_config.py` produced
**205 passed, 3 failed, 2 deselected in 14.87s**. The three failures were verified
against the original modules: **5 passed, 3 failed in 2.46s**, with identical
assertions. All involve empty authority partitions taking the existing
`sqlite_authority` shortcut instead of the tests' expected remote bypass/query
state. They are unchanged baseline failures, not repaired by altering config or
the authority shortcut.

Two existing tests attempted to create localhost HTTP servers and failed with
`PermissionError: [Errno 1] Operation not permitted` in this sandbox:
`test_openai_embedding_does_not_forward_bearer_across_redirects` and
`test_openai_embedding_enforces_total_deadline_on_slow_drip`. They were explicitly
deselected after that observed failure, not reported as passing. No live
PostgreSQL lifecycle test was run. `git diff --check` passed.

## Remaining acceptance

The main thread owns final review, production-dependency snapshot replay,
real-data task/Fujian/L0 acceptance and any eventual deployment. The saved
baseline snapshot confirms the cross-scope cause, but does not prove that inner
circuit poisoning or executor joining occurred during all original six calls.
Production half-open history, exact latency contributions, and generic legacy
L0 remain unproven here. No production acceptance is claimed.

No commit, push, deploy, restart, version change, production/config write or
formal-gate change was performed. `MAIN-REPLAY-EVIDENCE.txt` was supplied by the
main thread and left untouched.
