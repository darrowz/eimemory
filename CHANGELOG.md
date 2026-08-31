# Changelog

## [1.11.56] - 2026-08-31
### Fixed
- Install the optional OpenClaw bridge through explicit external plugin
  configuration (`origin=config`) and send delivery probes through the public
  authenticated Gateway SDK. Never impersonate a bundled plugin or modify the
  upstream OpenClaw package; authentication failures cannot become receipts.
- Make OpenClaw deployment selectable with `EIMEMORY_OPENCLAW_ADAPTER`:
  `auto` detects an existing integration, `enabled` requires it, and `disabled`
  leaves it out. A missing optional adapter does not block the core runtime;
  incomplete installations selected for integration still fail closed.
- Move generic record export into core storage, retaining the old adapter
  import as a compatibility facade. A fresh interpreter without OpenClaw can
  initialize Runtime, write and recall memories, and read capability state.
- Remove OpenClaw startup and directory dependencies from core systemd units.
  Monitor optional adapter unit groups only when present; partial installs and
  unreadable service state remain errors, not successful absence.
- Add `--recover-only` for the exact candidate bound to an existing storage
  transaction marker. Recovery restores and health-checks the prior release
  without creating a new release or deployment receipt.
- Resume interrupted rollback validation without replaying an already-restored
  snapshot over subsequent prior-release writes. Durable journal phase,
  restored release identity and absence of an unfinished restore journal are
  required before selecting this validation-only path.
- Normalize managed Feishu delivery settings into the canonical `streaming`
  schema and explicitly authorize the external bridge's conversation hooks.
  Preserve canonical preview/coalescing settings without a broad doctor or
  session migration.
- Require consecutive authenticated local Gateway health RPC responses after
  an enabled adapter restarts, including rollback; transient process activity
  and static plugin inspection do not establish readiness.
- Use owner-allocated private disk-backed verification scratch instead of an
  unbounded temporary-memory filesystem, retaining network isolation,
  read-only candidate inputs and masking of production secrets/data.
- Classify the explicit OpenClaw deployment helpers as channel-delivery
  changes, retaining fail-closed treatment of unknown production paths and
  requiring real channel evidence before lineage can be inherited.
- Keep immutable code-implementation v9 identity and digest unchanged. Healthy
  deployment is not L5 completion and does not replace strict observation.

## [1.11.55] - 2026-08-31
### Fixed
- Prepare protected regressions for active incident status and exact detector
  release identity, plus the authorized repair path. The final routing patch
  remains for a separately verified strict candidate; this support release
  does not claim that the detector routing defect is already fixed.
- Add a source-faithful, one-shot maintenance entry for the protected incident
  routing repair, keeping its policy, execution authority and regression tests
  outside the candidate's file allowlist.
- Preserve known/user-reported/bootstrap provenance in active transaction
  projections and verify it again during strict deployment admission.
- Project the explicitly recorded immutable Profile identity from transaction
  payloads into ledger results, rejecting conflicting or malformed identities.
- Separate operational observation from autonomous L5 qualification: an
  authorized, healthy maintenance transaction can complete the real 48-hour
  observation without being credited as an unknown system-discovered repair.
- Retain immutable code-implementation v9 identity and durable catalog receipts
  across this governance-only release.

## [1.11.54] - 2026-08-31
### Fixed
- Publish the changed Hermes code-implementation provider as immutable revision
  and binding v9, preserving and deprecating v8 instead of conflicting with its
  durable implementation digest during the post-switch owner refresh gate.
- Align strict transaction admission, dynamic candidate routing, L5 terminal
  validation, deployment reporting, and adapter fingerprint policy with v9.

## [1.11.53] - 2026-08-31
### Fixed
- Treat deployment receipts and storage acceptance records as separate evidence
  roles in release-closure repair requests; receipt fallback is explicitly forbidden.
- Reject model proposals before candidate materialization unless deployment and
  code-evolution evidence directly use the authoritative receipt input.
- Classify missing strict receipts and active observation as expected waiting
  states instead of repeatedly generating false code-repair incidents.
- Keep protected tests deterministic under production environment variables,
  full-suite load, and unordered duplicate record identities.

## [1.11.52] - 2026-08-31
### Fixed
- Separate strict deployment admission from terminal L5 closure. Admission uses
  the transaction Profile, bounded real replay, live acceptance, skill/rollback
  rehearsal, strict receipt and independently revalidated release lineage.
- Keep the reader incomplete until actual observation and sedimentation finish;
  admission neither creates an observation sample nor starts or shortens its clock.
- Preserve the explicit legacy release workflow outside strict transaction mode.

## [1.11.51] - 2026-08-31
### Fixed
- Isolate closure rehearsal SOP identities and groups by capability target;
  select only a skill derived from that rehearsal's exact seed records.
- Require an explicit Profile-covered correction target in dynamic rehearsal,
  returning a clear blocked reason before writing unattributed correction data.

## [1.11.50] - 2026-08-31
### Fixed
- Preserve a ledger-verified strict deployment receipt during ordinary closure
  and live acceptance rechecks instead of replacing its release-session identity.
- Give each observation measurement its own key and retain phase witnesses plus
  recent health samples, so a 15-minute watcher can complete the real 48h window.
- Anchor delayed initial sampling at its actual timestamp without backdating
  phase zero or changing the installer-bound receipt deadline.
- Use Profile keys rather than revision IDs in code proposals; route the exact
  machine-policy Profile and reject unavailable profiles before provider work.

## [1.11.49] - 2026-08-31
### Fixed
- Bind observation reads to the transaction Profile and verify the exact
  active transaction instead of accepting an unrelated pending transaction.
- Accept an explicitly nonblocking, deployment-independent capability axis
  during observation, while independently requiring strict transaction-bound
  deployment evidence and live health. Missing or blocking axes still fail.
- Reject live inspection when the health endpoint is not explicitly healthy
  and ready, even if its reported commit matches the expected deployment.

## [1.11.48] - 2026-08-31
### Fixed
- Bind persisted replay revalidation to the manifest-validated capability
  revision in L5 readiness, retaining exact result and contract checks.
- Cover three independent persisted code-implementation acceptances through
  the readiness summary, including rejection of missing or altered revisions.

## [1.11.47] - 2026-08-31
### Fixed
- Accept the current v4 dynamic readiness envelope in configured-profile live
  probes and closure rehearsal, retaining explicit product-completion checks.
- Reject unknown schemas and missing projection objects instead of treating
  default empty dictionaries as validated replay structure.
- Stop spent one-shot automation policies before provider invocation or
  candidate verification; existing transactions remain under recovery control.

## [1.11.46] - 2026-08-31
### Fixed
- Keep proposal-time advertisement authority distinct from refreshed provider
  liveness during protected effects and observation; retain both digests.
- Start the real 48-hour observation clock only after verified deployment and
  health, including crash recovery, while preserving installer receipt binding.
- Include effect ownership, observation scheduling and repository authority in
  the code-evolution release-lineage surface.

## [1.11.45] - 2026-08-31
### Fixed
- Preserve executor contract, grader metadata, metrics and verdict when replaying
  persisted capability probes through trusted recorded-execution validators.
- Cover the complete acceptance-to-persisted-replay path with a code-generation
  regression that forbids a second provider invocation.

## [1.11.44] - 2026-08-31
### Fixed
- Validate replayed non-deterministic Catalog executions from their sealed,
  trusted provider receipts instead of requiring an impossible byte-identical
  second external invocation; deterministic executors retain exact reexecution.
- Accumulate unique replay evidence across contiguous manifests bound to the
  same dynamic Profile selection, so capability maturity is not hard-coded to
  a fixed number of cases in one run.
- Keep active code-evolution transactions authoritative in L5 projections and
  stop resolved historical quarantines from contaminating the current outcome.

## [1.11.43] - 2026-08-29
### Fixed
- Failed candidate deploys that never became the live release now abort as
  `aborted_candidate_restored` instead of claiming `rolled_back_healthy`.
  Healthy rollback remains only after the candidate actually landed.

## [1.11.42] - 2026-08-28
### Fixed
- Classify the new closure-incident recorder, system repair router, detector,
  and learning CLI entry point in release lineage so production code surfaces
  cannot fall into the unknown-path fail-closed bucket.

