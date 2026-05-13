# eimemory Memory Scoring Contract v1

## Purpose

`eimemory` already has useful scoring signals, including `meta.quality`,
`salience_score`, recall `final_score`, source quality, and evaluation metrics.
The gap is that these signals are scattered across ingestion, storage, recall,
governance, and evaluation.

This document defines a unified scoring contract for all memory records and
memory candidates. The goal is to make scoring reusable, explainable,
auditable, and compatible with existing data.

The v1 contract should become the shared scoring language for:

- deciding whether a memory should be captured;
- ranking memories during recall;
- explaining why a record was selected or suppressed;
- reporting memory health and quality distribution;
- evaluating retrieval quality with repeatable metrics;
- giving later training/replay pipelines consistent labels.

## Standards References

This design borrows from three stable external practices:

1. NIST TREC relevance judgments and qrels
   - Reference: https://trec.nist.gov/data/reljudge_eng.html
   - Use in `eimemory`: normalize relevance into explicit grades, track expected
     results in evaluation datasets, and keep retrieval metrics such as MRR and
     precision@k.

2. W3C PROV-O provenance model
   - Reference: https://www.w3.org/TR/prov-o/
   - Use in `eimemory`: attach provenance to each score so operators can tell
     which entity, activity, source, and generated artifact produced the score.

3. DCMI Metadata Terms
   - Reference: https://www.dublincore.org/specifications/dublin-core/dcmi-terms/
   - Use in `eimemory`: align stable descriptive metadata such as `title`,
     `subject`, `type`, `source`, `created`, `modified`, and `provenance` with
     memory labels and score explanations.

These references are not copied as schemas. They define the vocabulary and
evaluation habits that `eimemory` adapts into local-first Python data contracts.

## Design Principles

1. One score contract, many callers.
   The same `MemoryScore` shape should be usable by ingestion, recall,
   evaluation, repair, and governance.

2. Compatibility first.
   Existing `meta.quality` remains valid. The new contract reads old quality
   fields and writes a richer score under a new key.

3. Scores are explainable.
   Every final score includes component scores, weights, labels, and provenance.

4. Scores are bounded.
   All component scores use `0.0` to `1.0`. Penalties also use `0.0` to `1.0`
   and are subtracted through a declared formula.

5. Labels are stable.
   Labels use namespaced dot notation so downstream tools can filter without
   parsing free-form explanation text.

6. Retrieval and retention are separate decisions.
   Capture scoring decides whether to store or demote a memory. Recall scoring
   decides how useful a stored memory is for a query.

## Target Package Layout

```text
eimemory/scoring/
  __init__.py
  contract.py        MemoryScore, ScoreComponent, ScoreProvenance, ScoreContext
  labels.py          Stable label constants and helpers
  evaluator.py       Deterministic scoring engine
  adapters.py        Compatibility adapters for meta.quality and recall reports
  thresholds.py      Tier thresholds and default weights
  reports.py         Summary helpers for governance/evaluation reports
```

The first implementation should keep the code standard-library only, matching
the rest of `eimemory`.

## Storage Contract

The canonical v1 score is stored at:

```python
record.meta["scoring"]["memory_score_v1"]
```

Existing compatibility metadata remains at:

```python
record.meta["quality"]
```

The legacy `quality` object is not removed in v1. It is derived from, or adapted
to, the new score contract so existing APIs, CLI commands, and tests keep
working.

## Core Data Model

### MemoryScore

```python
MemoryScore(
    schema_version="memory_score.v1",
    final_score=0.0,
    tier="candidate",
    components={...},
    labels=[...],
    explanation={...},
    provenance=ScoreProvenance(...),
)
```

Required fields:

- `schema_version`: fixed string, `memory_score.v1`
- `final_score`: bounded float from `0.0` to `1.0`
- `tier`: one of `rejected`, `candidate`, `confirmed`, `core`
- `components`: mapping of component name to `ScoreComponent`
- `labels`: stable namespaced labels
- `explanation`: compact, serializable reasons and formula details
- `provenance`: score generation metadata aligned with PROV-O concepts

