# Host runtime manager and controlled deployment jobs

This change moves registered eimemory services to PID1 with `User=darrow`,
`PrivateUsers=self`, `NoNewPrivileges=yes` and empty product capabilities.
Hermes/OpenClaw compatibility units retain their explicitly registered user
manager.
The manager registry is fixed at
`/etc/eimemory-code-automation-authority/runtime-manager.v1.json`, under an
existing root-owned non-writable authority directory. The ordinary
`/etc/eimemory` directory is owned by the runtime account on the deployed host;
it remains available for legitimate child policies and service configuration.
Neither bootstrap nor a registry reader changes that directory's ownership or
accepts it as a root trust anchor. A root-owned file inside a runtime-owned
parent is rejected before JSON parsing, since the parent owner could replace it.
A missing or corrupt root-owned registry is an error; no consumer
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

Receipt and post-resume business validation use separate fixed PID1 child
units. Their controller entry is pinned, while all release source and
interpreters are mounted read-only. The target remains the actual serving
release, including a legacy rollback; controller identity cannot substitute for
target identity. Submission is one-shot, and repeated status reads cannot
restart an ambiguous validation. Before exiting, the actual child cgroup seals
the exact result bytes through the bridge. Root stores an exclusive immutable
copy and digest, then records PID1's exit outcome independently. Replacing the
runtime user's output afterwards cannot replace the accepted evidence.

Every managed Python entry, including the root daemon, uses an explicit
`-X pycache_prefix=/var/lib/eimemory-manager/empty-pycache` with `-B`. The prefix
is an empty root-owned directory with mode 0555. `-B` alone still reads existing
bytecode and is insufficient to bind execution to reviewed source.

Automatic installation requests background resume but defers it. The worker
must persist and reread the real deployment/rollback ledger outcome before
calling `finalize_deployment_job(job_key)`. This resumes the captured set but
keeps the global transition reserved as `resumed_awaiting_business`. Only after
real business readback, the required stability interval and a persisted OBSERVING
transition may the worker call `complete_deployment_job(job_key)`. The bridge compares target manager
consumer bytes to its reviewed compatibility hashes. Rolling serving back to a
legacy release is allowed; incompatible background maintenance remains
explicitly held, never reported as restored or used as L5 completion evidence.
The root record retains the original active set. An explicit reviewed manual
upgrade may inherit a completed held set; automatic work cannot adopt it. A
reviewed controller maintenance compatibility entry is a separate follow-up.

## Review and bootstrap

These commands describe preparation; they are not permission to migrate a host
without reviewing the actual source/bundle and current manager inventory.

1. Build from a raw Git-byte source export as an unprivileged operator:
   `python -B deploy/build_runtime_manager_bundle.py --controller-commit FULL_CURRENT_COMMIT --output NEW_BUNDLE.zip`.
2. Review all bundle members, raw SHA256, fixed templates, service environment
   files, root authority directory mounts, repository root and single RPC owner.
3. Copy the reviewed `bootstrap_runtime_manager.py` to a root-private directory
   with a verified SHA256. Never execute an editable repository script as root.
4. Run that protected copy with `install --bundle ROOT_PROTECTED_BUNDLE --expected-bundle-sha256 REVIEWED_SHA --repository-root /dev-project/eimemory --unit-scope-manifest REVIEWED_SCOPES.json --expected-unit-scope-sha256 REVIEWED_SCOPE_SHA`.
   The scope manifest contains only individually reviewed native producer
   owners, initially watch and nightly; it cannot guess an owner for a closure
   reconcile service. Missing scopes cannot issue activation evidence.
   It preserves original root bytes and exact old active states before stopping
   old user product owners. It starts the root bridge and only the RPC initially;
   automatic jobs stay paused until the new release transition completes.
   Previously failed user units are disabled without stopping or resetting
   their failed invocation. The install entry rejects an existing manager
   registry: upgrading a live root daemon requires a separately reviewed
   authority handoff and cannot replace its modules in place.
5. Build the initial bundle with the actual current commit as its initial
   controller coordinate, while reviewing all new template/helper bytes. It
   intentionally does not dispatch an automatic worker absent from an old
   controller. The protected bootstrap's
   `stage-release --target-commit FULL_REVIEWED_COMMIT --attempt-id REVIEWED_UUID_HEX`
   creates a one-shot root permit and starts the fixed runtime-user launcher.
   The manual job invokes the registered repository worker as darrow. It cannot
   supply a root command. Activation verifies the new worker entry and every
   protected consumer byte, then atomically advances controller/runtime before
   watchers resume. Resume restores the original captured set.
   If the actual serving release is legacy or otherwise incompatible after a
   failed manual attempt, background services remain held with the original
   active set retained. This does not count as a completed owner migration.
6. If a manual attempt fails, retain its original root record and Invocation.
   A new attempt requires a new reviewed nonce and `--prior-job-key OLD_KEY`;
   root checks the original unit is physically failed/inactive, the current
   release and storage journal are unchanged/clean, and no unrelated transition
   exists. Unknown starts are never retried.
7. First-migration owner recovery uses `restore-owner --history-id ID
   --expected-history-sha256 SHA --expected-current-commit ORIGINAL
   --expected-registry-sha256 SHA`. It requires the original serving commit and
   a clean storage recovery journal. It stops the new owner, restores exact
   root/user source/drop-ins and original enablement, starts only the original
   RPC owner, verifies real health identity, then restores original active units.
   A changed serving release must first complete its real storage/deployment
   rollback; changing a symlink or owner cannot substitute for it.
8. Managed incident authority remains disabled until actual deployment inputs
   produce a reviewed manifest. The separate root-only
   `enable-managed-authority --managed-incident-manifest FILE
   --expected-managed-incident-manifest-sha256 SHA
   --expected-registry-sha256 SHA` validates the fixed issuer configuration and
   source before publishing it. The key and receipts stay root-only.

New deployment receipts carry a typed declarative managed-job rollback plan.
They do not contain shell commands claiming to work outside a registered job
cgroup. Publish/readback validators bind the exact source and prior target,
fixed controller API and required receipt/context conditions. Real legacy
command receipts remain readable under their historical contract.

No bootstrap, production authority change, migration or deployment was run as
part of preparing this source. A migration failure preserves its history and
stopped/active observations; it is never silently overwritten or retried.

## Evidence boundaries

The optional fused closure is also a registered PID1 owner. Its USER is the
runtime account, not PID1's manager user. It acquires a narrow root context for
the fixed L1 service/timer pair, preserving their original active set under the
same global transition mutex as deployment. The shell cannot select units or
use user-manager mutations. Its normal exit trap restores that context; fixed
root-reviewed ExecStopPost and next ExecStartPre hooks recover it after an
interrupted invocation. A changed serving release, another active transition
or unknown native identity leaves the durable context held. Failed/inactive L1
units are never started or reset. All four configured scope coordinates are
passed identically to L1 eval and production-query/release-closure commands.

The current source is a reviewable boundary candidate. Unit/contract tests do
not prove PID1 namespace behavior. Release integration still requires isolated
native SO_PEERCRED/NNP/cgroup-survival tests, both full CI workflows and actual
migration review. A business 12/12 result is not an L5 terminal or 28800-second
observation. Legacy rollback held-background behavior is an explicit safety
limit until the separate controller compatibility entry is validated.