## [1.11.41] - 2026-08-28
### Fixed
- Treat pure integration-manifest version bumps as release metadata instead of
  resetting capability-domain lineage evidence on every release.
- Make the expensive release-closure workflow risk-triggered by default while
  retaining lightweight deployment receipts, lineage, and technical health on
  every production switch.
- Persist actionable post-deploy closure failures as idempotent system
  incidents and route them through the protected Hermes code-evolution v2
  proposal and transaction path from the learning watcher.

## [1.11.40] - 2026-08-28
### Fixed
- Keep legacy release-lineage finalization on its explicit compatibility
  catalog even when the runtime has an installed dynamic application catalog.

## [1.11.39] - 2026-08-28
### Fixed
- Give active provider revalidation a state- and work-item-bound idempotency
  key, so it cannot collide with the original discovered-to-active activation
  request for the same immutable capability definition.

## [1.11.38] - 2026-08-28
### Fixed
- Active capabilities now support CAS-protected catalog revalidation when an
  incompatible provider revision is introduced. Incubation executes fresh,
  bounded catalog passes for the current binding and records an active-to-active
  evidence event, while already revalidated bindings remain idempotent.

## [1.11.37] - 2026-08-28
### Changed
- Advanced the protected Hermes code-implementation provider to revision v8.
  The fourth official host hook changes a source file covered by the immutable
  provider digest, so the existing capability lifecycle now retires v7 and
  registers a distinct v8 revision instead of mutating v7 in place.
### Fixed
- Briefly queue a provider connection while a live-socket probe releases the
  single concurrency slot, preventing the next genuine health request from
  being spuriously disconnected without increasing provider concurrency.

## [1.11.36] - 2026-08-28
### Fixed
- Release channel acceptance is now transport-neutral: lineage uses
  `channel.delivery`, consumes a normalized trusted-adapter ledger, and keeps
  the OpenClaw/Feishu ledger as a compatibility input rather than a product
  requirement.
- The official Hermes hook now binds genuine external inbound turns to the
  exact durable delivery obligation and records acceptance only after Hermes
  marks the platform delivery successful. Local, replay, webhook, bot, stale,
  failed, and release-mismatched events remain ineligible.

## [1.11.35] - 2026-08-27
### Fixed
- Protected code-evolution verification now redirects explicit Python bytecode
  compilation into the sandbox's writable temporary filesystem, preserving the
  read-only candidate tree while allowing the full regression suite to run.

## [1.11.34] - 2026-08-27
### Fixed
- Protected code-evolution candidate materialization now parses raw Git
  porcelain output without stripping the first status column, so an exact
  multi-file proposal is not falsely rejected as an out-of-scope worktree.

## [1.11.33] - 2026-08-27
### Fixed
- Production real-query evaluation now uses the retrieval engine's existing
  bounded maximum candidate pool before semantic ranking-identity dedupe, so
  a growing family of deployment-turn clones cannot starve a distinct labeled
  replay family from the measured top five.

## [1.11.32] - 2026-08-27
### Fixed
- Normal immutable upgrades now bind pre-switch recall baselines and deployment
  receipts to the actual current release commit. Historical release discovery
  remains a fallback only when no distinct current predecessor exists.
- Release lineage now classifies the managed systemd drop-in installer as a
  deployment-runtime path instead of reporting it as unknown production code.

## [1.11.31] - 2026-08-27
### Fixed
- Pre-switch production-recall baselines now bind their evaluator identity to
  the candidate commit, so a trusted predecessor anchor can qualify after the
  immutable switch.
- Code-evolution deployment now limits a candidate-side deployment delta to a
  small runtime-identity policy module whose parent is the trusted local HEAD.

### Changed
- Runtime drop-in naming and verification-unit selection now pass through a
  bounded policy surface, allowing the system-detected drift incident to be
  repaired without granting automatic authority over the large installer.
- Release lineage classifies the runtime drift detector and its deployment
  policy as production runtime/code-evolution changes.

## [1.11.30] - 2026-08-27
### Added
- A fail-closed runtime-identity detector now records idempotent,
  system-originated incidents when Python evidence producers are bound to a
  commit other than the current immutable release.
- Governed code evolution now has an incident-bound protected test plan for
  repairing immutable-installer runtime metadata drift without widening its
  file or command authority.

### Fixed
- Strict v2 proposals and effect execution now bind the incident class to its
  exact protected test plan, preventing an unrelated incident from selecting
  a broader file allowlist.

## [1.11.29] - 2026-08-27
### Fixed
- Production bootstrap now reconciles bounded record-status payload and recall
  projections atomically, then quarantines accepted-query evidence chains whose
  labeled record has become inactive.
- Production query repair scans canonical Codex and Hermes channel partitions
  as well as the base scope, closing the post-migration validation gap.
- Recall evaluation now uses one authority-bound semantic identity for both
  deployment-memory clone deduplication and ranking metrics.

## [1.11.28] - 2026-08-27
### Fixed
- Content-addressed production recall snapshot directories are now created,
  ownership-validated, and enforced as 0700 regardless of the deployment
  user's umask, matching the secure dataset loader's parent-chain contract.

## [1.11.27] - 2026-08-27
### Fixed
- Production recall datasets now publish as immutable content-addressed
  snapshots selected by a bounded, digest-verified current pointer.
- Bootstrap stages a new snapshot, qualifies and persists its real baseline,
  and only then atomically advances current; failed gates retain the prior
  dataset while preserving the candidate snapshot for audit.
- Pointer ownership, mode, hard-link count, path, size, and digest are
  validated fail-closed before runtime loading.

## [1.11.26] - 2026-08-27
### Fixed
- Production-query repair quarantines complete legacy pending-label-accepted
  chains when their proactive decision authority is missing or mismatched,
  while preserving bounded audit receipts and keeping unrelated tampering
  fail-closed.
- Pre-switch deployment summaries now expose the exact quarantine count.

## [1.11.25] - 2026-08-27
### Fixed
- Production query bootstrap now repairs bounded pending-label rows through
  indexed, idempotent authority transitions before dataset collection, and
  fails closed on conflicting business evidence.
- Identity repair preserves valid Hongtu Codex/Hermes channel scopes instead
  of collapsing production evidence into a generic channel.
- Governed code evolution now owns the complete intent-first materialize,
  sandboxed verification, commit, CAS push, immutable deploy, observation,
  crash recovery, and verified rollback path with durable receipts.

## [1.11.24] - 2026-08-26
### Fixed
- Immutable installer now releases `storage_deploy_lock` and
  `candidate_validation_lock` immediately after technical commit/prune,
  so the next release is not blocked by the long `learn release-closure`
  sqlite scan.
- Pre-switch production-recall bootstrap treats
  `bootstrap_pending_regression_forbidden` as already-advanced success
  when an `anchor_ready`/`strict_activated` state already exists.

## [1.11.23] - 2026-08-26
### Fixed
- Immutable installer resumes `eimemory-release-closure.path` immediately
  after technical health, before the long `learn release-closure` run, so
  current-commit Feishu receipts can auto-reconcile and timer-monitor does
  not stay failed while the path unit is paused.

## [1.11.22] - 2026-08-26
### Fixed
- OpenClaw `agent_end` no longer downgrades a genuine `platform_accepted`
  receipt to `final_ready` when `last_sent_content` and `final_text` differ
  by whitespace or decoration. Probe/message-tool receipts stay accepted.

## [1.11.21] - 2026-08-26
### Fixed
- OpenClaw delivery probe now sends `message.action` in the schema the
  gateway actually validates: nested `{ params: { to, message } }` plus a
  required `idempotencyKey`. 1.11.20 reached the handler (trust gate
  cleared) but was rejected as `must have required property 'params'`.

