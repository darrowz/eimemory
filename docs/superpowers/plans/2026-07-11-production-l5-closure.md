# Production L5 Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the production Hongtu scope reach evidence-bound L5 through structured, source-backed capability probes, strict replay, verified deployment, and complete assessment.

**Architecture:** Outcome traces gain an optional strict capability contract. A non-destructive acceptance runner creates immutable probe records and linked verified traces for the twelve weak-capability cases; replay consumes only those contracts. Patch metrics measure latest executed deployment attempts, and L5 assessment imports executed rollback evidence before final readiness is evaluated.

**Tech Stack:** Python 3.11+, pytest, existing RuntimeStore/SQLite records, OpenClaw hooks, governance replay/readiness, Bash immutable deployment.

## Global Constraints

- Do not accept `not_run`, text-only matches, status-only deployment, or a boolean without source-backed checks as L5 evidence.
- Probe evidence is `rehearsal=true`, performs no external side effect, and never enters production task-success metrics.
- Preserve `outcome_trace.v1`; the nested contract schema is `capability_contract.v1`.
- Every accepted contract references a retrievable source record in the same scope.
- Readiness remains fail-closed and must report `current_stage=L5` and `readiness_score=1.0` before completion.
- Do not run the full test suite; use associated layered suites, compileall, AST, and diff checks.

---

### Task 1: Structured capability contract and OpenClaw producer repair

**Files:**
- Create: `eimemory/experience/capability_contract.py`
- Modify: `eimemory/experience/outcome.py`
- Modify: `eimemory/adapters/openclaw/hooks.py`
- Test: `tests/test_experience_outcome.py`
- Test: `tests/test_openclaw_outcome_hooks.py`

**Interfaces:**
- Produces: `normalize_capability_contract(value) -> dict[str, Any]`
- Produces: `validate_capability_contract(contract, *, expected_capability="", expected_case_id="") -> str`
- Produces: `contract_source_ids(contract) -> list[str]`
- Outcome traces hoist `capability`, `capability_case_id`, and `contract_verified` metadata.

- [ ] **Step 1: Write failing contract tests**

Add tests proving a valid contract is persisted and hoisted, while an unknown
case, failed check, capability/case mismatch, or missing same-scope source ID
returns `ok=false`. Use a real source record created in the temporary Runtime.

- [ ] **Step 2: Run the new outcome tests and verify RED**

Run: `python -m pytest tests/test_experience_outcome.py -q`

Expected: failures because capability-contract validation and metadata do not
exist.

- [ ] **Step 3: Implement the contract module and outcome validation**

Define the twelve case-to-capability mappings and exact observation validators.
`record_outcome_trace` must validate the normalized contract, resolve every
source ID with `runtime.store.get_by_id(..., scope=scope_ref)`, reject failed or
missing evidence, persist the contract inside `payload`, and hoist indexed
metadata.

- [ ] **Step 4: Write failing OpenClaw hook tests**

Assert `_outcome_trace_payload` returns dictionary-shaped `outcome` and
`verifier`, preserves an explicit contract from event/task context, and does
not synthesize a contract for generic terminal text.

- [ ] **Step 5: Run hook tests and verify RED**

Run: `python -m pytest tests/test_openclaw_outcome_hooks.py -q`

Expected: failures showing the current string-shaped fields.

- [ ] **Step 6: Repair the hook producer**

Store:

```python
"outcome": {"status": status, "success": success, "rehearsal": rehearsal},
"verifier": {
    "passed": passed,
    "method": f"openclaw.{end_kind}",
    "evidence_refs": [recorded_event_id],
    "checks": {"verification": verification, "result": result},
},
```

Pass through only an explicit dictionary `capability_contract` from event,
outcome, or task context.

- [ ] **Step 7: Verify Task 1 GREEN and commit**

Run: `python -m pytest tests/test_experience_outcome.py tests/test_openclaw_outcome_hooks.py -q`

Commit: `fix(experience): preserve structured capability evidence`

---

### Task 2: Non-destructive capability acceptance probes

**Files:**
- Create: `eimemory/governance/capability_acceptance.py`
- Modify: `eimemory/api/runtime.py`
- Modify: `eimemory/cli/main.py`
- Test: `tests/test_capability_acceptance.py`

