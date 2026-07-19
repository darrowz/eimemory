# Changelog

All notable changes to eimemory are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Planned
- Enhanced semantic recall with vector embeddings
- Multi-agent memory coordination
- Advanced rollback strategies
- Performance optimizations for large memory stores

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
