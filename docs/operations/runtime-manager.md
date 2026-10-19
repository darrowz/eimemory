# Host runtime manager and controlled deployment jobs

This change moves registered eimemory services to PID1 with `User=darrow`,
`PrivateUsers=self`, `NoNewPrivileges=yes` and empty product capabilities.
Hermes/OpenClaw compatibility units retain their explicitly registered user
manager. A missing or corrupt root-owned registry is an error; no consumer
chooses an alternative manager from a healthy shadow service.

The privileged socket daemon imports only stdlib and its reviewed root-owned
contract copy. It never imports runtime-owned product Python. It accepts bounded
reads and content-addressed registered deployment jobs, not commands, paths,
environments or generic unit control. Lifecycle requests require the actual job
cgroup, runtime UID and NNP. Root persists the exact request before starting the
one-shot unit. Repeating a submission never dispatches it again after an
ambiguous start. The original invocation and captured active writer set survive
bridge restart. A same-transaction rollback may inherit that set only when all
request pins other than operation and target are identical.

`controller_commit` is the root-approved deployment controller; `runtime_commit`
is the actual serving release. An automatic candidate does not become a new
trusted controller. Only an explicit root-reviewed manual release permit can
advance that authority. Product ExecStart/Pre/Condition commands all run as the
runtime UID. The deployment worker is a separate PID1 cgroup and is not a
storage writer. It remains runnable while the storage transaction start guard
blocks writers. The worker closes its DB before installation.

Automatic installation requests background resume but defers it. The worker
must persist and reread the real deployment/rollback ledger outcome before
calling `finalize_deployment_job(job_key)`. The bridge compares target manager
consumer bytes to its reviewed compatibility hashes. Rolling serving back to a
legacy release is allowed; incompatible background maintenance remains
explicitly held, never reported as restored or used as L5 completion evidence.
A reviewed controller maintenance compatibility entry is a separate follow-up.

## Review and bootstrap

These commands describe preparation; they are not permission to migrate a host
without reviewing the actual source/bundle and current manager inventory.

1. Build from a raw Git-byte source export as an unprivileged operator:
   `python -B deploy/build_runtime_manager_bundle.py --controller-commit FULL_CURRENT_COMMIT --output NEW_BUNDLE.zip`.
2. Review all bundle members, raw SHA256, fixed templates, service environment
   files, root authority directory mounts, repository root and single RPC owner.
3. Copy the reviewed `bootstrap_runtime_manager.py` to a root-private directory
   with a verified SHA256. Never execute an editable repository script as root.
4. Run that protected copy with `install --bundle ROOT_PROTECTED_BUNDLE --expected-bundle-sha256 REVIEWED_SHA --repository-root /dev-project/eimemory`.
   It preserves original root bytes and exact old active states before stopping
   old user product owners. It starts the root bridge and only the RPC initially;
   automatic jobs stay paused until the new release transition completes.
5. The protected bootstrap's `stage-release --target-commit FULL_REVIEWED_COMMIT`
   creates a one-shot root permit and starts the fixed runtime-user launcher.
   The manual job invokes the registered repository worker as darrow. It cannot
   supply a root command. Resume restores the original captured set.

No bootstrap, production authority change, migration or deployment was run as
part of preparing this source. A migration failure preserves its history and
stopped/active observations; it is never silently overwritten or retried.

## Evidence boundaries

The current source is a reviewable boundary candidate. Unit/contract tests do
not prove PID1 namespace behavior. Release integration still requires isolated
native SO_PEERCRED/NNP/cgroup-survival tests, both full CI workflows and actual
migration review. A business 12/12 result is not an L5 terminal or 28800-second
observation. Legacy rollback held-background behavior is an explicit safety
limit until the separate controller compatibility entry is validated.