### ScoreComponent

```python
ScoreComponent(
    name="relevance",
    value=0.0,
    weight=0.0,
    evidence={...},
)
```

Required component fields:

- `name`: stable dimension name
- `value`: bounded float from `0.0` to `1.0`
- `weight`: bounded float from `0.0` to `1.0`
- `evidence`: serializable evidence used by this component

### ScoreProvenance

The provenance object maps to PROV-O concepts without requiring RDF:

```python
ScoreProvenance(
    entity_id=record_id,
    activity="memory.score",
    agent="eimemory.scoring.v1",
    source="runtime.ingest",
    generated_at=now_iso(),
    inputs=[...],
)
```

Required provenance fields:

- `entity_id`: record id or candidate id being scored
- `activity`: score activity, such as `memory.score`, `memory.recall_score`, or
  `memory.backfill_score`
- `agent`: scoring implementation name/version
- `source`: source subsystem, such as `runtime.ingest`, `sqlite.recall`, or
  `quality.repair`
- `generated_at`: ISO timestamp
- `inputs`: compact references to source text, legacy quality, task context, or
  recall query

## Scoring Dimensions

v1 defines seven dimensions.

### relevance_score

Meaning: How useful this record is for the current query or task.

Reference habit: TREC relevance judgments.

Capture-time default:

- If no query exists, use a neutral value derived from content specificity.

Recall-time behavior:

- Use lexical, semantic, vector, source, and task-context evidence.
- Map graded relevance labels as:
  - `0`: not relevant
  - `1`: partially relevant
  - `2`: highly relevant

Stable labels:

- `relevance.none`
- `relevance.partial`
- `relevance.high`

### confidence_score

Meaning: How certain and internally stable the memory appears.

Evidence:

- explicit `confidence`;
- uncertainty phrases;
- contradiction metadata;
- verified/manual source markers;
- successful recall or replay evidence.

Stable labels:

- `confidence.low`
- `confidence.medium`
- `confidence.high`

### salience_score

Meaning: Long-term importance of the memory.

Compatibility:

- Existing `meta.quality.salience_score` maps directly into this dimension.

Evidence:

- memory type;
- preference/rule/decision markers;
- user confirmation;
- durable project or identity relevance.

Stable labels:

- `salience.low`
- `salience.medium`
- `salience.high`

### freshness_score

Meaning: Whether time improves or weakens the memory.

Rules:

- Recent episodic observations get a freshness boost.
- Durable preferences, rules, identities, and architecture decisions decay
  slowly or not at all.
- News and paper intake records can decay faster unless promoted into compiled
  knowledge.

Stable labels:

- `freshness.recent`
- `freshness.stable`
- `freshness.stale`

### provenance_score

Meaning: Trustworthiness of the origin and generation path.

Reference habit: W3C PROV-O and DCMI `source` / `provenance` metadata.

Source guidance:

- user-confirmed memory: high
- first-party runtime memory: medium to high
- tool-generated memory with successful outcome: medium to high
- migration import: medium until repaired or confirmed
- external scrape/intake: medium or lower until promoted
- malformed or unknown origin: low

Stable labels:

- `provenance.user_confirmed`
- `provenance.first_party`
- `provenance.tool_generated`
- `provenance.external_source`
- `provenance.migration`
- `provenance.unknown`

### reuse_score

Meaning: How reusable the memory is across future tasks.

High reuse examples:

- preferences;
- rules;
- architecture decisions;
- stable identity facts;
- reusable operational lessons.

Low reuse examples:

- wrapper-only chat;
- one-off acknowledgements;
- thin status messages;
- unstructured noise.

Stable labels:

- `reuse.low`
- `reuse.medium`
- `reuse.high`

