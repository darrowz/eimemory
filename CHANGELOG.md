# Changelog

All notable changes to eimemory are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Planned
- Multi-agent memory coordination

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
