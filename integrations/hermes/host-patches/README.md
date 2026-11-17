# Hermes memory lifecycle companion patch

`memory-lifecycle-2bd378ed.patch` is a separate Hermes host change. Installing
the eimemory Python package or plugins does not apply it. The normal eimemory
installer currently manages the completed-turn snapshot and profile-boundary
compatibility changes only; this package adds no automatic host installer.

The patch removes failed providers from host routes, transfers only the actual
cached memory-manager owner during a compatible agent rebuild, drains an
incompatible owner before replacement, and makes an occupied owner a retryable
failed turn before a model call. It includes the earlier initialization-status
fix. Do not apply that earlier patch and this combined patch sequentially.

## Exact compatibility boundary

The reviewed host HEAD is
`2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19`. The authoritative file preimages and
postimages are in [the manifest](memory-lifecycle-2bd378ed.manifest.json).
The published patch SHA256 (Git canonical LF bytes) is
`f756ce42475491824ffe1f7c13f6f1923e11059eeab9935dc2c49d2137f8529d`.

HEAD alone is insufficient. The copied `agent/memory_manager.py` already
contains the existing **redacted** completed-turn snapshot integration. The
live checkout may legitimately have those snapshot and profile-boundary
changes recorded as dirty. Preserve them: do not reset, clean, stash away, or
replace the checkout with a pristine upstream tree to make a patch apply.
Verify all four existing files against `before_sha256`, and confirm that
`agent/memory_handoff.py` is absent. A mismatch requires a separately reviewed
adaptation; do not use forced application or a three-way merge as acceptance.

This patch changes only:

- `agent/memory_manager.py`
- `agent/agent_init.py`
- `agent/memory_handoff.py` (new)
- `gateway/session_execution.py`
- `gateway/run_turn_runner.py`

The manifest also pins the current compatible eimemory provider and registry.
The provider exposes the read-only, strict-boolean
`can_reuse_for_session` opt-in; the registry exposes the typed
`memory_owner_busy` collision. The host must already own the exact previous
manager. The contract does not authorize acquiring an owner by matching kwargs.
The manifest separates the Git canonical LF `sha256` values used to verify
published plugin files from each `frozen_original_sha256` used by the historical
local replay. A later compatible adapter change updates only its current hash
and records its own verification; historical replay identities are retained.

The adapter emits `eimemory.hermes_session_observation.v1` events with hashes of
the session, canonical scope, home and sources. Collision events retain separate
attempted and retained-owner namespace hashes. `initialized` records only that
initialization returned. Recall observations distinguish `returned`, `empty`
and `unavailable` after the adapter has produced its response. They do not
certify answer correctness or recovery of a different attempted namespace.
An explicit recall's opaque capture reference can be checked against the
existing signed capture for the same namespace. No session text, query,
memory content, raw path or exception text is included in these events.

Publication uses LF line endings. Only CRLF-to-LF normalization is allowed; it
does not change patch semantics. `patch_audit_original_sha256` preserves the
original audit artifact identity separately from the published `patch_sha256`.
The audited patch was already LF when this package was verified, so those two
patch hashes are equal. The five host source **before/after** hashes remain the
exact original byte hashes and are never normalized; they identify the reviewed
host preimages and postimages, not Git-normalized substitutes.

The older `memory-sync-snapshot.json` describes a different historical patch
with base `693641aa...`. Its base and compatibility declaration do not apply to
this lifecycle patch. Existing snapshot and profile-boundary integration must
remain in place.

## Reviewed application and switching procedure

The deployment owner applies this package in the independently identified
Hermes checkout, using its actual deployment and gateway controls. No command
in this document selects a production host or starts a retired profile gateway.

1. Record the actual host HEAD, existing working-tree changes, the four exact
   preimage hashes, the absence of the new file, the plugin release identity,
   and the currently active gateway topology. Retain exact preimage backups
   and the existing restoration plan. Verify the patch bytes against the
   manifest. All checks are required even if HEAD matches.
2. Preflight the exact patch with `git apply --check`. In the existing controlled
   transition, stop/quiesce the gateway processes using this host checkout and
   confirm the old processes have exited. Preserve the previous active-unit
   set; do not revive inactive or retired standalone profile gateways.
