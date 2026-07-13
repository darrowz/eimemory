# OpenClaw Operational Closure Repair Plan

## Task 1: Bound Watchdog Evidence

- Add failing tests in `scripts/test_openclaw_loop.py` proving watchdog records
  contain aggregate stale reasons and bounded samples rather than complete tasks.
- Update `eimemory/ops/openclaw_loop.py` to build the bounded summary once and use
  it in watch, verification, lesson, and report records.
- Verify the focused loop test file.

## Task 2: Close Lifecycle Correlation

- Add a failing Node bridge integration test in `tests/test_platform.py` that
  executes `before_prompt_build` followed by `agent_end` for the same session and
  asserts the original task becomes terminal with no orphaned running task.
- Add a bounded pending-task registry to
  `integrations/openclaw/eimemory-bridge/index.js` and inject the task ID into
  terminal hook context.
- Verify bridge and outcome-hook tests.

## Task 3: Reconcile And Compact Safely

- Add failing tests for dry-run/apply stale reconciliation and atomic compaction
  with a gzip archive.
- Implement `reconcile-stale` and `compact` CLI commands in
  `eimemory/ops/openclaw_loop.py`.
- Preserve latest task states and bound oversized historical fields during
  compaction.

## Task 4: Stop Masking Watchdog Failure

- Add deployment tests requiring a managed `openclaw-loop-watch.service` without
  `|| true` and installation through `deploy/install_immutable_release.sh`.
- Add the service/timer files and installer wiring.

## Task 5: Release And Deploy

- Advance `pyproject.toml`, `eimemory/version.py`, and systemd bytecode-cache
  versions to `1.9.28`.
- Run focused tests, `compileall`, and `git diff --check`.
- Commit and push `master`, then fast-forward `/dev-project/eimemory` on honxin.
- Deploy the exact commit with the immutable installer.

## Task 6: Production Repair And Verification

- Back up and update OpenClaw Codex policy and fallback routing.
- Stop the loop timer during maintenance, preview and apply stale reconciliation,
  compact with archive, then restart the managed timer and gateway.
- Run config validation, direct gateway/channel checks, cron smoke, loop health,
  ledger-size checks, and deployment identity checks.