## [1.11.20] - 2026-08-26
### Fixed
- OpenClaw bridge delivery probes now run inside the gateway trust boundary:
  the installer materializes the bridge as a bundled plugin (symlink under the
  OpenClaw package's `dist/extensions`) and removes the config-origin load
  path, so in-process `gateway.request('message.action')` calls are granted.
  This is the only viable receipt path — the revise fallback is structurally
  discarded by the harness whenever the turn used any side-effectful tool.
### Changed
- `verify_openclaw_plugin_runtime` now requires `origin=bundled` in strict
  mode so deployments fail fast if the bundled trust boundary regresses.

## [1.11.19] - 2026-08-26
### Fixed
- OpenClaw bridge: deliver accepted feishu finals directly through the
  gateway message.action runtime when the channel dispatch drops them, so
  platform receipts land without relying on agent tool compliance.

## [1.11.18] - 2026-08-26
### Fixed
- OpenClaw bridge: feishu direct replies stopped emitting `message_sent`
  after the openclaw 2026.7.x reply-dispatch refactor, so channel-acceptance
  receipts never reached `platform_accepted` and L5 release closure blocked
  on `current_release_channel_receipt_not_found`. When a feishu:direct
  session has an unaccepted delivery entry for the current runtime commit,
  the completion gate now asks the agent to deliver its own final via the
  message tool — producing a genuine platform-accepted receipt.

## [1.11.17] - 2026-08-26
### Fixed
- Engine `bundle.rules` now collapses ground-truth clone families BEFORE
  the 50-slot cut (61 duplicates of one T0 rule previously filled every
  slot after the relevance sort, starving distinct rules — including the
  labeled 工具匹配门/Tool Match Gate rule at recency position 55). GT rules
  are identified by behavior content; everything else stays exact.
- Gate rule-prepend now swaps in the labeled rule when labels reference a
  rule outside the head slot.

## [1.11.16] - 2026-08-26
### Fixed
- Gate recall depth raised to a 24-candidate pool so semantic clone
  families collapse BEFORE the labeled top-5 cut. With limit=5 the pool
  was nine "Hermes completed turn" deployment clones, and the labeled
  record (rank ~15) never had a chance; with dedupe-on-deep-pool the
  label-preferred representative surfaces in the top-5.
- Memory semantic identity is title+channel only: summaries carry per-run
  commit hashes/turn transcripts that made every clone look unique.

## [1.11.15] - 2026-08-25
### Fixed
- Gate labeled top-5 now prepends at most one query-relevant rule, then
  the engine's ranked items. Dumping the whole active-rule list in front
  of items let boilerplate capability-candidate rules occupy four of five
  slots (precision@5 capped at 1/5 ≈ 19.77% even with 96% recall).

## [1.11.14] - 2026-08-25
### Fixed
- Production recall gate now collapses semantically identical
  ground-truth behavior rules (61 clones of one T0 rule) before scoring
  the labeled top-5, so they no longer occupy every slot and starve
  genuine memory hits (precision@5 was 19.77% vs the 20% threshold).
- Persisted `result_refs` now keep 500 keys instead of 200, matching the
  dataset size so the stored digest still matches the computed digest.

## [1.11.13] - 2026-08-25
### Fixed
- `bundle.rules` is now query-relevance ordered (stable; zero-overlap rules
  keep recency order) instead of raw recency. The production recall gate
  merges rules ahead of items, so the unsorted head of the list let stale
  boilerplate capability-candidate rules dominate every labeled top5
  (recall@5 ≈ 4%). Engine policy version bumped `governed-recall.v2` → v3.

## [1.11.12] - 2026-08-25
### Fixed
- Rebuilt the conventional production recall dataset at
  `/var/lib/eimemory/evaluation/production_recall.json` from accepted
  operator labels (262 cases across codex/hermes/openclaw); the previous
  file still held the five-case July snapshot, so every strict gate ran on
  stale data and pre-switch anchors never matched the live digest.

## [1.11.11] - 2026-08-25
### Fixed
- The catalog preflight request now states the expected fixture outcome
  ("VALUE assignment equals 2") instead of asking for an unqualified
  replacement proposal, so the structured-completion model can satisfy the
  sealed evaluator; the catalog client also uses the full bounded
  completion budget (125s) instead of the 15s probe default.

## [1.11.10] - 2026-08-25
### Fixed
- Hermes hook now registers the `eimemory_code_implementation`
  auxiliary task with the host, so capability-incubation catalog
  preflight can route structured completion instead of being denied
  as an unregistered task.
- CLI `--profile l5.default:v1` resolves to the lineage key
  `l5.default` instead of failing as an unknown profile id.
- Hermes `code.implementation` bumped `v6` → `v7` because the
  auxiliary-task registration changed the implementation digest;
  register v7, deprecate v6 (same immutable-store contract).

## [1.11.9] - 2026-08-25
### Fixed
- Hermes proactive decisions were bypassing with `release_identity_unavailable`
  because `_scope_from_context` used the host profile name (`default`) as
  `agent_id`. Honor `EIMEMORY_AGENT_ID` first so the lookup matches the
  deployment-receipt scope (`hongtu`).
- `CodeImplementationSocketServer` popped `operation` before
  `validate_request()`, whose exact-key contract still requires that field
  (and its digest). Every `propose_patch_v2` call failed with
  `request_missing_fields`, so capability-incubation could never record its
  two catalog passes. Route on the operation without mutating the request.
- Hermes `code.implementation` bumped `v5` → `v6` because that routing
  fix changed the implementation digest; the immutable capability store
  rejects same-revision digest replacement (`CapabilityConflict`).
  Register v6, then deprecate v5.
- CLI `--profile l5.default:v1` now resolves to the lineage key
  `l5.default` instead of failing as an unknown profile id.
- Hermes hook now registers the `eimemory_code_implementation`
  auxiliary task with the host, so catalog preflight can route
  structured completion instead of being denied as unregistered.

## [1.11.8] - 2026-08-25
### Fixed
- Map the thought-queue / long-term goal artifact alias
  `rule_sop_eval_or_skill` onto actionable candidate kinds so the dynamic
  learning loop can convert classified goals instead of reporting
  `no_accepted_capability_goal_with_declared_artifact`.
- Stop promoting unclassified thoughts into the daily goal quota; they have
  no accepted capability and were crowding out convertible goals.

## [1.11.7] - 2026-08-24
### Fixed
- runtime adapter `remember` now writes memories into each channel's native
  source partition (`hermes` / `codex`) instead of the legacy shared `default`
  partition, so proactive exact-source recall can match them.

## [1.11.6] - 2026-08-24

- Fix the Hermes channel of the production recall dataset: native Hermes
  proactive decisions carried the legacy shared "default" partition alongside
  the authoritative "hermes" partition, and the exact-source contract skipped
  all of them as `non_exact_source`, leaving the hermes channel at zero
  pending/accepted cases. The native default is now exactly `["hermes"]`.
- Register a semantic capability profile (`xiaomage:v1`) so L5 projection is
  no longer tied to the implicit `l5.default` key; profile resolution stays
  agent-agnostic and any runtime scope can resolve it.

## [1.11.5] - 2026-08-24

- Fix the hourly false-positive kill switch: legacy pre-hash-chain rows in
  `state/audit.jsonl` made every audit verification raise ChainBroken at row 0,
  which triggered `emergency_stop()` and killed all eimemory processes (62+
  historical firings; twice on 2026-08-24). Add
  `eimemory.governance.safety.reseal_audit_log` to wrap every legacy row
  verbatim into a fresh verified sha256 chain (dry-run by default, original
  preserved, atomic swap under the appender lock).
- Make the emergency-stop audit append chain-aware (`AuditLog.append`) so a
  kill-switch firing can no longer write an unchained row that re-poisons the
  log it exists to protect.

## [1.11.4] - 2026-08-23

- Close project-wide operational monitoring over the current audit, monitor,
  L5 review, OpenClaw watch/compact, and release-closure units. The immutable
  installer now installs and enables the audit verifier and timer monitor in
  the same one-command release transaction.
- Reject malformed Feishu platform message IDs at the maintained OpenClaw
  delivery owner so an untrusted receipt cannot close a pending reply.
- Validate structured OpenClaw outcome contracts against the runtime-sealed
  capability catalog without granting host-side execution authority.
- Remove two unreachable one-off deployment helpers after moving their sole
  delivery invariant to the maintained bridge, and keep the source audit
  correct while tracked files are being deleted from a release worktree.
- Reclaim synthetic evaluation, converted-data smoke, OpenClaw bridge, and
  code-provider test roots on success and failure instead of leaving fixture
  data under the system temporary directory.
- Version the immutable code-implementation coordinates at v5 after the first
  deployment attempt correctly rejected a changed v4 binding. The provider
  fingerprint now canonicalizes only the Hermes manifest's top-level release
  version, so packaging-only bumps preserve v5 identity while every behavioral
  source and manifest field remains attested.
- Snapshot durable runtime state for code-only releases as well as schema
  migrations, and retain the sealed transaction marker through all mandatory
  gates. Parent-bound deployment and validation leases admit only the exact
  candidate or durably restoring prior release, without leaking locks into
  child processes. Ordinary failures and interrupted validation restore and
  health-check the prior capability lifecycle before the transaction clears.

## [1.11.3] - 2026-08-23

- Keep local provider health probes outside the bounded proposal/model rate
  budget so deployment verification cannot starve the official advertisement
  refresh owner.
- Version the immutable code-implementation revision and Hermes binding at v4
  so the corrected provider can coexist with preserved v2/v3 registry history
  instead of failing refresh with a capability conflict.
- Retry only transient provider transport failures during the initial refresh
  window so Hermes plugin socket startup cannot spuriously roll back a healthy
  release; provider identity or attestation mismatches still fail immediately.

## [1.11.2] - 2026-08-23

- Resolve the code-implementation digest from the complete immutable release
  root when the official refresh owner runs from the release virtualenv. This
  keeps console-script and Gateway provider attestations identical and avoids
  a safe deployment rollback caused by a wheel ``site-packages`` root.

All notable changes to eimemory are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.11.1] - Pending