3. Recheck the hashes and absence after quiescence. From the Hermes checkout,
   with `PATCH` set to the verified absolute path of this package's patch:

   ```sh
   git apply --check "$PATCH"
   git apply "$PATCH"
   ```

4. Verify all five `after_sha256` values and inspect the resulting diff. Confirm
   the existing snapshot and profile-boundary changes are retained. Run the
   applicable host tests in its real dependency environment with the matching
   plugin build before accepting the change.
5. Complete the coordinated host/plugin transition and start the selected
   gateway through its existing controller. A fresh process is required: the
   new lifecycle needs owner stamps created on the canonical cache entries.
   Reloading files into a live process or retaining unstamped old cached agents
   is not a supported installation method. Read back host/plugin identities,
   current admission/lease routing and lifecycle behavior after restart.

The eimemory `current` link alone does not roll back an independently modified
Hermes checkout. Record the host source identity and eimemory release as a pair.
Generic gateway health is not evidence that manager handoff and retry behavior
have passed acceptance.

## Recovery

Stop the affected gateway processes through the same controlled transition.
If all five files still match the manifest's **after** hashes, the exact patch
can be reversed without removing the pre-existing compatibility changes:

```sh
git apply --reverse --check "$PATCH"
git apply --reverse "$PATCH"
```

Verify all four original **before** hashes and that the newly introduced
`agent/memory_handoff.py` is absent. If postimage hashes differ, do not force a
reverse or restore entire files over unrelated edits; have the deployment
owner reconcile the recorded preimages and subsequent changes. Restore the
reviewed host/plugin pair and the prior active topology, then restart and
read back its identity. Do not delete or reset profile configuration, session
databases, memory queues, credentials, or other user state.

## Tests and evidence boundary

The retained local round2 replay compared the same 26 cases using the same
eimemory plugin: original host **2 passed / 24 failed**, initialization-only
control **2 passed / 24 failed**, combined patch **26 passed / 0 failed**.
Patch application in an isolated copy matched all five frozen postimages.
The recorded eimemory reuse contract suite passed 17 cases. These are historical
local results, not a new run performed while packaging or a production claim.

The replay sources and their hashes are recorded in the manifest:

- `.audit/round2/host/tests/test_canonical_memory_lifecycle.py`: rebuild and
  transfer, identity/source/config changes, rollback, cancellation, stale
  generation, strict opt-in, and exactly-once close.
- `.audit/round2/host/tests/test_busy_owner_lifecycle.py`: pending release/retry
  and another cache key pointing at the same physical session.
- `.audit/round2/host/tests/test_busy_turn_result.py`: the actual TurnRunner
  resolution/catch statement returns retryable failure before a model call.

Those retained audit files and copied source fixtures are not included in this
three-file publication package. Their replay substitutes outer constructor
services, native profile APIs, session DB, transport and release scheduling.
It does not run the complete host import/dependency environment, real adapters,
or complete TurnRunner admission and lease integration.

Only one standard host test file is present in the local copied `host/tests`
inventory: `tests/gateway/test_api_server_memory_sessions.py`. Its two observed
tests are:

- `test_manager_survives_across_requests_and_is_exclusive_per_home`
- `test_a_manager_parked_past_the_retry_horizon_is_not_handed_to_a_reused_id`

Run that file with the host's documented `scripts/run_tests.sh` runner in the
complete host checkout. It checks the **legacy** API checkout/checkin pool,
which this patch does not change. It is a compatibility check, not proof of
the canonical lifecycle fix. The copied host's `AGENTS.md` requires that runner
instead of bare pytest. No other standard host test filenames or pass results
are inferred from the partial local inventory; select additional canonical
cache, memory-manager, admission and lease tests after inspecting the actual
complete host test tree.

The reviewed normal bootstrap registers `session_authority` and routes unified
API requests through the canonical caller. A directly constructed legacy
adapter or custom runner that bypasses that authority has a separate ownership
path and is outside this fix. Real route readback and the host's integration
suite remain deployment acceptance work.
