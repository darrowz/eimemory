Execute Task 1.2 (held-out 70/30 split for 145 learning_playbook) from E:\eimemory\docs\superpowers\plans\2026-06-17-eimemory-karpathy-loop.md. Read AGENTS.md and .harness/docs/conventions.md first, then follow the plan's TDD steps for Task 1.2 strictly. Test must RED first. After committing, append a line to .harness/changelogs/2026-06-17.md. Report back to mvs_0256a07393a44dbfabde11aaa5aff75b with: commit hash, test count (PASS/FAIL), minutes spent, any blockers. Do NOT push, do NOT use bash `&&`, do NOT call any paid API. NOTE: substitute `E:/eimemory/...` for the `/var/lib/eimemory/...` Linux paths.

==CRITICAL: BRANCH DISCIPLINE== (shared worktree is hostile) Before any other git operation:
1. cd to E:\eimemory and verify current branch with `git -C E:\eimemory branch --show-current`
2. If on master, create a feature branch: `git -C E:\eimemory checkout -b phase-1-task-1.2-held-out-split master`
3. Do ALL work on that feature branch. Do NOT `git checkout master` at any point.
4. Do NOT run `git reset` on shared branches.
5. Commit on your feature branch only. Do NOT merge to master.
6. Final report: include the feature branch name in your reply.