### risk_penalty

Meaning: How much risk should reduce the final score.

Risk evidence:

- prompt injection indicators;
- contradiction metadata;
- mojibake or malformed text;
- duplicate memory;
- privacy-sensitive content without explicit capture;
- unsupported source or unknown provenance;
- failed validation.

Stable labels:

- `risk.none`
- `risk.duplicate`
- `risk.conflict`
- `risk.injection_suspected`
- `risk.malformed`
- `risk.privacy_sensitive`
- `risk.unknown_source`

## Default Formula

The default v1 final score is:

```text
base_score =
  relevance_score   * 0.22 +
  confidence_score  * 0.16 +
  salience_score    * 0.22 +
  freshness_score   * 0.08 +
  provenance_score  * 0.14 +
  reuse_score       * 0.18

final_score = clamp(base_score - risk_penalty * 0.35)
```

Reasoning:

- Salience and relevance matter most because memory must be both important and
  useful.
- Reuse is high because `eimemory` should favor durable memory over noise.
- Provenance is explicit because trust must be auditable.
- Freshness is lower-weighted because some high-value memories are intentionally
  long-lived.
- Risk penalty is separated so unsafe or polluted memory can be suppressed even
  when it looks relevant.

Recall-time scoring may override weights through a named scoring profile, but
the output must still use the same `MemoryScore` contract.

## Tier Thresholds

```text
0.00 - 0.24  rejected
0.25 - 0.49  candidate
0.50 - 0.74  confirmed
0.75 - 1.00  core
```

Tier labels:

- `lifecycle.rejected`
- `lifecycle.candidate`
- `lifecycle.confirmed`
- `lifecycle.core`

Capture decision compatibility:

- `rejected` maps to `capture_decision = "reject"`
- `candidate`, `confirmed`, and `core` map to `capture_decision = "accept"`

## Label Namespace

Labels must be machine-filterable and stable.

Required namespaces:

```text
relevance.*
confidence.*
salience.*
freshness.*
provenance.*
reuse.*
risk.*
lifecycle.*
memory.*
dcmi.*
prov.*
```

Examples:

```text
memory.preference
memory.rule
memory.episodic
memory.knowledge
dcmi.type.memory
dcmi.source.runtime
prov.activity.memory.score
prov.agent.eimemory.scoring.v1
```

The `dcmi.*` and `prov.*` labels are local labels that align with the external
vocabularies. They are not intended to be complete RDF serializations.

## Compatibility Mapping

### Legacy quality to MemoryScore

Existing legacy field:

```python
record.meta["quality"] = {
    "importance": 0.7,
    "confidence": 0.8,
    "freshness": 1.0,
    "reuse_potential": 0.6,
    "salience_score": 0.7,
    "quality_tier": "confirmed",
    "capture_decision": "accept",
}
```

v1 mapping:

```text
importance        -> salience evidence
confidence        -> confidence_score
freshness         -> freshness_score
reuse_potential   -> reuse_score
salience_score    -> salience_score
quality_tier      -> tier hint
capture_decision  -> lifecycle/risk hint
```

### MemoryScore to legacy quality

For existing consumers:

```python
record.meta["quality"] = {
    "importance": memory_score.components["salience"].value,
    "confidence": memory_score.components["confidence"].value,
    "freshness": memory_score.components["freshness"].value,
    "reuse_potential": memory_score.components["reuse"].value,
    "salience_score": memory_score.components["salience"].value,
    "quality_tier": memory_score.tier,
    "capture_decision": "reject" if memory_score.tier == "rejected" else "accept",
}
```

## Integration Points

### Record creation

`RecordEnvelope.create()` should continue accepting existing inputs. The scoring
adapter can compute `memory_score_v1` and legacy `quality` together.

No caller should be forced to provide a score manually.

### Runtime ingest

`Runtime.memory.ingest()` should use the scoring evaluator to decide capture
tier. Existing `force_capture` keeps its meaning and is recorded as provenance
evidence.