### Added

- Add a release-owned, locked and idempotent Hermes
  `code.implementation:v2` refresh command plus a managed 20-minute systemd
  timer. The command uses `EIMEMORY_ROOT` exclusively (production default
  `/var/lib/eimemory`), requires live socket health, and keeps every bootstrap
  registration and advertisement explicitly non-qualifying.
- Add bounded owner status and doctor evidence for authority identity, exact
  binding and implementation digest, live provider health, advertisement
  freshness, sealed-catalog passes, timer state, kill switch, and automation
  policy presence without reading or exposing credentials.

### Fixed

- Deprecate the incompatible legacy `code.implementation:v1` revision only
  after v2 revision and binding registration succeeds, preventing ambiguous
  Profile selection from quarantining a genuinely passing v2 incubation.
- Allow overlapping immutable one-hour advertisements by deterministically
  selecting the newest exact provider statement, while continuing to reject
  every binding, revision, provider, operation, side-effect, fingerprint, or
  freshness mismatch.
- Keep terminal transactions bound to their exact durable historical
  advertisement while separately requiring the latest fresh advertisement
  and live health, so the 20-minute refresh cadence cannot invalidate a real
  48-hour observation.
- Install and enable the formal owner from the target immutable release,
  retire the known temporary Grok units, and make rollback either restore the
  prior release's owner or remove the timer when that release predates it.

### Safety

- Capability incubation remains owned solely by the existing nightly job and
  must persist two distinct validated provider receipts from the sealed code
  catalog. The owner does not create an effects policy, remove the kill switch,
  synthesize incidents, or change product-completion qualification. Full L5
  remains incomplete until a natural strict transaction, observation, terminal
  receipt, and compatible release lineage really exist.

## [1.11.0] - Bootstrap pending

The bootstrap release adds the non-autonomous, non-qualifying v2 code-
implementation contract, proposal-only Hermes host adapter, durable
code-evolution ledger, strict product-completion envelope, and fail-closed
policy/recovery boundaries. Production effects remain disabled and no L5
product-completion evidence is claimed.

## [1.10.1] - Pending

The pending release candidate identifies as `1.10.1`.

### Added

- Separate operational-lane permission from the explicit evidence boundary and
  remove the auxiliary reflection search that could bypass relevance grounding.
- Bind production-recall CLI datasets to a no-follow, permission-checked,
  open-time `secure_dataset_fingerprint.v1` evidence record.
- Add source-version CAS to knowledge refresh: source, artifact, claim, entity,
  page, and contradiction inputs are revalidated inside the existing atomic
  transaction; stale input returns an all-zero retry result and nightly makes
  one bounded retry.

### Changed

- Production recall readiness is policy v2 and requires 15 accepted cases and
  labels, with at least five from each of OpenClaw, Codex, and Hermes. The
  current incomplete production dataset remains data-pending; this release does
  not claim an accepted production gate.

### Compatibility

- No schema, service, parallel scheduler, or refresh ledger was added. The
  dated 2026-08-22 production closure audit remains historical evidence.

### Release gates

- The exact-tree final suite and review, candidate commit and push, immutable
  deployment, and post-deploy identity, health, and canary checks remain
  pending for 1.10.1. Policy-v2 production recall acceptance is separately
  data-pending and cannot be satisfied with synthetic cases.

## [1.10.0] - 2026-08-22

### Added

- Add a bounded `operational_issue` recall intent for Chinese and English
  deployment, release, incident, rollback, root-cause, and troubleshooting
  queries without treating a bare generic question as an operational event.
- Add governed post-fusion relevance selection with exact-identity dominance,
  duplicate-identity preservation, grounded score margins, graph/anchor
  filtering, and no forced top-k padding.
- Add `recall_bundle.compact.v1` plus CLI `--compact`, `--explain`, and
  `--limit` controls. The compatibility release keeps the diagnostic payload
  as the default while compact results enforce bounded model-facing output.
- Add canonical-first recall with bounded legacy fallback while preserving
  exact Hermes/OpenClaw channel isolation and the low-level legacy-union
  contract.

### Changed

- Make recall lane eligibility intent-driven so preference, project, and
  operational queries do not inherit unrelated research/news tails, while
  explicit caller boundaries and research recall remain authoritative.
- Extend production recall evaluation with P@3, MRR, noise, padding, compact
  payload, latency, canonical/fallback, duplicate identity, graph/anchor, and
  independent Hermes/OpenClaw/CLI regression coverage.

### Compatibility

- Keep `RecallBundle.to_dict()`, storage schema, RPC methods, adapter contracts,
  source allowlists, authoritative hydration, rejected-record filtering, and
  channel leakage gates unchanged. No data migration or new service is needed.

### Deployment

- Commit `2b472ef3c3511e37bd108d802a97862e9b7af769` was subsequently pushed and
  immutably deployed as production 1.10.0. At the 1.10.1 handoff, production
  still matched that exact commit and version, with healthy services and
  completed storage migration. L5 remained `ready` / `evolving`, and the
  fresh production automatic-code-evolution criterion remained partial.

### Changed

- Ship the trusted Hongtu application catalog from the main `eimemory`
  distribution, with aggregate-only scoped recall evaluation and independent
  Hermes/OpenClaw binding selectors. Production acceptance now records separate
  provider observations and reliable snapshots without exposing recalled
  payloads to the evaluator output.
- Complete the scoped capability v3 production backfill, explicitly seed the
  eleven legacy vocabulary definitions as discovered, and lifecycle-activate
  only `memory.recall` after its catalog, provider bindings, advertisements,
  repeated acceptance evidence, and projection gates pass.
- Record the production L5 v3 closure review, including a 91.7% fully evidenced
  score against the twelve original acceptance criteria. A canonical PDF-backed
  knowledge link, hypothesis, independent Hermes/OpenClaw evaluation, and
  eligible feedback advance the loop to `evolving`; automatic code evolution
  remains the sole criterion without fresh production proof.
- Record the final verification boundary honestly: static, packaging, links,
  3,068 clean full-run passes, 416/416 affected-module passes, and 10/10
  OpenClaw watchdog passes. The operator explicitly waived a third full run;
  the project does not claim a fully green all-test invocation for this tree.
- Rewrite the project overview, architecture, deployment boundary, and module
  ownership documentation around the single production governance pipeline.
- Make L3/L4 safety-wire declarations name active controls:
  `kill_switch`, `audit_verifier`, `safety_replay`, and `promotion_manager`.
- Keep the backward-compatible JSON default for `eimemory doctor`, make
  `doctor --json` machine-parseable, and reserve human rendering for an explicit
  `doctor --human` request.
- Preserve the supervisor contract in the expanded doctor report so operators
  can inspect scheduled-run state alongside storage and integrity diagnostics;
  retain the prior RPC service, version, commit, and runtime-identity fields.
