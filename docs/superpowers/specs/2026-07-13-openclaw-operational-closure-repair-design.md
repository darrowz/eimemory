# OpenClaw Operational Closure Repair

## Context

Honxin's OpenClaw gateway and Feishu channel are serving traffic, but unattended
Codex turns wait for an approval that nobody can answer, lifecycle tasks created
at `before_prompt_build` are not correlated with `agent_end`, and the watchdog
duplicates the complete stale-task set into two JSONL records every five minutes.
The watchdog unit also masks its non-zero exit status with `|| true`.

## Design

1. Make the Codex app-server policy explicit in the live OpenClaw configuration
   for this trusted unattended host. Keep normal OpenClaw exec policy unchanged,
   validate the configuration, and verify an actual cron command can finish
   without an approval request.
2. Preserve the loop task ID inside the bridge process. Correlate it by the most
   specific turn/request identifiers available and fall back to the session ID.
   Inject it into terminal hook task context, and forget the mapping only after a
   terminal loop state is confirmed.
3. Store bounded watchdog evidence: stale counts, reason counts, and a small task
   ID sample. Never embed complete stale task records or one code per stale task.
4. Add an explicit, dry-run-by-default stale reconciliation command. Applying it
   moves expired active tasks to `failed` with a machine-readable reconciliation
   reason, without manufacturing positive verification evidence.
5. Add atomic ledger compaction with a gzip archive. Tasks retain only their
   latest state; oversized historical watchdog checks and messages are replaced
   with bounded summaries. The archive is created before any current file is
   replaced.
6. Install a managed watchdog service without `|| true`. Exit code 2 represents
   a real degraded loop and must be visible to systemd and monitoring.
7. Restore the model route to `openai/gpt-5.6-sol` with only
   `minimax/MiniMax-M3` as fallback.

## Safety And Rollback

- All code behavior receives a failing regression test before implementation.
- Reconciliation is a preview unless `--apply` is supplied.
- Compaction archives original ledgers and uses same-directory atomic replace.
- Live OpenClaw configuration is backed up before editing and validated before
  the gateway is restarted.
- Deployment uses `/dev-project/eimemory` as the honxin code repository and the
  immutable installer for the separate runtime location.

## Verification

- Focused loop, bridge, deployment, and version tests pass.
- Python compilation and `git diff --check` pass.
- A production cron smoke completes without approval timeout.
- Fresh lifecycle traffic produces no stale running task.
- Watchdog returns zero only when healthy and writes bounded records.
- Health endpoints report version `1.9.28` and the deployed commit.
