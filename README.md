# eimemory

`eimemory` is a local-first memory and evolution runtime for OpenClaw and eibrain.

## Memory Boundary

`eimemory` is a memory system. It stores knowledge, recalls knowledge, and refines memory over time.
It does not own execution, task orchestration, or workflow control.

## Current Capabilities

- Unified record model for memory, incidents, feedback, rules, and replay
- JSONL append log plus SQLite materialized store
- Recall API for OpenClaw and eibrain consumers
- Evolution API for observe, feedback, review, promote, replay, and ROI
- OpenClaw hook shim, tool shim, and plugin manifest
- OpenClaw lifecycle bridge plugin plus `openclaw-hook` CLI bridge
- eibrain SDK, RPC bridge, and HTTP RPC server
- CLI for init, ingest, recall, export, import, and nightly jobs
- Conservative migration scanner/importer for markdown, JSONL, and SQLite sources
- Local vector-assisted hybrid retrieval layer
- Memory quality metadata, capture tiers, and quality-aware hybrid recall
- Lightweight reflection/operator commands for check, log, read, and stats
- Paper-first knowledge memory: source intake, extract, claim/entity/relation records, compiled knowledge pages, recall views, and contradiction-aware refresh signals

## Quick Start

```bash
python -m pip install -e .
eimemory init
eimemory ingest "Remember concise replies" --title "Concise"
eimemory recall "concise replies"
eimemory recall "compact retrieval" --view page_centered
eimemory quality stats
eimemory reflect check
eimemory reflect log reply-style "Forgot concise style" "Reply with one sentence"
```

## eibrain RPC Service

Start the eibrain-facing RPC boundary from the deployed runtime environment, not from a source checkout path:

```bash
EIMEMORY_ROOT=/var/lib/eimemory eimemory serve-eibrain-rpc --host 127.0.0.1 --port 8091
```

`eibrain` should connect to the running endpoint, for example `http://127.0.0.1:8091/`.
The integration contract is the endpoint address, not the repository location.

A production systemd template is available at `deploy/systemd/eimemory-rpc.service`.

## Memory Quality

New memory records carry deterministic quality metadata under `meta.quality`:
`importance`, `confidence`, `freshness`, `reuse_potential`, `salience_score`,
`quality_tier`, and `capture_decision`.

The quality tier is used to keep the long-term store useful:

- `rejected`: not persisted or excluded from recall when present in legacy data
- `candidate`: low-confidence memory that should not dominate recall
- `confirmed`: normal reusable memory
- `core`: high-salience memory that should be favored during recall

Hybrid recall now combines lexical, semantic/vector, graph, and quality signals.
Recall explanations include a `quality_summary` plus per-item scoring details so
operators can see why a memory was selected.
For user-scoped integrations, `user_id=""` records are treated as shared global
memory within the same tenant/agent/workspace, while other users' records remain
isolated.

Quality can be inspected from the CLI:

```bash
eimemory quality stats
```

Nightly jobs include a `memory_quality` report with tier distribution, average
salience, source counts, and memory type counts.

## Paper Knowledge Memory

Papers enter `eimemory` as source memory before becoming usable knowledge records.
The pipeline stays memory-only: it structures and recalls knowledge for consumers,
but does not control tasks.

```bash
eimemory paper ingest --arxiv-id 2501.12345 --title "Compact Retrieval" --abstract "Compact retrieval improves embodied response quality."
eimemory paper extract --paper-source-id <paper_source_id> --title "Compact Retrieval" --abstract "Compact retrieval improves embodied response quality." --body "Method: compact retrieval."
eimemory paper compile --paper-source-id <paper_source_id>
eimemory recall "compact retrieval" --view claim_centered
eimemory recall "compact retrieval synthesis" --view page_centered
```

External research sources are registered separately from fetched paper content:

```bash
eimemory source add --source-kind url --title "ChatPaper arXiv cs.AI" --uri "https://www.chatpaper.ai/zh/dashboard/arxiv/cs/AI" --tag chatpaper --tag arxiv --tag paper
eimemory source scan --persist
eimemory intake collect --source-kind url --fetch --persist
```

`source scan --persist` records that a source exists and has been scanned.
`intake collect --fetch --persist` fetches external items and persists them as
reviewable `knowledge_candidate` records. Nightly jobs run the conservative
closed loop: collect external sources, persist safe candidates, promote
paper-like candidates into paper knowledge objects, and project only high-value
operational knowledge into runtime memory.

Core records:

- `paper_source`: canonical source identity and provenance
- `paper_extract`: structured text extracted from one source
- `claim_card`: atomic evidence-backed knowledge
- `entity_record` and `relation_record`: graph-shaped context around claims
- `knowledge_page`: compiled paper/topic memory for longer-horizon reuse
- `recall_view`: memory-only output shape for task, research, mixed, contradiction, or freshness use

## OpenClaw QMD Compatibility

`eimemory` can expose a QMD-compatible command surface for OpenClaw's experimental
`memory.backend = "qmd"` path:

```json
{
  "memory": {
    "backend": "qmd",
    "qmd": {
      "command": "eimemory qmd"
    }
  }
}
```

The compatibility layer currently supports:

- `eimemory qmd collection list --json`
- `eimemory qmd collection add <path> --name <name> --mask <pattern>`
- `eimemory qmd collection remove <name>`
- `eimemory qmd update`
- `eimemory qmd embed`
- `eimemory qmd search|query|vsearch <query> --json -n <limit> [-c <collection>]`

Every exported memory write also materializes a markdown record under
`<EIMEMORY_ROOT>/qmd/records/`, so QMD collections can point at a clean markdown
tree instead of indexing `records.jsonl` directly.

## OpenClaw Lifecycle Bridge

`eimemory` also exposes a small stdin/stdout bridge for OpenClaw lifecycle hooks:

```bash
echo '{"agent_id":"main","workspace_id":"repo-x","message":{"content":"Remember concise replies"}}' | eimemory openclaw-hook message_received
```

The corresponding OpenClaw bridge assets live in `integrations/openclaw/eimemory-bridge/`
and forward `message_received`, `before_prompt_build`, and `agent_end` into the
local runtime.

The OpenClaw adapter applies memory hygiene before persistence or injection:
low-value chatter, wrapper-only messages, prompt-injection-like inputs, malformed
hook output, and model thinking traces are filtered by default. Explicit
`capture_memory=true` or `captureMemory=true` can still force intentional capture.

## Migration

`eimemory` can screen legacy sources before importing them:

```bash
eimemory migrate scan /path/to/legacy-memory
eimemory migrate import /path/to/legacy-memory --candidate-id md-1
```

Supported sources:

- markdown and plain text files
- JSONL record logs
- SQLite sources with `records` or OpenClaw-style `files/chunks` tables

Only candidates that pass the conservative screen are imported by default.

## Layout

- `eimemory/` package source
- `integrations/openclaw/` plugin metadata
- `examples/standalone/` basic usage example
- `docs/` architecture and platform notes