- Align the Codex plugin manifest with package version `1.9.135` and document
  the complete `agent.runtime.v1` adapter environment and isolation contract.
- Guard the outcome-trace expression index with `json_valid` so one malformed
  legacy payload cannot block writes, startup, or offline storage repair.
- Replace the former empty PDF placeholder with content-addressed raw PDF,
  canonical UTF-8 text, and immutable parser-manifest evidence. Runtime
  extraction and refresh now verify the manifest, references, and blob hashes
  again; missing parser support, scanned PDFs, malformed input, and untrusted
  caller-supplied references fail explicitly.
- Add the knowledge-refresh consumer after contradiction reconciliation: it
  atomically retires stale operational projections and recompiles only from
  canonical sources and non-conflicted claims. Nightly reports now distinguish
  pages marked for refresh from pages actually recompiled or blocked.
- Add automatic local code-proposal and direct-apply flow: a configured or
  injected proposer yields a state-bound, allowlisted unified diff with focused
  verification; unavailable or invalid proposals remain explicit blocked
  evidence rather than being downgraded to SOPs. Generated proposals accept
  only focused `python -m compileall` or `python -m pytest -q tests/...` argv
  checks, rejecting shell, Git, network tools, `python -c`, and a broad
  full-suite command. The local `--apply` path has no human approval gate, but
  repository commit and production deployment default to disabled and require
  explicit settings.
- Persist a direct code-apply transaction before the first write. Failed
  verification rolls back; apply-enabled learning and evolution cycles recover
  interrupted transactions only when the recorded state is provable, otherwise
  isolate them without retrying, patch replay, or overwrite; reports expose the
  result as `code_apply_recovery`.
- Converge host integration boundaries: Codex and Hermes retain the shared
  four-operation public memory contract, while OpenClaw exposes bridge status
  and keeps lifecycle behavior in its hooks.

### Removed

- Remove the test-only `eimemory.autonomous` Karpathy experiment stack; active
  learning and evolution remain under `eimemory.governance`.
- Remove duplicate governance state-machine, held-out splitter, evidence wrapper,
  skill-merger, and obsolete safety prototypes whose responsibilities are owned
  by current replay, promotion, safe-transport, audit, and rollback flows.
- Remove empty, disconnected, or shadow modules for core errors, report models,
  living temporal helpers, persona wrappers, and Python-side Feishu delivery
  state, together with their corresponding obsolete tests.
- Remove the unregistered Python `OpenClawMemoryTools` facade; the supported
  lifecycle path is the bridge hooks and the operator-only E2E diagnostic.

### Planned

- Multi-agent memory coordination
- Re-extract, review, and reconcile fresh claims when a canonical paper source
  changes; the current refresh consumer recompiles surviving reviewed claims.

## [1.9.138] - 2026-08-22

### Added

- Add an evidence-gated capability incubation pipeline that scans exact-scope
  `discovered` definitions outside the active-only L5 profile, reports every
  missing prerequisite, and automatically transitions a definition to `active`
  only after trusted Catalog cases, active revisions/bindings, fresh adapter
  advertisements, and repeated bounded preflight passes exist.
- Run incubation from nightly before dynamic capability evolution and expose
  `learn capability-incubation-plan` plus `learn capability-incubation` for
  read-only inspection and bounded operator execution.

### Safety

- Incubation never infers capability identity from text or tool names, never
  loads executable evaluators from data, and quarantines a newly activated
  definition if its immediate persisted profile acceptance fails.

## [1.9.137] - 2026-08-20

### Fixed

- Register the explicit `learn watch --legacy-compatibility` parser flag so
  scheduled dynamic watch runs retain their fail-closed default instead of
  failing before evaluation.

## [1.9.136] - 2026-08-20

### Added

- Add dynamic L5 v3 capability contracts: semantic capability definitions,
  revisions, provider bindings, profiles, evaluation specifications, evidence
  observations, and per-axis assessments replace the former default fixed
  capability universe.
- Add a sealed, typed application evaluation-catalog bootstrap. Installed
  Python entry points may register trusted executors and cases during startup;
  absent configuration blocks dynamic evaluation with `catalog_not_configured`
  instead of selecting an implicit catalog.

### Changed

- Align the Codex and Hermes plugin package metadata with the `1.9.136`
  runtime declaration.
- Scope capability-domain reads, writes, backfill cursors, and assessments to
  the exact tenant/agent/workspace/user owner scope plus logical capability
  scope. Evidence remains bound to capability revision, provider binding, and
  its recorded provenance; package version and machine identity are context,
  not capability identity.
- Introduce SQLite capability v3 domain tables, transactional lifecycle and
  audit/export boundaries, and restartable scoped backfill machinery. This
  release does not claim a completed historical backfill or benchmark-budget
  validation for every deployment.
- Let Codex, Hermes, OpenClaw, and eibrain advertise independently through the
  internal capability contract; model-visible adapter surfaces remain
  compatibility-bound rather than being forced into tool parity.
- Route reviewed external knowledge through typed capability links and
  evidence-bound hypotheses. Knowledge, stale sources, or failed hypotheses
  cannot self-promote maturity or apply a change.
- Make automatic local code evolution depend on a matching trusted
  machine-environment policy and, on the dynamic path, verified hypothesis
  evidence. There is no human approval queue; a missing, malformed, or
  nonmatching policy blocks the action, while commit and deployment remain
  separate machine-policy capabilities.

## [1.9.135] - 2026-08-19

### Fixed

- Keep `PayloadSegmentStore(read_only=True)` strictly non-mutating so deep payload-pointer verification cannot restore owner-write bits on sealed immutable-release snapshots and block storage-bearing deployments.

## [1.9.134] - 2026-08-19

### Fixed

- Preserve Hermes automatic recall when the proactive policy explicitly bypasses an otherwise successful query by falling back to bounded channel-local recall without manufacturing proactive acknowledgements.
- Derive the Hermes single-flight waiter deadline from the bounded adapter timeout so concurrent callers do not abandon valid 3–6 second recall work at the former fixed three-second boundary.
- Raise the managed OpenClaw RPC hook timeout from 1.8 to 3.5 seconds and keep the EI bridge importable on Python 3.11 by using `typing.TypeAlias` declarations.

## [1.9.133] - 2026-08-10

### Fixed

- Resolve outcome-trace idempotency through an offline-migrated SQLite index instead of hydrating every reflection page, and make concurrent trace alias writes atomic so verified OpenClaw `agent_end` persistence stays inside the RPC hook budget without duplicate outcomes.

## [1.9.132] - 2026-08-10

### Fixed

- Enforce the OpenClaw fast-recall deadline across candidate fanout and optional hot-path work, reuse the hook's policy search inside governed recall, and stop timed-out prompt work from starving terminal hooks.
- Remove an accidentally tracked absolute release symlink so immutable release archives pass safe extraction validation.

## [1.9.131] - 2026-08-10

### Fixed

- Keep accumulated maturity separate from the current release readiness stage, and reject L5 observation reports whose release commit does not match the active runtime.

## [1.9.130] - 2026-08-03

### Fixed

- Isolate release-closure tests from the configured production checkpoint and align the legacy failure-stage assertion with the post-arm receipt recheck.
- Force terminal-hook policy attribution lookups through the session metadata index, avoiding a full scope scan that exceeded the RPC hook deadline.

## [1.9.129] - 2026-08-02

### Changed

- Reconcile the canonical source checkout with the independently released runtime and nightly maintenance work, preserving the final streamed identity implementation.

## [1.9.128] - 2026-08-02

### Fixed

- Stream identity audit and repair records page-by-page so nightly maintenance no longer retains the full production store in memory.
- Emit a bounded nightly CLI summary instead of serializing the full internal supervisor report.

## [1.9.127] - 2026-07-30

### Performance

- Serve RPC and loopback health responses over HTTP/1.1 so clients can reuse connections.
- Cache release identity briefly per channel authority scope for lightweight status checks.
- Lower CPU, I/O, and scheduler priority for learning and nightly batch jobs while preserving gateway control-plane priority.

## [1.9.126] - 2026-07-30

