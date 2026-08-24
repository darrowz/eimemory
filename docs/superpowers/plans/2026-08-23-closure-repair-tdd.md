# Business Closure Repair TDD Plan

**Goal:** Close the two reproduced operational/adapter gaps, then remove only the deployment helpers proven unreachable by the project-wide audit.

**Evidence boundary:** Synthetic states exercise mechanisms in private temporary roots and are deleted. They do not contribute to production L5 or autonomous-evolution evidence.

## Task 1: Monitor the maintained OpenClaw control loop

- Add a failing `tests/test_timer_monitor.py` case in which `openclaw-loop-watch.service` is failed and every other default unit is healthy; require an incident/issue for that exact unit.
- Update the default-inventory contract to include current installed audit, timer-monitor, OpenClaw watch/compact timers and their services, plus release-closure service. Retain learning watch/think/dashboard behind `include_legacy_learning_timers`.
- Implement the smallest constant-list change in `eimemory/ops/timer_monitor.py`.
- Run `tests/test_timer_monitor.py`, `tests/test_openclaw_loop_io.py`, and the deployment unit inventory assertions.

## Task 2: Validate Feishu platform receipts at the maintained consumer

- Add a failing `tests/test_openclaw_reply_delivery_tracker.py` case proving an otherwise successful message-tool receipt with an invalid platform message ID leaves the reply pending and stores no delivery ID.
- Add one JavaScript message-ID predicate matching the existing Feishu `om_` platform contract; reuse it for inbound correlation, persisted delivery attempts, outbound `message_sent`, and message-tool receipts.
- Run the reply-delivery tracker and OpenClaw terminal bridge suites.

## Task 3: Delete proven dead deployment/test surfaces

- Delete `deploy/extract_feishu_message_id.py` after its fail-closed invariant passes at the JavaScript owner.
- Delete only the old helper import and parameterized helper tests from `tests/test_deployment_tools.py`; keep deployment and OpenClaw coverage.
- Delete uncalled `deploy/verify_l5_v3_migration.py`; capability schema/FK/backfill/dual-write/L5/release tests remain its surviving invariant owners.
- Re-run deployment tools, capability storage, L5 v3 release independence, and source-audit coverage.

## Task 4: Verify the affected flow families

- Run timer/loop, OpenClaw delivery/terminal, deployment, capability/L5, package syntax, source audit, and `git diff --check`.
- Update the project-wide audit with repaired/deleted dispositions and no prospective production claim.
