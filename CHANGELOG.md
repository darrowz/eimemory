# Changelog

All notable changes to eimemory are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Planned
- Multi-agent memory coordination

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