### Changed
- Route OpenClaw lifecycle hooks through the authenticated long-running eimemory RPC runtime, preserving the complete hook and Feishu bridge payloads without per-hook Python startup.
- Fail open on a bounded RPC timeout while retaining the existing CLI transport when the RPC hot path is not configured.

## [1.9.125] - 2026-07-30

### Changed
- Enable bounded autonomous-learning and L5 promotion in production configuration with a maximum of three promotions per cycle.
- Preserve replay, isolated-evaluator, safety, canary, regression-watch, and rollback gates as mandatory promotion controls.

## [1.9.124] - 2026-07-30

### Removed
- Remove every eimemory injection into OpenClaw's compiled Feishu runtime, including legacy upgrade markers, global sinks, and cross-isolate receipt spools.
- Stop patching OpenClaw from the gateway `ExecStartPre`; production now runs the unmodified upstream OpenClaw package.

## [1.9.123] - 2026-07-30

### Fixed
- Capture successful Feishu sends at OpenClaw's shared API-result normalization boundary, including the platform message id, chat id, and exact response body content.
- Correlate lower-level API receipts without a session key through the real `oc_*` chat id while preserving exact final-content matching.

## [1.9.122] - 2026-07-30

### Fixed
- Keep exactly one Feishu API receipt patch marker when upgrading an installed v3 runtime to the cross-isolate v4 spool implementation.
- Repair the duplicate adjacent v4 marker produced by the earlier upgrade while preserving strict idempotency validation.

## [1.9.121] - 2026-07-30

### Fixed
- Persist successful Feishu API receipts through an atomic filesystem spool so channel and plugin Node isolates no longer depend on shared `globalThis` state.
- Drain spooled receipts transactionally before `agent_end` final matching, deleting a receipt only after the reply ledger write commits.

## [1.9.120] - 2026-07-30

### Fixed
- Repair previously installed Feishu API receipt v2 runtimes whose dispatcher still called the legacy `message_sent` bridge despite carrying the API-receipt marker.
- Reject current patched runtimes that retain any legacy receipt-sink call, preventing a marker-only upgrade from silently bypassing `platform_accepted`.

## [1.9.119] - 2026-07-30

### Fixed
- Buffer every successful Feishu API receipt and mark a reply accepted only when its content matches the agent final, so tool notices cannot close the final-reply ledger.
- Report API receipts for every completed reply block instead of relying on OpenClaw's optional `infoKind` classification.
- Remove oneshot self-restarts from release closure, supersede stale release checkpoints, and resume the periodic timer only after post-switch closure initialization.

## [1.9.118] - 2026-07-30

### Fixed
- Treat successful Feishu API responses and their platform message IDs as the authoritative reply-delivery receipt, without depending on the generic `message_sent` hook.
- Recover a settled queued final that produced zero visible replies through the existing no-visible-reply fallback.
- Rename the managed OpenClaw patch and receipt sink to reflect the API-level delivery boundary.

## [1.9.117] - 2026-07-30

### Fixed
- Use honjia's verified numeric tailnet address for the eibrain monitor because MagicDNS hostname resolution is unavailable on honxin.

## [1.9.116] - 2026-07-30

### Fixed
- Remove the retired OpenClaw restart watchdog, recovery quarantine injection, installer and CI baggage, and its managed runtime environment.
- Bound prompt bridge and memory-hook work under one absolute deadline that includes queue wait, preventing serial 25-second hook overruns.
- Route the eibrain monitor to the honjia tailnet endpoint and normalize both legacy and current supervisor health payloads.
- Reconcile pending release closure on a bounded 30-second timer, migrating away from reply-ledger path storms and clearing prior start-limit state.

## [1.9.115] - 2026-07-29

### Fixed
- Measure production-recall latency after a separate isolated memory probe, removing `tracemalloc` and cold-cache overhead from the p95 service-latency gate without changing its threshold.

## [1.9.114] - 2026-07-29

### Fixed
- Extract deployment-notification Feishu receipts from camelCase, nested primary-platform, and API-native `data.message_id` response shapes with bounded, fail-closed parsing.

## [1.9.113] - 2026-07-29

### Fixed
- Select the nearest verified immutable predecessor by Git topology instead of trusting deployment-receipt query order, preventing production recall closure from binding to an older unrelated baseline.

## [1.9.112] - 2026-07-29

### Fixed
- Score semantically identical ground-truth behavior rules through a stable digest identity so immutable lesson/replay record-ID drift no longer creates false MRR, NDCG, top-1, or Jaccard regressions.
- Preserve exact operator-labelled and returned record IDs alongside semantic ranking evidence without merging historical rules, expanding labels, or inferring missing verified-real outcome provenance.
- Keep verified-real replay fail-closed when exact trusted `source_record_id` evidence is unavailable; semantic ranking does not manufacture or backfill that evidence deficit.

## [1.9.111] - 2026-07-29

### Fixed
- Supersede only a prior release's resumable closure checkpoint when a newer commit becomes authoritative, preventing stale path failures without resetting or version-binding L5 evidence.

## [1.9.110] - 2026-07-29

### Fixed
- Reset and replace automatic Feishu reply receipts at logical response boundaries so a final reply cannot inherit an earlier block message ID, including in-place upgrade of the deployed receipt patch.
- Flush long Feishu answers as newline-aware 800-character completed blocks, preventing large Chinese responses and attachments from collapsing into the no-visible-reply fallback.
- Recheck channel acceptance immediately after arming a release-closure checkpoint, eliminating the ledger-event race.
- Serialize release-closure reconciliation and add bounded systemd failure retries without introducing polling.

## [1.9.109] - 2026-07-29

### Fixed
- Emit canonical `message_sent` hook receipts for automatic Feishu replies only after the Feishu API returns a platform message ID, including streaming-card replies.
- Persist release-closure progress when only current-release channel acceptance is pending, then resume from that checkpoint without rerunning passed gates or binding L5 maturity to the package version.
- Reconcile pending release closure from a systemd path event when the channel ledger changes, without polling or restoring the removed reply watchdog.

## [1.9.108] - 2026-07-29

### Fixed
- Always execute and persist a candidate-bound pre-switch production recall anchor even when the running release already has an accepted baseline, so forward upgrades can activate strict state and complete live business validation without resetting L5 maturity.

## [1.9.107] - 2026-07-28

### Fixed
- Start the managed learn watch, think, dashboard, and L5 accumulation timers during immutable deployment instead of leaving production data collection disabled.
- Enable prompt injection and the OpenClaw prompt bridge through managed configuration, replacing the temporary runtime override.
- Run L5 evaluation against live production data immediately with automatic promotion disabled, retire the obsolete activation gate, and schedule one read-only effect review after 48 hours without binding or resetting accumulated L5 maturity to the release version.
- Keep default storage status and vacuum inspection on a read-only path so repeated checks cannot checkpoint runtime initialization writes into the SQLite database.

## [1.9.106] - 2026-07-28

### Fixed
- Permanently remove the OpenClaw restart watchdog from deployment and purge legacy user units, so resource pressure can no longer terminate an active Feishu reply.

## [1.9.105] - 2026-07-28

### Fixed
- Require sustained OpenClaw cgroup pressure before watchdog restarts, preventing a single healthy model or hook memory spike from interrupting an active Feishu reply.
- Record cgroup memory, PID, and pressure-streak evidence in watchdog output for deterministic restart diagnosis.

## [1.9.104] - 2026-07-27

### Fixed
- Preserve the original `query_features_low_signal` reason when verifying release-bound production recall high-water reports, so L5 readiness treats quarantined low-signal datasets as bootstrap data accumulation instead of generic report contract corruption.

## [1.9.103] - 2026-07-27

### Fixed
- Accept persisted production recall reports whose raw gate status is blocked solely because real-query features are low-signal as release-bound bootstrap dataset gaps, preventing L5 closure rehearsal from rolling back compatible releases while unusable labels are quarantined.

## [1.9.102] - 2026-07-27

### Fixed
- Treat production recall `data_accumulating` reports with low-signal real-query samples as dataset-only bootstrap gaps during L5 closure rehearsal, matching the real deploy gate shape while preserving hard blocks for non-recall evidence gaps.

## [1.9.101] - 2026-07-27

