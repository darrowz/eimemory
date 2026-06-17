Execute Task 1.3 (gate=blocked rate > 0.3 forces verdict=fail - the most important fix in the whole plan) from E:\eimemory\docs\superpowers\plans\2026-06-17-eimemory-karpathy-loop.md. Read AGENTS.md and .harness/docs/conventions.md first, then follow the plan's TDD steps for Task 1.3 strictly. Test must RED first. The plan's Step 5 (real reproduction) is REQUIRED - the 6/17 evidence must produce verdict=fail. After committing, append a line to .harness/changelogs/2026-06-17.md. Report back to mvs_0256a07393a44dbfabde11aaa5aff75b with: commit hash, test count (PASS/FAIL), minutes spent, the actual verdict produced for the 6/17 evidence, any blockers. Do NOT push, do NOT use bash `&&`, do NOT call any paid API. NOTE: this task modifies `eimemory/governance/learning_eval.py` - read the existing file first to find where to add the new `compute_verdict` function without breaking the existing API.

==CRITICAL: BRANCH DISCIPLINE== (shared worktree is hostile) Before any other git operation:
1. cd to E:\eimemory and verify current branch with `git -C E:\eimemory branch --show-current`
2. If on master, create a feature branch: `git -C E:\eimemory checkout -b phase-1-task-1.3-gate-veto master`
3. Do ALL work on that feature branch. Do NOT `git checkout master` at any point.
4. Do NOT run `git reset` on shared branches.
5. Commit on your feature branch only. Do NOT merge to master.
6. Final report: include the feature branch name in your reply.