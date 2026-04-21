# eimemory Knowledge Engine Design

**Status:** Approved design direction

**Goal:** Extend `eimemory` from a runtime memory/evolution system into a paper-first compiled knowledge system that can both accumulate research knowledge over time and serve that knowledge during OpenClaw and `eibrain` runtime tasks.

**Primary Outcome:** Knowledge must be able to enter the system, be structured and refined, and then become directly usable in task-time recall, operator workflows, and long-horizon research synthesis.

## Context

The current `eimemory` platform already provides:

- local-first append + materialized storage
- runtime ingest/recall APIs
- evolution feedback, rule, replay, and ROI loops
- OpenClaw lifecycle hooks and QMD integration
- hybrid local retrieval with lightweight vector assist
- conservative migration/import tooling

What it does not yet provide is a dedicated research knowledge pipeline. Today, papers can only be treated as generic memory inputs. That is insufficient for the intended `ei` use case because:

- raw papers are too large and noisy for runtime recall
- knowledge should accumulate instead of being re-derived from sources repeatedly
- long-term synthesis and task-time reuse require different representations
- contradictions, scope conditions, and operational value need explicit modeling

## Product Intent

This extension must satisfy two equally important jobs:

1. **Task-time utility**
   OpenClaw and `eibrain` should be able to retrieve actionable knowledge during real tasks.

2. **Long-horizon knowledge accumulation**
   The system should continuously absorb papers into an interlinked knowledge base that compounds over time.

The design therefore cannot be "just RAG" and cannot be "just a wiki". It must combine both.

## Recommendation

Adopt a **dual-engine compiled knowledge architecture**:

- `knowledge engine` handles source intake, parsing, extraction, compilation, and synthesis
- `memory engine` handles runtime recall and operational use
- `evolution engine` handles feedback, contradiction management, promotion/demotion, and synthesis refresh

The key design choice is:

> Papers do not enter runtime memory directly.
> Papers first become structured knowledge objects.
> Only the operationally useful subset is projected into runtime memory.

## Architecture

### 1. Source Layer

Immutable source intake for papers and adjacent research materials.

Supported input classes in first phase:

- `arxiv_id`
- `doi`
- `paper_url`
- `pdf_file`

Future-compatible but not first priority:

- OpenAlex search results
- Semantic Scholar metadata
- author watchlists
- RSS/news research feeds

Responsibilities:

- fetch metadata
- store raw source files and normalized text
- fingerprint source identity
- retain provenance and retrieval metadata

This layer is append-oriented and should remain immutable after ingestion except for metadata enrichment.

### 2. Extraction Layer

Transforms normalized paper text into structured research objects.

Extraction targets:

- paper metadata
- section map
- core claims
- methods
- assumptions
- limitations
- experimental findings
- benchmarks/datasets
- entities and keywords
- cited and compared methods
- open questions

This layer is not yet "knowledge". It is a structured interpretation of one source.

### 3. Knowledge Layer

Compiles extracted source material into durable knowledge assets.

Primary knowledge objects:

- `paper_page`
- `concept_page`
- `method_page`
- `comparison_page`
- `question_page`
- `synthesis_page`
- `claim_card`

The most important split is:

- `claim_card`: atomic, evidence-backed, runtime-friendly
- `knowledge_page`: synthesized, cross-linked, long-horizon, human-and-agent readable

This layer is the Karpathy-style accumulation layer. It is where the system learns.

### 4. Operational Projection Layer

Projects selected knowledge into the existing `eimemory` runtime surface.

Projection targets:

- runtime `memory` records
- reusable patterns
- task hints
- rule candidates
- retrieval boosts

Only knowledge with high operational value should be projected aggressively, but the intake side can remain broad.

This is what makes the knowledge actually usable by OpenClaw and `eibrain`.

## Core Principle

The system should be **wide on intake, selective on use**.

The user explicitly chose:

- dual-layer consumption (`claim cards` + `knowledge pages`)
- broad intake
- paper-first source priority

This means the design should tolerate more initial material entering the knowledge engine, while runtime consumers remain protected by retrieval policy, projection policy, and feedback loops.

## Data Model

Add the following first-class object kinds.

### `paper_source`

Immutable canonical source object.

Fields:

- `paper_source_id`
- `source_kind` (`arxiv`, `doi`, `pdf`, `url`)
- `title`
- `authors`
- `abstract`
- `venue`
- `published_at`
- `doi`
- `arxiv_id`
- `canonical_url`
- `pdf_blob_ref`
- `normalized_text_ref`
- `source_hash`
- `provenance`

### `paper_extract`

Structured extraction result for one source.

Fields:

- `paper_extract_id`
- `paper_source_id`
- `section_map`
- `claims`
- `methods`
- `assumptions`
- `limitations`
- `results`
- `datasets`
- `metrics`
- `entities`
- `open_questions`
- `extraction_model`
- `extraction_version`

### `claim_card`

Atomic, evidence-backed knowledge unit.

Fields:

- `claim_card_id`
- `statement`
- `claim_type`
- `evidence_refs`
- `source_paper_ids`
- `concept_refs`
- `method_refs`
- `scope_conditions`
- `limitations`
- `confidence`
- `freshness`
- `contradiction_refs`
- `utility_tags`
- `operationalizable`

### `knowledge_page`

Compiled multi-source page.

Fields:

- `knowledge_page_id`
- `page_type`
- `title`
- `summary`
- `sections`
- `supporting_claim_ids`
- `related_page_ids`
- `open_question_ids`
- `contradiction_ids`
- `last_compiled_at`
- `compile_version`

### `projection_record`