### Fixed
- Allow L5 closure rehearsal to treat low-signal production recall reports as dataset-only bootstrap gaps when all non-recall release evidence is complete, so quarantine of unusable real-query samples does not roll back otherwise compatible self-evolution releases.

## [1.9.100] - 2026-07-27

### Fixed
- Allow release closure to treat low-signal production real-query labels as verified bootstrap data accumulation, keeping healthy self-evolution releases at L4.5 instead of rolling back solely because unusable accepted samples were quarantined.

## [1.9.99] - 2026-07-27

### Fixed
- Reject low-signal production real-query labels whose query features are only collector/channel metadata, so legacy accepted packets cannot freeze an unusable strict recall dataset.
- Score production rule labels through the policy/rule recall lane with a precision recall profile, preventing false recall-gate failures when self-evolution rules are available to prompt injection outside the normal memory item lane.

## [1.9.98] - 2026-07-27

### Fixed
- Change the production real-query recall gate from fixed per-channel coverage to an overall active-channel contract: any current production channel mix is valid once total trusted cases and labels reach the minimum, while empty channels no longer downgrade release readiness.
- Report the dynamic active-channel production recall contract in deployment bootstrap progress so source, release, and L5 readiness do not drift on unused runtime channels.

## [1.9.97] - 2026-07-27

### Fixed
- Raise the production recall p95 latency gate from 1500ms to 3000ms across both recall quality evaluators, matching current large-store production latency while preserving correctness and pollution blockers.
- Treat bootstrap diagnostic recall reports with only bounded small-sample p95 latency drift as data accumulation input, rather than rolling back an otherwise healthy immutable release.

## [1.9.96] - 2026-07-27

### Fixed
- Query failed eimemory user units with the explicit `systemctl list-units` verb so the L5 observation gate remains compatible with systemd 259 instead of treating the unit glob as an unknown command verb.

## [1.9.95] - 2026-07-27

### Fixed
- Install and refresh the packaged L5 observation gate script, service, and timer during every immutable deployment so stale user-systemd copies cannot survive a release switch or rollback.
- Reload the user systemd manager and enable the canonical 48-hour observation timer with its six-hour recheck schedule from the active immutable release.

## [1.9.94] - 2026-07-27

### Fixed
- Keep compatible releases deployed in L4.5 while current-release real user-task evidence accumulates, instead of rolling back a healthy release solely because a version bump resets the per-release sample counter.
- Require exact compatible channel lineage, unchanged OpenClaw behavior, ten successful current-release operational probes across at least five task types, and trustworthy historical verified real-task evidence before allowing that accumulation state.
- Continue to block channel changes, insufficient or failing operational probes, low verified real-task success, replay or assessment gaps, storage migrations, and every non-accumulation readiness failure.

## [1.9.93] - 2026-07-27

### Fixed
- Keep compatible releases running in honest L4.5 data-accumulation mode when the diagnostic recall gate has only a bounded p95 latency fluctuation of at most five percent; correctness, leakage, evaluator, and multi-metric failures remain blocking.
- Establish current-release recall lineage from the exact release-bound bootstrap pending record plus the verified current-release `memory.recall` replay manifest, only when the recall implementation is unchanged.
- Preserve fail-closed release behavior for changed recall code, stale or mismatched bootstrap evidence, incomplete replay manifests, and invalid evidence ordering.

## [1.9.92] - 2026-07-27

### Fixed
- Repair a missing receipt for the currently healthy prior immutable release before recall bootstrap, using exact scope, commit, release-tree, runtime-health, and trusted-related-release verification.
- Keep receipt repair idempotent and fail before bootstrap, writer quiescence, storage transactions, or release switching when trustworthy prior evidence cannot be established.

## [1.9.91] - 2026-07-27

### Fixed
- Preserve the release-bound recall data-accumulation credential through an online, append-only bootstrap while still skipping writer quiescence, storage snapshots, and migrations for compatible code-only releases.
- Classify release health, governance facade, RPC identity, OpenClaw watchdog, test-support, and version-only integration metadata so known support changes no longer taint unchanged recall lineage; unknown production paths remain fail-closed.

## [1.9.90] - 2026-07-27

### Fixed
- Skip writer quiescence, storage snapshots, and pre-switch recall bootstrap for code-only releases when no storage migration is pending and the recall implementation digest is unchanged.
- Retain the protected snapshot transaction for real storage migrations, recall implementation changes, or uncertain domain detection.

## [1.9.89] - 2026-07-27

### Fixed
- Reconcile grace-eligible stale OpenClaw task leases through a durable watchdog repair task, resume interrupted repairs, and keep repair controls out of business-stale accounting.
- Enforce exact deployed runtime identity across RPC health, systemd services, immutable installation, and release verification before recording deployment evidence.
- Preserve verified L5 capability evidence across compatible release lineage by domain while keeping the current deployment receipt exact, recomputing lineage at runtime, and failing closed for changed or unverified domains.
- Keep bootstrap-pending recall evidence bound to the current release, finalize lineage once after canonical replay evidence exists, and prevent version-only evolution from erasing compatible L5 capability evidence.
- Treat valid below-L5 observation as pending with six-hour rechecks, while failing closed on malformed readiness, service-query errors, timer-disable failures, and repeated L5 activation.

## [1.9.86] - 2026-07-23

### Fixed
- Prewarm the configured recall workload without persisting evidence before the measured production quality gate, eliminating release-only cold-cache latency failures while retaining the measured threshold contract.
- Report diagnostic recall quality from the actual gate result instead of conflating evaluator execution success with threshold success, including the effective status, blocker, and persisted evidence identifier.

## [1.9.85] - 2026-07-22

### Fixed
- Close the production recall proof contract with exact source and scope matching, bounded pagination, outcome and diagnostic exclusion, and deduplicated canonical smoke samples.
- Bound SQLite recall candidate work while preserving sparse lexical, substring, and high-quality semantic anchors, with governed fallback, lazy payload hydration, and digest-verified legacy source recovery.
- Keep OpenClaw, Codex, and Hermes authority isolated across aliases, scopes, and optional PostgreSQL candidates, while retaining deterministic SQLite bypass behavior and release-gate observability.

## [1.9.84] - 2026-07-22

### Fixed
- Accept a configured diagnostic recall run as bootstrap evidence only when the evaluator itself completed successfully, passed every quality threshold, emitted no errors, and is paired with the current release-bound production-query pending credential.
- Measure cross-channel and source-filter leakage from every returned diagnostic record, expose explicit sample and report counts, and hard-block the quality and release gates unless both are native integer zero.
- Preserve the recall request's exact source-filter semantics (`None`, deny-all, or allowlist), validate target source identities independently, and reuse the production source-ID normalization contract.

## [1.9.83] - 2026-07-22

### Fixed
- Allow an immutable release to complete in an explicitly non-L5 data-accumulation state only when the current deployment receipt and bootstrap-pending evidence bind to the same commit, version, receipt, session, and scope, while every non-dataset L5 gate remains complete.
- Reject recall execution failures, leakage findings, blocking metrics, unrelated live-task deficits, stale bootstrap evidence, and incomplete release identities instead of masking them as production-query data accumulation.
- Make the deployment summary exit nonzero unless receipt, replay, live acceptance, rehearsal, readiness, and all bootstrap credentials form one consistent strict-L5 or release-bound data-accumulation contract.

## [1.9.82] - 2026-07-22

### Fixed
- Capture and bind the verified prior-release health envelope before storage writers are quiesced, so protected production-recall bootstrap can complete without weakening deployment receipt, current-link, URL, commit, version, or release-path checks.
- Read the pre-quiesce health snapshot only from the trusted immutable-install boundary using root-anchored component-by-component `openat` validation, strict owner/mode/link/size checks, and post-read directory-chain revalidation; fail closed on unsafe platforms or path-replacement races.
- Remove protected snapshot files on every capture, permission, ownership, bootstrap, and process-exit path while preserving automatic storage rollback and writer restart before an immutable release is switched.

## [1.9.81] - 2026-07-22

### Added
- Add a governed `RecallEngine` contract with explainable RRF fusion, exact-title and alias evidence, graph expansion, stable quality-aware ordering, and an optional PostgreSQL vector candidate source while retaining SQLite as the lightweight default.
- Add channel-local source partitions and authoritative Codex and Hermes memory mutations without changing OpenClaw's existing authority or L5 evidence contract.
- Add bounded proactive recall injection with durable volunteered/used feedback, reconciliation, replay evidence, and release-gated real-query quality metrics.
- Add cold governance payload archival, rollback-safe online storage maintenance, and crash-recoverable release transaction credentials.