**Interfaces:**
- Produces: `run_capability_acceptance(runtime, *, scope=None, persist=True, execution_id="") -> dict[str, Any]`
- Runtime method: `run_capability_acceptance(...)`
- CLI: `eimemory learn capability-acceptance --json`

- [ ] **Step 1: Write failing acceptance tests**

Cover all twelve cases, distinct probe and trace source IDs, failed validator
behavior, fresh execution IDs, `rehearsal=true`, and no event/task-success
records.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/test_capability_acceptance.py -q`

Expected: import or API failures because the runner is absent.

- [ ] **Step 3: Implement probe execution**

For each case, evaluate the canonical safe artifact with the Task 1 validator,
persist one `capability_probe_result` record containing input, checks,
observation, digest, and execution ID, then record a linked outcome trace with
structured verifier and contract. A failed probe must persist its failure but
must not emit a successful trace.

- [ ] **Step 4: Add Runtime and CLI surfaces**

The CLI always persists, prints JSON, and exits nonzero unless all twelve
probes pass with distinct sources.

- [ ] **Step 5: Verify Task 2 GREEN and commit**

Run: `python -m pytest tests/test_capability_acceptance.py tests/test_experience_outcome.py -q`

Commit: `feat(governance): add capability acceptance probes`

---

### Task 3: Contract-only L5 attribution and replay

**Files:**
- Modify: `eimemory/governance/capability_attribution.py`
- Modify: `eimemory/governance/capability_replay_executor.py`
- Modify: `eimemory/governance/capability_replay_packs.py`
- Test: `tests/test_capability_attribution.py`
- Test: `tests/test_capability_replay_packs.py`

**Interfaces:**
- Attribution evidence includes `contract_verified`, `case_id`, and
  `source_record_ids`.
- Replay results include the trace ID, probe source ID, contract schema, and an
  immutable observation.

- [ ] **Step 1: Write failing strict-attribution tests**

Prove explicit contracts attribute exactly one capability/case and legacy
keyword traces remain visible only as `contract_verified=false`.

- [ ] **Step 2: Write failing strict-replay tests**

Prove text-only, wrong-case, missing-source, failed-verifier, reused-source, and
generic success traces return `not_run` or `fail`; twelve acceptance traces
return twelve passes with distinct evidence.

- [ ] **Step 3: Run tests and verify RED**

Run: `python -m pytest tests/test_capability_attribution.py tests/test_capability_replay_packs.py -q`

- [ ] **Step 4: Implement contract-first attribution and executor**

Use indexed outcome-trace lookup, prefer explicit contracts, remove keyword
matching from the trusted replay path, verify both trace and probe records in
the same scope, and preserve legacy attribution only for diagnostics.

- [ ] **Step 5: Verify Task 3 GREEN and commit**

Run: `python -m pytest tests/test_capability_attribution.py tests/test_capability_replay_packs.py tests/test_l5_readiness.py -q`

Commit: `fix(governance): require contract-backed capability replay`

---

### Task 4: Deployment metrics, rollback assessment, and closure orchestration

**Files:**
- Create: `eimemory/governance/deployment_receipt.py`
- Modify: `eimemory/governance/capability_dashboard.py`
- Modify: `eimemory/governance/l5_loop.py`
- Modify: `eimemory/governance/closure_rehearsal.py`
- Modify: `eimemory/api/runtime.py`
- Modify: `eimemory/cli/main.py`
- Test: `tests/test_capability_dashboard_metrics.py`
- Test: `tests/test_l5_closed_loop.py`
- Test: `tests/test_l5_closure_rehearsal.py`
- Test: `tests/test_deployment_receipt.py`

**Interfaces:**
- Produces: `verify_and_record_deployment(runtime, *, scope, repo_root, current_link, health_url, prior_commit="") -> dict[str, Any]`
- CLI: `eimemory learn deployment-receipt --repo-root ... --current-link ... --health-url ... --prior-commit ... --json`
- Dashboard reports `patch_candidate_validity_rate`,
  `patch_deployment_success_rate`, and backward-compatible patch aliases.

- [ ] **Step 1: Write failing dashboard and receipt tests**

Verify preflight-invalid candidates are visible in candidate validity but not
deployment denominator; retries collapse to latest per candidate; one complete
executed deployment is sufficient quality; status-only or mismatched
commit/version/health is rejected.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/test_capability_dashboard_metrics.py tests/test_deployment_receipt.py -q`