Tracks movement from knowledge layer into runtime layer.

Fields:

- `projection_record_id`
- `source_kind`
- `source_id`
- `target_record_id`
- `projection_type`
- `projection_reason`
- `projection_score`
- `last_projected_at`

## File and Module Structure

Recommended additions:

```text
eimemory/
├── intake/
│   └── papers/
│       ├── sources.py
│       ├── fetchers.py
│       ├── normalize.py
│       ├── pdf_parse.py
│       └── metadata.py
├── knowledge/
│   ├── compiler.py
│   ├── extract.py
│   ├── claims.py
│   ├── pages.py
│   ├── contradictions.py
│   ├── synthesis.py
│   └── projectors.py
├── scheduler/
│   └── papers.py
└── models/
    ├── paper_sources.py
    ├── paper_extracts.py
    ├── claim_cards.py
    ├── knowledge_pages.py
    └── projection_records.py
```

## Ingestion Flow

Phase 1 flow:

`paper input -> normalize -> persist source -> parse text -> extract -> compile -> project -> index`

Detailed behavior:

1. **Normalize**
   Convert incoming `arxiv_id`, `doi`, `url`, or `pdf` into a canonical `paper_source`.

2. **Persist Source**
   Save metadata, raw file, canonical text, and identity hash.

3. **Extract**
   Generate a `paper_extract` with structured research elements.

4. **Compile Knowledge**
   Create/update:
   - one `paper_page`
   - zero or more `concept_page`
   - zero or more `method_page`
   - zero or more `comparison_page`
   - zero or more `question_page`
   - one or more `claim_card`

5. **Project Operational Value**
   Select knowledge that can help during runtime and convert it into runtime-accessible records.

6. **Index**
   Make claim cards and pages retrievable through the existing store/retrieval infrastructure.

## Runtime Consumption Design

Runtime should not treat all knowledge equally.

### Task Recall Path

When OpenClaw or `eibrain` is solving a task:

1. search `claim_card` first
2. expand through related `knowledge_page` when needed
3. fall back to runtime `memory`
4. fall back to original paper only when traceability is required

Goal:

- optimize for fast, actionable recall
- minimize raw-paper noise

### Research Answer Path

When the user asks for explanation, comparison, or synthesis:

1. search `knowledge_page`
2. attach supporting `claim_card`
3. optionally attach `paper_source`

Goal:

- optimize for synthesis, not just snippets

## Retrieval Policy

Retrieval should become route-aware.

Suggested route policy:

- `task_type in runtime/agent/workflow` -> claim-first
- `task_type in research/explain/compare/summarize` -> page-first
- `task_type unknown` -> mixed retrieval with rerank

Claim cards should expose:

- actionability
- scope conditions
- limitations
- provenance

This prevents the system from overusing catchy but brittle findings.

## Evolution Integration

The knowledge engine must plug into the existing `evolution engine`.

Required feedback loops:

### 1. Runtime Usage Feedback

Track which claim cards are actually used in tasks and whether they helped.

### 2. Contradiction Tracking

When a new paper conflicts with existing claims or pages:

- mark contradiction edges
- lower operational confidence
- trigger page refresh

### 3. Promotion / Demotion

Frequently useful claim cards should become high-priority runtime knowledge.
Rarely useful or contradicted claim cards should be demoted.

### 4. Synthesis Refresh

When a topic accumulates enough new evidence, trigger recompilation of affected pages.

This is the mechanism that turns knowledge accumulation into knowledge improvement.

## Why This Is Better Than Plain Wiki or Plain RAG

### Versus plain RAG

- knowledge compounds over time
- contradictions and scope can be tracked
- task-time runtime gets distilled knowledge instead of full-document fragments

### Versus plain wiki

- knowledge is available by API, not only by browsing files
- runtime projection makes it directly useful in live tasks
- evolution loops decide what should stay operationally prominent

## Out of Scope for First Implementation

To keep phase 1 focused, exclude:

- general news-first pipelines
- broad multimodal paper figure understanding
- full citation graph analytics
- public static wiki site generation
- collaborative multi-user editing workflows

These can come later, but they should not block the paper-first compiled knowledge core.

## Risks

### 1. Extraction Overreach

If extraction tries to fully understand every paper on first pass, the pipeline becomes brittle.

Mitigation:

- start with conservative structured extraction
- keep provenance on every claim
- allow uncertain claims to stay low-confidence

### 2. Runtime Pollution

Broad intake can flood runtime memory if projection is too loose.

Mitigation:

- separate knowledge compilation from runtime projection
- keep projection scored and reversible

### 3. Knowledge Rot

Pages can become stale if sources accumulate without recompilation.

Mitigation:

- synthesis refresh jobs
- contradiction signals
- freshness metadata

## Success Criteria

This design is successful when:

- papers can be ingested through canonical paper-first inputs
- raw sources remain traceable and immutable
- extracted knowledge compiles into durable pages and claim cards
- OpenClaw and `eibrain` can recall actionable claims during tasks
- research queries can be answered from synthesized pages
- new evidence can refine, contradict, or promote existing knowledge

## Deployment Shape

This capability belongs inside `eimemory`, not as a Honxin-only add-on.

Deployment target:

- a new clean server running the unified `eimemory` project
- Honxin remains a consumer and test environment, not the canonical home of the feature

That preserves:

- one system of truth
- one deployment artifact
- multiple consumers

## Final Decision

Proceed with **Option C: dual-engine compiled knowledge architecture**.

This is the only option that satisfies both:

- "knowledge must be learned into the system"
- "knowledge must be usable in live runtime tasks"

Any simpler approach would optimize only one side of that requirement.
