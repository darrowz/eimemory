# Protected Code Evolution Effect Owner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Apply superpowers:test-driven-development for every behavior change and superpowers:verification-before-completion before claiming success.

**Goal:** Replace the unconditional `effect_executor_unavailable` abort with a protected, restart-safe effect owner that can materialize a registered proposal, run immutable test plans, consume one-shot policy, commit/push/deploy exact bytes, verify health, observe for 48 hours, and roll back on failure.

**Architecture:** The transaction ledger remains the source of truth. A new effect owner executes only typed, code-owned operations and records intent before every external effect. Repository/deployment boundaries are injected for isolated tests but production construction accepts no model-provided commands, environment, secrets, or arbitrary paths. Existing promotion orchestration calls the owner only when both `apply` and machine policy enable effects; the production kill switch remains fail-closed until a genuine qualifying system incident exists.

**Tech Stack:** Python 3.11+, subprocess argv without shell, Git detached worktrees, existing code-evolution CAS store/policy/test plans/deployment receipts, pytest.

---

## Task 1: Specify protected effect boundaries

**Files:**
- Create: `eimemory/governance/code_evolution_effects.py`
- Create: `tests/test_code_evolution_effects.py`

1. Write failing tests for repository root/ref mismatch, unsupported file path, duplicate file update, absolute/traversal path, symlink escape, base commit/tree mismatch, unregistered plan, and proposal-carried command/env/secret fields.
2. Define immutable internal value objects for a materialized candidate and command result, plus a narrow effect adapter protocol used only for tests/recovery.
3. Implement production path validation from transaction/provider/test-plan coordinates. Normalize with POSIX relative paths, require exact membership in the registered plan, and reject symlinks or escapes before writing.
4. Keep production argv constructors private and fixed. Use `subprocess.run(argv, cwd=..., shell=False, env=minimal_allowlist, check=False)`; never interpolate a shell string.
5. Run `pytest -q tests/test_code_evolution_effects.py` until the boundary tests pass.

## Task 2: Materialize candidate and run protected verification

**Files:**
- Modify: `eimemory/governance/code_evolution_effects.py`
- Modify: `tests/test_code_evolution_effects.py`

1. Add an isolated-repository success test whose strict v2 proposal updates only the registered file. Assert a detached worktree is created from the exact base commit, the resulting tree digest is stored, and the source checkout remains untouched.
2. Add failure tests for patch application errors and for each protected test phase. Assert terminal failure records the failed phase/digest and leaves no false verification receipt.
3. Implement lease acquisition, candidate materialization, and `CANDIDATE_MATERIALIZED` transition.
4. Resolve the candidate interpreter from the trusted environment, construct all argv through `build_test_plan_argv`, and run focused, regression, and full-suite phases in order.
5. Hash phase, exact argv, exit code, and bounded output digest into existing verification receipts; transition through `FOCUSED_VERIFIED`, `REGRESSION_VERIFIED`, and `FULL_SUITE_VERIFIED` only after exit code zero.
6. Ensure temporary worktree cleanup is idempotent and recovery can rediscover a candidate by transaction ID.

## Task 3: Consume one-shot policy and own Git effects

**Files:**
- Modify: `eimemory/governance/code_evolution_effects.py`
- Modify: `tests/test_code_evolution_effects.py`
- Modify: `tests/test_code_automation_policy_v2.py`

1. Add tests proving no commit occurs before all three receipts and policy consumption, a reused policy fails, and policy coordinates must exactly match transaction/root/origin/base commit/tree.
2. Add crash-recovery tests at commit intent and push intent. Reconciliation must detect an already-created exact commit/push and advance once without duplication.
3. Consume policy with `consume_code_automation_policy()` only after verification receipts. Store policy and authorization digests, then transition to `POLICY_AUTHORIZED`.
4. Before committing, append `COMMIT_INTENT` with expected parent/tree/message digest. Create one commit containing only allowed files and a transaction trailer; verify parent and tree before `COMMITTED`.
5. Before pushing, append `PUSH_INTENT` with expected remote ref and old/new commit. Use a force-with-lease/CAS equivalent against the policy-authorized old commit; verify remote exactness before `PUSHED`.

## Task 4: Own immutable deployment, health verification, and rollback

**Files:**
- Modify: `eimemory/governance/code_evolution_effects.py`
- Modify: `tests/test_code_evolution_effects.py`
- Modify: `eimemory/governance/promotion_watch.py`
- Modify: `tests/test_code_evolution_recovery.py`

1. Add a fake-deployment success test asserting the immutable installer receives the exact pushed commit and returns a matching deployment receipt/release identity.
2. Add failure/crash tests for deploy intent, receipt mismatch, health failure, and rollback. Assert rollback targets the recorded prior release and terminal receipts distinguish forward failure from rollback failure.
3. Record `DEPLOY_INTENT` before invoking the fixed immutable installer argv. Verify deployment receipt identity, advertised commit/version, and release health before `DEPLOYED_VERIFIED` then `HEALTHY`.
4. Enter `OBSERVING` with the current incident measurement and the existing 48-hour schedule. Wire a trusted observation sampler so nightly resume supplies real deployment/health/incident evidence to `observe_code_evolution_transaction()`.
5. On regression, transition through rollback intent, deploy the exact prior release, verify restored health, and sediment a rollback terminal receipt. Never silently mark success.

## Task 5: Route proposals through the effect owner

**Files:**
- Modify: `eimemory/governance/code_evolution_transaction.py`
- Modify: `eimemory/governance/promotion_manager.py`
- Modify: `tests/test_code_evolution_transactions.py`

1. Preserve failing tests proving `apply=false`, disabled effects, nonqualifying/user-reported/manual-bootstrap proposals, and a missing/invalid policy remain no-effect terminal outcomes.
2. Add a failing qualifying-path test that injects the isolated effect adapter and expects progression beyond `PATCH_VALIDATED` instead of `effect_executor_unavailable`.
3. Replace the unconditional abort with the effect-owner call only when `apply`, `effects_enabled`, and production eligibility are all true.
4. Keep promotion manager as orchestration authority: it derives `effects_enabled` solely from machine policy flags and never forwards execution fields from the candidate.
5. Return a stable result with transaction ID, state, applied flag, blocked reason, and receipt coordinates.

## Task 6: Verify mechanism without fabricating L5 evidence

1. Run:
   ```bash
   python -m pytest -q \
     tests/test_code_evolution_effects.py \
     tests/test_code_evolution_transactions.py \
     tests/test_code_automation_policy_v2.py \
     tests/test_code_evolution_recovery.py \
     tests/test_promotion_watch.py
   ```
2. Run a disposable-repository rehearsal covering success, crash reconciliation, health regression, and rollback. Delete the disposable repository/database afterward.
3. Assert the production kill switch/policy remains absent or disabled and no synthetic transaction is counted as qualifying L5 evidence.
4. Run `python -m pytest -q --strict-markers tests`, `git diff --check`, and a final security-focused diff review before commit.
5. Document the remaining evidence boundary: mechanism complete now; L5 success can be sedimented only after a later genuine unknown system-detected incident, exact release, and real 48-hour observation.