### SQLite recall

`SQLiteStore.recall()` should replace local inline scoring math with a scoring
adapter over time.

The recall explanation should include:

- `scoring_version`;
- per-item `memory_score`;
- component scores;
- final score;
- labels;
- provenance summary.

The existing `scored_items` shape remains available for compatibility.

### Evolution API

`memory_quality_report()` and `repair_memory_quality()` should become scoring
contract consumers.

Reports should include:

- count by tier;
- average final score;
- average component scores;
- missing score count;
- high-risk label counts;
- provenance source distribution.

### Evaluation framework

`run_evaluation()` should keep `pass_rate`, `mrr`, and `precision_at_k`, then add
score-aware reporting:

- `mean_final_score`;
- `mean_relevance_score`;
- `high_relevance_hit_rate`;
- `nDCG_like` for graded expected relevance;
- label distribution for misses.

The evaluation dataset should accept optional graded relevance:

```json
{
  "query": "reply style",
  "expect": [
    {"title": "Reply Style", "relevance": 2},
    {"title": "Old weak note", "relevance": 1}
  ]
}
```

## Scoring Profiles

The first implementation should include three named profiles:

### balanced

Default profile. Used for general recall and existing behavior.

### precision

Favors confidence, provenance, and risk suppression. Good for high-stakes
policy, identity, or long-lived preferences.

### exploration

Allows more candidate memories and lower provenance confidence. Good for
research, paper intake, and knowledge discovery.

Profiles change weights but not the output schema.

## API Examples

### Capture score

```python
score = evaluate_memory_score(
    text="User prefers concise spoken replies.",
    title="Reply Style",
    memory_type="preference",
    source="eibrain.audio_dialogue",
    context=ScoreContext(activity="runtime.ingest"),
)
```

### Recall score

```python
score = evaluate_recall_score(
    record=record,
    query="how should I reply",
    lexical_score=1.0,
    semantic_score=0.42,
    vector_score=0.51,
    context=ScoreContext(activity="sqlite.recall", profile="balanced"),
)
```

### Backfill score

```python
score = score_from_legacy_quality(
    record=legacy_record,
    activity="quality.repair",
)
```

## Testing Requirements

The scoring layer needs focused tests before it is wired deeply into runtime:

- Contract serialization and deserialization.
- Score clamping for invalid numbers.
- Legacy `meta.quality` to `MemoryScore` mapping.
- `MemoryScore` to legacy `meta.quality` mapping.
- Tier threshold boundaries.
- Risk penalty suppression.
- Provenance fields are always present.
- Label namespace stability.
- Recall explanation includes score components.
- Evaluation supports graded relevance.
- Existing `quality stats` and `quality repair` behavior stays compatible.

## Migration Strategy

1. Add scoring package and tests.
2. Make `evaluate_memory_quality()` delegate to scoring while preserving its
   return shape.
3. Add `meta.scoring.memory_score_v1` during new record creation.
4. Add repair/backfill support for existing records.
5. Add recall explanation fields without removing old fields.
6. Gradually move inline recall scoring into the scoring evaluator.
7. Extend evaluation datasets with graded relevance.

No one-step destructive migration is allowed in v1.

## Non-Goals

- No external scoring service.
- No required LLM scoring dependency.
- No database schema migration required for the first scoring package.
- No removal of `meta.quality`.
- No complete RDF or JSON-LD export in v1.

## Acceptance Criteria

The v1 design is complete when:

- `eimemory.scoring` exposes a stable contract.
- New memory records can carry `meta.scoring.memory_score_v1`.
- Existing `meta.quality` remains populated.
- Recall explanations can show both legacy scores and `MemoryScore`.
- Quality stats and repair commands remain compatible.
- Evaluation can report both classic retrieval metrics and score-aware metrics.
- Tests prove old data and new score objects work together.