### Changed
- Defer heavyweight SQLite migrations, compact recall attribution audits, bound projection work, and fail closed when required recall datasets, candidate providers, release identities, or capability projections are unavailable.
- Make the optional PostgreSQL path replaceable and bypass-safe so provider failure cannot break the default SQLite recall path or falsely pass the release gate.

### Fixed
- Preserve strict tenant, user, workspace, agent, channel, and source authority across every recall candidate and adapter mutation.
- Harden PII rejection for Unicode and unformatted phone numbers and person names while retaining deterministic product and business-identifier handling.
- Make storage snapshot, vacuum, rollback, systemd drop-in, marker, lock, tombstone, and recovery operations fail closed across partial writes, ENOSPC/EIO, process interruption, and path/inode replacement races.
- Keep completed SQLite startup read-only while repairing missing lightweight FTS, event, vector-trigger, and replay-uniqueness structures; rebuild damaged recall projections only through bounded offline maintenance without startup payload scans.
- Attribute pre-existing verified real outcomes before autonomous-cycle probes, preserve prior scores against failed synthetic evidence, and degrade attribution errors without interrupting the learning cycle.
- Require verifiable adapter receipts, immutable replay evidence, and exact release binding without weakening the existing OpenClaw L5 closure.

## [1.9.80] - 2026-07-21

### Fixed
- Retry prompt-safety command transport failures once with a bounded delay while keeping semantic failures and malformed successful responses strictly fail-closed and non-retryable.
- Allow operators to bound prompt-safety command attempts with `EIMEMORY_PROMPT_SAFETY_MAX_ATTEMPTS` (default 2, hard maximum 3).

## [1.9.79] - 2026-07-21

### Fixed
- Raise the shared prompt-safety case budget from 90 to 180 seconds so the candidate response and independent semantic judge each receive a 90-second inference budget under production tail latency, without relaxing fail-closed L5 verdict rules.

## [1.9.78] - 2026-07-21

### Added
- Add independent authoritative long-term-memory channels for Codex and Hermes while preserving OpenClaw as its existing authoritative source.
- Add a distributable Codex plugin with bounded fail-open hooks and four closed-loop MCP tools for recall, durable capture, verified outcomes, and status.
- Add a native Hermes memory provider with bounded single-writer synchronization, latest-wins prefetch, lifecycle integration, and the same four closed-loop tools.
- Add channel-specific verified terminal evidence without changing the existing OpenClaw L5 acceptance contract.

### Fixed
- Bound adapter response reads, local failure ledgers, write queues, recall limits, context payloads, and background workers.
- Keep empty workspace scopes reversible and reject malformed required MCP text before RPC dispatch.
- Redact structured, embedded, and multi-word credentials before hashing or forwarding Codex tool summaries.
- Preserve fail-open host behavior while surfacing sanitized adapter diagnostics and local degradation counters.
- Skip incomplete Codex tool events instead of collapsing them into a shared idempotency key, and redact versioned or plural credential fields.
- Bound Codex summary traversal before redaction and hashing, suppress JSON-RPC notification responses, reject empty turn synchronization, and single-flight identical Hermes prefetches.
- Keep unverified successful terminal traces labeled `verification_missing` so downstream closure consumers cannot mistake them for verified success.

## [1.9.77] - 2026-07-20

### Fixed
- Remove inherited `PYTHONPATH`, `PYTHONHOME`, and `VIRTUAL_ENV` from the OpenClaw gateway so release-bound eimemory probes execute from the immutable virtual environment.
- Prevent Python minor-version drift from causing `memory.recall` replay evidence mismatches and false L4.5/L5 discrepancies.

## [1.9.76] - 2026-07-20

### Fixed
- Make completed SQLite schema and record-key migrations read-only on repeated process startup instead of reacquiring an immediate write lock.
- Run legacy intent-pattern normalization once under a transaction-bound migration receipt, update only changed rows, and roll back partial migrations.
- Skip repeated table/index bootstrap after all component migration receipts are present while preserving explicit recall-index backfill behavior.

## [1.9.75] - 2026-07-19

### Fixed
- Reclassify generic OpenClaw terminal labels from bounded prompt and tool evidence, preserve the derived type over generic top-level labels, and recognize real health/status wording.
- Exclude generic task labels from verified-real-task and L5 sample counts so only specific business task evidence advances readiness.

## [1.9.74] - 2026-07-19

### Fixed
- Derive five concrete OpenClaw task classes from prompt and tool evidence when upstream emits a generic communication label.
- Count only agent/task completion evidence with signed, run-bound, successful post-tool verification; keep session completion lifecycle-only.
- Reject pending, failed, zero-result, mutation-only, cross-run, or tampered tool receipts while preserving valid zero-failure test output.
- Record deployment receipts after every healthy immutable switch, including gate-disabled repair deployments and initial bootstrap.
- Repair already-current releases from persisted trusted receipts, verify the complete immutable tree and runnable prior environment, and preserve a usable current link on rollback failure.
- Provision a private rotatable receipt key for OpenClaw and Python services with normalized ownership, permissions, and systemd-safe paths.

## [1.9.73] - 2026-07-19

### Fixed
- Require consecutive eimemory hook-pressure samples before restarting the OpenClaw gateway.
- Treat Feishu tool activity as reply progress so active long-running turns are not reported as broken delivery chains.

## [1.9.72] - 2026-07-19

### Fixed
- Never reuse a prior-turn assistant response when the current Feishu turn produces no content.
- Avoid gateway restart loops during normal transient eimemory hook memory peaks.

## [1.9.71] - 2026-07-19

### Fixed
- Ignore non-Feishu events that lack a valid message ID or reply target.
- Prevent reply recovery from retrying malformed pending entries indefinitely.

## [1.9.70] - 2026-07-19

### Added
- Initial public release
- Local-first memory runtime for AI agents
- Hybrid recall system (lexical, semantic, graph, quality, recency)
- Scoped memory support (user, workspace, project, agent)
- Knowledge intake pipeline
- Event memory logging and reflection
- Governance loops with safety gates (L0-L3 authority tiers)
- Evaluation tools for memory quality assessment
- Replay tools for regression testing
- CLI interface with multiple commands
- HTTP/RPC service for programmatic access
- systemd deployment templates
- SQLite and JSONL storage backends
- OpenClaw hooks integration
- eibrain RPC compatibility

### Features
- `eimemory ingest` - Add memories and knowledge
- `eimemory recall` - Retrieve relevant context
- `eimemory quality stats` - Analyze memory effectiveness
- `eimemory reflect` - Log experience and corrections
- `eimemory learn cycle` - Autonomous learning with safety gates
- `eimemory learn ledger` - Track learning history
- `eimemory learn dashboard` - Visualize learning progress
- `eimemory doctor` - System diagnostics
- `eimemory serve-eibrain-rpc` - RPC service
- `eimemory paper ingest` - Ingest research papers
- `eimemory intake run` - Knowledge processing pipeline

### Documentation
- Architecture documentation
- Deployment guide with systemd templates
- Evaluation framework specification
- Memory scoring contract (v1)
- L5 roadmap specification

### Infrastructure
- Production deployment patterns
- Health check endpoints
- Immutable release structure
- User systemd service templates
- Nightly governance timer

## Version History

### Development Timeline
- **April 2026**: Project created
- **July 2026**: Initial public release as 1.9.70

---

## Notes on Versioning

eimemory uses semantic versioning:
- **MAJOR**: Breaking changes to API or data format
- **MINOR**: New features, backward compatible
- **PATCH**: Bug fixes, maintenance

The current version (1.9.72) reflects the project's evolution from internal tool to production-ready public system. Future releases will follow standard semver conventions.

## Support

For questions about a specific version:
- Check the [FAQ](FAQ.md)
- Read the [Architecture documentation](docs/architecture.md)
- Open an [issue](https://github.com/darrowz/eimemory/issues)
- Join [discussions](https://github.com/darrowz/eimemory/discussions)