- [ ] **Step 3: Implement deployment receipt and latest metrics**

The receipt function reads repo HEAD, resolves the current symlink, fetches the
health JSON, verifies exact commit/version/release identity, confirms a prior
rollback commit, and persists a promotion request shaped like a fully executed
code-patch deployment. It cannot accept caller-supplied `ok=true` as proof.

- [ ] **Step 4: Write failing rollback and closure tests**

Prove L5 assessment imports an executed policy/lifecycle rollback reference,
closure runs acceptance before replay, and closure success additionally
requires complete assessment plus readiness L5.

- [ ] **Step 5: Run tests and verify RED**

Run: `python -m pytest tests/test_l5_closed_loop.py tests/test_l5_closure_rehearsal.py -q`

- [ ] **Step 6: Implement rollback import and closure sequencing**

Add executed ledger refs to the L5 report before assessment. Run acceptance,
replay, skill/rollback rehearsal, L5 observation assessment, dashboard, then
readiness. Stop immediately at a failed gate and do not create downstream
success records.

- [ ] **Step 7: Verify Task 4 GREEN and commit**

Run: `python -m pytest tests/test_capability_dashboard_metrics.py tests/test_deployment_receipt.py tests/test_l5_closed_loop.py tests/test_l5_closure_rehearsal.py tests/test_l5_readiness.py -q`

Commit: `fix(governance): close deployment and L5 assessment evidence`

---

### Task 5: Release, autonomous patch proof, and production L5 verification

**Files:**
- Modify: `pyproject.toml`
- Modify: `eimemory/version.py`
- Modify: `docs/l5-roadmap-spec.md`
- Modify: `docs/audits/2026-07-11-1.9.10-1.9.14-l5-audit.md`
- Create remotely through the governed patch: `docs/audits/production-l5-autonomous-proof.json`

**Interfaces:**
- First implementation release is `1.9.17`; the governed autonomous proof
  advances both version files together to final version `1.9.18`.
- Production acceptance is the official `hongtu/embodied/darrow` scope.

- [ ] **Step 1: Update docs and first patch version**

Document the contract, probe/rehearsal distinction, executed deployment metric,
and exact L5 gates. Update both version files identically to `1.9.17`.

- [ ] **Step 2: Run fresh local layered verification**

Run associated tests from Tasks 1-4 plus contract tests, `compileall`, AST parse,
version consistency, secret diff scan, and `git diff --check`. Do not run the
full suite.

- [ ] **Step 3: Request whole-branch review and fix findings**

Review the complete branch against the design and plan. Resolve all Critical
and Important findings and rerun covering tests.

- [ ] **Step 4: Commit, fast-forward master, and push**

Commit: `fix(governance): complete production L5 evidence loop`

Push the exact commit to GitHub and synchronize `/dev-project/eimemory`.

- [ ] **Step 5: Remote layered verification and first immutable deploy**

Run the same associated suites using `/dev-project/eimemory/.venv`, install the
full commit with `deploy/install_immutable_release.sh`, restart services, and
verify RPC/gateway/proxy health and commit/version identity.

- [ ] **Step 6: Exercise one real governed autonomous patch**

Create a code-patch candidate through `create_sandbox_experiment` and
`distill_capability_candidate`. Its patch updates both version files to
`1.9.18` and creates the proof JSON. It must use the real promotion manager
with verification commands, commit, immutable deployment command, post-deploy
health command, and rollback commands. Require `production_applied=true`,
successful health, commit identity, rollback evidence, and a promoted candidate.

- [ ] **Step 7: Push and synchronize the governed commit**

Push the governed commit, fetch it locally, and verify local, GitHub, remote
repo, current symlink, version files, and health all identify the same commit.

- [ ] **Step 8: Record deployment receipt and run production closure**

Record the verified current deployment receipt, run
`eimemory learn closure-rehearsal --json`, then run
`eimemory learn l5-readiness --limit 500 --json`.

- [ ] **Step 9: Require final production evidence**

Completion requires:

- twelve latest weak-capability replay passes with distinct source IDs;
- overall replay count at least ten and pass rate at least `0.8`;
- complete trusted latest L5 assessment with zero missing evidence;
- executed patch deployment success rate at least `0.8` with sufficient quality;
- at least one executed rollback/quarantine reference;
- `current_stage=L5` and `readiness_score=1.0`;
- active services and live health endpoints.
