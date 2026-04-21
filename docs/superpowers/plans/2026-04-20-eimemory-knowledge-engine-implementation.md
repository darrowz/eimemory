# eimemory Knowledge Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a paper-first compiled knowledge subsystem to `eimemory` so the memory system can absorb papers into durable structured memory objects and serve those objects through recall views without becoming an execution system.

**Architecture:** Extend `eimemory` with five memory layers: source memory, structured memory, compiled memory, recall views, and memory evolution. Papers flow through `source -> extract -> claim/entity/relation -> page -> recall_view`, while the existing runtime APIs and retrieval stack are upgraded to consume claim-centered and page-centered memory outputs. This plan keeps `eimemory` strictly as a memory system: it stores, compiles, recalls, and evolves memory, but never owns task execution or orchestration.

**Tech Stack:** Python 3.13, existing `eimemory` runtime/store APIs, SQLite + JSONL, local embedding helper, pytest

---

## File Structure

### New package areas

- Create: `C:\Users\maiph\Desktop\hypermemory\eimemory\intake\__init__.py`
- Create: `C:\Users\maiph\Desktop\hypermemory\eimemory\intake\papers\__init__.py`
- Create: `C:\Users\maiph\Desktop\hypermemory\eimemory\intake\papers\sources.py`
- Create: `C:\Users\maiph\Desktop\hypermemory\eimemory\intake\papers\normalize.py`
- Create: `C:\Users\maiph\Desktop\hypermemory\eimemory\intake\papers\metadata.py`
- Create: `C:\Users\maiph\Desktop\hypermemory\eimemory\intake\papers\pdf_parse.py`
- Create: `C:\Users\maiph\Desktop\hypermemory\eimemory\knowledge\__init__.py`
- Create: `C:\Users\maiph\Desktop\hypermemory\eimemory\knowledge\extract.py`
- Create: `C:\Users\maiph\Desktop\hypermemory\eimemory\knowledge\claims.py`
- Create: `C:\Users\maiph\Desktop\hypermemory\eimemory\knowledge\pages.py`
- Create: `C:\Users\maiph\Desktop\hypermemory\eimemory\knowledge\relations.py`
- Create: `C:\Users\maiph\Desktop\hypermemory\eimemory\knowledge\views.py`
- Create: `C:\Users\maiph\Desktop\hypermemory\eimemory\knowledge\compiler.py`

### New model files

- Create: `C:\Users\maiph\Desktop\hypermemory\eimemory\models\paper_sources.py`
- Create: `C:\Users\maiph\Desktop\hypermemory\eimemory\models\paper_extracts.py`
- Create: `C:\Users\maiph\Desktop\hypermemory\eimemory\models\claim_cards.py`
- Create: `C:\Users\maiph\Desktop\hypermemory\eimemory\models\entity_records.py`
- Create: `C:\Users\maiph\Desktop\hypermemory\eimemory\models\relation_records.py`
- Create: `C:\Users\maiph\Desktop\hypermemory\eimemory\models\knowledge_pages.py`
- Create: `C:\Users\maiph\Desktop\hypermemory\eimemory\models\recall_views.py`

### Existing files to extend

- Modify: `C:\Users\maiph\Desktop\hypermemory\eimemory\models\records.py`
- Modify: `C:\Users\maiph\Desktop\hypermemory\eimemory\storage\sqlite_store.py`
- Modify: `C:\Users\maiph\Desktop\hypermemory\eimemory\storage\runtime_store.py`
- Modify: `C:\Users\maiph\Desktop\hypermemory\eimemory\api\runtime.py`
- Modify: `C:\Users\maiph\Desktop\hypermemory\eimemory\api\memory.py`
- Modify: `C:\Users\maiph\Desktop\hypermemory\eimemory\api\evolution.py`
- Modify: `C:\Users\maiph\Desktop\hypermemory\eimemory\cli\main.py`
- Modify: `C:\Users\maiph\Desktop\hypermemory\eimemory\scheduler\jobs.py`
- Modify: `C:\Users\maiph\Desktop\hypermemory\README.md`

### Test files

- Create: `C:\Users\maiph\Desktop\hypermemory\tests\test_paper_intake.py`
- Create: `C:\Users\maiph\Desktop\hypermemory\tests\test_knowledge_models.py`
- Create: `C:\Users\maiph\Desktop\hypermemory\tests\test_knowledge_compiler.py`
- Create: `C:\Users\maiph\Desktop\hypermemory\tests\test_recall_views.py`
- Modify: `C:\Users\maiph\Desktop\hypermemory\tests\test_runtime.py`
- Modify: `C:\Users\maiph\Desktop\hypermemory\tests\test_storage.py`
- Modify: `C:\Users\maiph\Desktop\hypermemory\tests\test_platform.py`

## Phase Plan

### Phase 0: Guardrails and Shared Vocabulary

**Outcome:** Lock the memory-only boundary into code and tests so future implementation does not drift into execution concerns.

**Files:**
- Modify: `C:\Users\maiph\Desktop\hypermemory\eimemory\models\records.py`
- Modify: `C:\Users\maiph\Desktop\hypermemory\README.md`
- Create: `C:\Users\maiph\Desktop\hypermemory\tests\test_knowledge_models.py`

- [ ] **Step 1: Write failing tests for new memory object kinds**

```python
from eimemory.models.records import VALID_KINDS


def test_knowledge_memory_kinds_are_registered() -> None:
    assert "paper_source" in VALID_KINDS
    assert "paper_extract" in VALID_KINDS
    assert "claim_card" in VALID_KINDS
    assert "entity_record" in VALID_KINDS
    assert "relation_record" in VALID_KINDS
    assert "knowledge_page" in VALID_KINDS
    assert "recall_view" in VALID_KINDS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_knowledge_models.py::test_knowledge_memory_kinds_are_registered -v`
Expected: FAIL because the new kinds are not yet part of the shared record vocabulary.

- [ ] **Step 3: Add the shared kind registry update**

Update `eimemory\models\records.py` so the record envelope accepts the new memory kinds while keeping them explicitly under memory scope, not execution scope.

- [ ] **Step 4: Add boundary language to the README**

Add a short section clarifying:

- `eimemory` is a memory system
- it stores and recalls knowledge
- it does not own execution or orchestration

- [ ] **Step 5: Run focused tests**

Run: `python -m pytest tests/test_knowledge_models.py tests/test_models.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add eimemory/models/records.py README.md tests/test_knowledge_models.py
git commit -m "feat: add knowledge memory kind vocabulary"
```

### Phase 1: Source Memory Layer for Papers

**Outcome:** Support canonical paper intake into immutable source memory objects.

**Files:**
- Create: `C:\Users\maiph\Desktop\hypermemory\eimemory\models\paper_sources.py`
- Create: `C:\Users\maiph\Desktop\hypermemory\eimemory\intake\papers\sources.py`
- Create: `C:\Users\maiph\Desktop\hypermemory\eimemory\intake\papers\normalize.py`
- Create: `C:\Users\maiph\Desktop\hypermemory\eimemory\intake\papers\metadata.py`
- Create: `C:\Users\maiph\Desktop\hypermemory\tests\test_paper_intake.py`

- [ ] **Step 1: Write failing tests for canonical paper source normalization**

```python
from eimemory.intake.papers.normalize import normalize_paper_input


def test_normalize_arxiv_input_to_paper_source_payload() -> None:
    payload = normalize_paper_input({"arxiv_id": "2501.12345"})
    assert payload["source_kind"] == "arxiv"
    assert payload["arxiv_id"] == "2501.12345"


def test_normalize_pdf_input_to_paper_source_payload(tmp_path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")
    payload = normalize_paper_input({"pdf_file": str(pdf_path)})
    assert payload["source_kind"] == "pdf"
    assert payload["pdf_path"] == str(pdf_path)
```

- [ ] **Step 2: Run tests to verify failure**

Run: `python -m pytest tests/test_paper_intake.py::test_normalize_arxiv_input_to_paper_source_payload tests/test_paper_intake.py::test_normalize_pdf_input_to_paper_source_payload -v`
Expected: FAIL because the intake modules do not exist yet.

- [ ] **Step 3: Implement `PaperSource` model and normalization helpers**

Implement:

- canonical source kind detection
- stable source hash creation
- normalized source payload generation
- minimal metadata placeholders for future enrichers

- [ ] **Step 4: Add runtime ingestion entrypoint for paper sources**

Expose a runtime method that persists a `paper_source` record through the existing store without yet compiling claims/pages.

- [ ] **Step 5: Run focused tests**

Run: `python -m pytest tests/test_paper_intake.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add eimemory/models/paper_sources.py eimemory/intake/__init__.py eimemory/intake/papers/__init__.py eimemory/intake/papers/sources.py eimemory/intake/papers/normalize.py eimemory/intake/papers/metadata.py eimemory/api/runtime.py tests/test_paper_intake.py
git commit -m "feat: add canonical paper source intake"
```

### Phase 2: Structured Memory Layer

**Outcome:** Parse paper text into structured memory objects: extracts, claims, entities, and relations.

**Files:**
- Create: `C:\Users\maiph\Desktop\hypermemory\eimemory\models\paper_extracts.py`
- Create: `C:\Users\maiph\Desktop\hypermemory\eimemory\models\claim_cards.py`
- Create: `C:\Users\maiph\Desktop\hypermemory\eimemory\models\entity_records.py`
- Create: `C:\Users\maiph\Desktop\hypermemory\eimemory\models\relation_records.py`
- Create: `C:\Users\maiph\Desktop\hypermemory\eimemory\intake\papers\pdf_parse.py`
- Create: `C:\Users\maiph\Desktop\hypermemory\eimemory\knowledge\extract.py`
- Create: `C:\Users\maiph\Desktop\hypermemory\eimemory\knowledge\claims.py`
- Create: `C:\Users\maiph\Desktop\hypermemory\eimemory\knowledge\relations.py`
- Create: `C:\Users\maiph\Desktop\hypermemory\tests\test_knowledge_models.py`

- [ ] **Step 1: Write failing tests for structured extraction output**

```python
from eimemory.knowledge.extract import extract_paper_memory


def test_extract_paper_memory_returns_claims_entities_and_relations() -> None:
    result = extract_paper_memory(
        title="Test Paper",
        abstract="This paper shows compact retrieval improves embodied response quality.",
        body="Method: compact retrieval. Limitation: tested only on one robot."
    )
    assert result.claims
    assert result.entities
    assert result.relations
```

- [ ] **Step 2: Run test to verify failure**

Run: `python -m pytest tests/test_knowledge_models.py::test_extract_paper_memory_returns_claims_entities_and_relations -v`
Expected: FAIL because the extraction pipeline is missing.

- [ ] **Step 3: Implement conservative extraction**

Implement a deterministic first-pass extractor that:

- builds a `paper_extract`
- extracts sentence-level claims conservatively
- derives named entity-like records from titles and repeated capitalized tokens
- builds simple typed relations such as `supports`, `mentions`, and `limited_by`

Avoid LLM-heavy logic in phase 2; keep it testable and deterministic.

- [ ] **Step 4: Persist structured objects through the shared store**

Extend the runtime/store surface so structured memory objects are stored as first-class records with provenance.

- [ ] **Step 5: Run focused tests**

Run: `python -m pytest tests/test_knowledge_models.py tests/test_storage.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add eimemory/models/paper_extracts.py eimemory/models/claim_cards.py eimemory/models/entity_records.py eimemory/models/relation_records.py eimemory/intake/papers/pdf_parse.py eimemory/knowledge/extract.py eimemory/knowledge/claims.py eimemory/knowledge/relations.py eimemory/storage/sqlite_store.py eimemory/storage/runtime_store.py tests/test_knowledge_models.py tests/test_storage.py
git commit -m "feat: add structured paper memory extraction"
```

### Phase 3: Compiled Memory Layer

**Outcome:** Compile extracted paper memory into durable pages for long-horizon knowledge accumulation.

**Files:**
- Create: `C:\Users\maiph\Desktop\hypermemory\eimemory\models\knowledge_pages.py`
- Create: `C:\Users\maiph\Desktop\hypermemory\eimemory\knowledge\pages.py`
- Create: `C:\Users\maiph\Desktop\hypermemory\eimemory\knowledge\compiler.py`
- Create: `C:\Users\maiph\Desktop\hypermemory\tests\test_knowledge_compiler.py`

- [ ] **Step 1: Write failing tests for page compilation**

```python
from eimemory.knowledge.compiler import compile_paper_knowledge


def test_compile_paper_knowledge_creates_paper_and_topic_pages() -> None:
    result = compile_paper_knowledge(
        paper_title="Compact Retrieval for Embodied Agents",
        claims=["Compact retrieval reduces prompt noise."],
        entities=["Embodied agents", "Compact retrieval"],
    )
    page_types = {page.page_type for page in result.pages}
    assert "paper" in page_types
    assert "topic" in page_types
```

- [ ] **Step 2: Run test to verify failure**

Run: `python -m pytest tests/test_knowledge_compiler.py::test_compile_paper_knowledge_creates_paper_and_topic_pages -v`
Expected: FAIL because the compiler and page models do not yet exist.

- [ ] **Step 3: Implement page compilation rules**

Implement:

- `paper_page` creation for each source
- `topic_page` compilation for repeated entities/topics
- claim-backed page summaries
- page linkage through supporting claim IDs

Do not add advanced contradiction merging yet; that belongs in phase 5.

- [ ] **Step 4: Add compiler entrypoint to runtime**

Expose a runtime method that:

- accepts a persisted `paper_source`
- loads or receives its extraction result
- writes compiled `knowledge_page` records

- [ ] **Step 5: Run focused tests**

Run: `python -m pytest tests/test_knowledge_compiler.py tests/test_runtime.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add eimemory/models/knowledge_pages.py eimemory/knowledge/pages.py eimemory/knowledge/compiler.py eimemory/api/runtime.py tests/test_knowledge_compiler.py tests/test_runtime.py
git commit -m "feat: add compiled knowledge pages"
```

### Phase 4: Recall View Layer

**Outcome:** Add memory-only recall views so consumers can use the same knowledge system in different ways without coupling to execution.

**Files:**
- Create: `C:\Users\maiph\Desktop\hypermemory\eimemory\models\recall_views.py`
- Create: `C:\Users\maiph\Desktop\hypermemory\eimemory\knowledge\views.py`
- Modify: `C:\Users\maiph\Desktop\hypermemory\eimemory\api\memory.py`
- Create: `C:\Users\maiph\Desktop\hypermemory\tests\test_recall_views.py`
- Modify: `C:\Users\maiph\Desktop\hypermemory\tests\test_runtime.py`

- [ ] **Step 1: Write failing tests for task-oriented and research-oriented recall views**

```python
from eimemory.models.recall_views import build_claim_centered_view, build_page_centered_view


def test_claim_centered_view_prioritizes_claim_cards() -> None:
    view = build_claim_centered_view(
        claims=[{"statement": "Compact retrieval reduces prompt noise."}],
        pages=[{"title": "Compact retrieval topic"}],
    )
    assert view.view_type == "claim_centered"
    assert view.items[0]["kind"] == "claim_card"


def test_page_centered_view_prioritizes_knowledge_pages() -> None:
    view = build_page_centered_view(
        claims=[{"statement": "Compact retrieval reduces prompt noise."}],
        pages=[{"title": "Compact retrieval topic"}],
    )
    assert view.view_type == "page_centered"
    assert view.items[0]["kind"] == "knowledge_page"
```

- [ ] **Step 2: Run tests to verify failure**

Run: `python -m pytest tests/test_recall_views.py -v`
Expected: FAIL because the view builders do not exist yet.

- [ ] **Step 3: Implement recall view builders**

Implement memory-only views for:

- `claim_centered`
- `page_centered`
- `mixed`
- `contradiction`
- `freshness`

The builders must only organize memory outputs. They must not encode execution decisions or workflow control.

- [ ] **Step 4: Extend `MemoryAPI.recall`**

Route recall into:

- claim-first view for task-oriented contexts
- page-first view for research/explain/summarize contexts
- mixed view when no route hint is available

- [ ] **Step 5: Run focused tests**

Run: `python -m pytest tests/test_recall_views.py tests/test_runtime.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add eimemory/models/recall_views.py eimemory/knowledge/views.py eimemory/api/memory.py tests/test_recall_views.py tests/test_runtime.py
git commit -m "feat: add memory recall views"
```

### Phase 5: Memory Evolution for Knowledge

**Outcome:** Let the memory system refine itself through contradiction tracking, promotion/demotion, and page refresh without becoming an execution policy engine.

**Files:**
- Modify: `C:\Users\maiph\Desktop\hypermemory\eimemory\api\evolution.py`
- Modify: `C:\Users\maiph\Desktop\hypermemory\eimemory\scheduler\jobs.py`
- Modify: `C:\Users\maiph\Desktop\hypermemory\eimemory\knowledge\compiler.py`
- Create: `C:\Users\maiph\Desktop\hypermemory\tests\test_knowledge_compiler.py`
- Modify: `C:\Users\maiph\Desktop\hypermemory\tests\test_evolution_layer.py`

- [ ] **Step 1: Write failing tests for contradiction and refresh behavior**

```python
def test_new_conflicting_claim_marks_contradiction_and_triggers_refresh(runtime) -> None:
    first = runtime.knowledge.add_claim_card(
        statement="Method A improves accuracy.",
        scope={"agent_id": "main", "workspace_id": "papers"},
    )
    second = runtime.knowledge.add_claim_card(
        statement="Method A does not improve accuracy.",
        scope={"agent_id": "main", "workspace_id": "papers"},
    )
    report = runtime.evolution.reconcile_knowledge(scope={"agent_id": "main", "workspace_id": "papers"})
    assert report["contradiction_count"] >= 1
    assert report["page_refresh_count"] >= 1
```

- [ ] **Step 2: Run test to verify failure**

Run: `python -m pytest tests/test_evolution_layer.py::test_new_conflicting_claim_marks_contradiction_and_triggers_refresh -v`
Expected: FAIL because knowledge reconciliation is missing.

- [ ] **Step 3: Implement knowledge evolution helpers**

Implement:

- contradiction edge creation
- claim confidence demotion
- page refresh candidate generation
- nightly compile refresh summary

Do not add automatic deletion of memory objects. Only mark, demote, or recompile.

- [ ] **Step 4: Add nightly summary coverage**

Extend nightly jobs to report:

- paper source count
- claim card count
- knowledge page count
- contradiction count
- refreshed page count

- [ ] **Step 5: Run focused tests**

Run: `python -m pytest tests/test_evolution_layer.py tests/test_knowledge_compiler.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add eimemory/api/evolution.py eimemory/scheduler/jobs.py eimemory/knowledge/compiler.py tests/test_evolution_layer.py tests/test_knowledge_compiler.py
git commit -m "feat: add knowledge memory evolution"
```

### Phase 6: CLI, Documentation, and Integration Surface

**Outcome:** Expose the new memory capability cleanly for operators, imports, and downstream consumers.

**Files:**
- Modify: `C:\Users\maiph\Desktop\hypermemory\eimemory\cli\main.py`
- Modify: `C:\Users\maiph\Desktop\hypermemory\README.md`
- Modify: `C:\Users\maiph\Desktop\hypermemory\tests\test_platform.py`

- [ ] **Step 1: Write failing CLI tests for paper ingestion and recall views**

```python
def test_cli_supports_paper_ingest_and_research_recall(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path / "runtime"))
    assert cli_main(["paper", "ingest", "--arxiv-id", "2501.12345"]) == 0
    assert cli_main(["recall", "compact retrieval", "--view", "page_centered"]) == 0
```

- [ ] **Step 2: Run test to verify failure**

Run: `python -m pytest tests/test_platform.py::test_cli_supports_paper_ingest_and_research_recall -v`
Expected: FAIL because the CLI has no paper subcommands or view selection.

- [ ] **Step 3: Extend the CLI**

Add:

- `eimemory paper ingest`
- `eimemory paper extract`
- `eimemory paper compile`
- `eimemory recall --view claim_centered|page_centered|mixed`

- [ ] **Step 4: Update operator documentation**

Document:

- what this subsystem is
- what it is not
- paper-first workflow
- recall view differences
- expected new server deployment shape

- [ ] **Step 5: Run full test suite**

Run: `python -m pytest tests -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add eimemory/cli/main.py README.md tests/test_platform.py
git commit -m "feat: expose paper knowledge memory workflows"
```

## Deployment Stages

### Stage A: Local Development Baseline

Ship phases 0 through 2 locally and verify:

- canonical paper source intake
- structured extraction persistence
- no runtime regression in existing tests

### Stage B: Knowledge Compilation Baseline

Ship phase 3 and verify:

- paper pages compile successfully
- topic pages are generated from repeated paper entities

### Stage C: Recall View Usability

Ship phase 4 and verify:

- task-oriented recall returns claim-centered views
- research-oriented recall returns page-centered views

### Stage D: Knowledge Evolution Stability

Ship phase 5 and verify:

- contradiction handling
- refresh loops
- nightly summaries

### Stage E: New Server Deployment

Ship phase 6 to a clean new server and verify:

- CLI workflows
- OpenClaw consumer compatibility
- `eibrain` consumer compatibility
- storage portability

## Verification Checklist

Before declaring the feature ready:

- `paper_source`, `paper_extract`, `claim_card`, `entity_record`, `relation_record`, `knowledge_page`, and `recall_view` all persist through the shared store
- task-time recall can return claim-centered memory views
- research-time recall can return page-centered memory views
- contradiction handling adjusts memory reliability without deleting evidence
- the subsystem remains memory-only and introduces no execution orchestration logic
- full test suite passes

## Self-Review

Spec coverage check:

- source memory layer: phases 1 and 6
- structured memory layer: phase 2
- compiled memory layer: phase 3
- recall view layer: phase 4
- memory evolution layer: phase 5
- new server deployment shape: deployment stages section

Placeholder scan:

- no `TODO`
- no `TBD`
- every phase has concrete files, commands, and expected outcomes

Type consistency check:

- `paper_source`, `paper_extract`, `claim_card`, `entity_record`, `relation_record`, `knowledge_page`, `recall_view` are used consistently across phases
- recall view names are consistently `claim_centered`, `page_centered`, `mixed`, `contradiction`, `freshness`

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-04-20-eimemory-knowledge-engine-implementation.md`.

Two execution options:

1. **Subagent-Driven (recommended)** - dispatch a fresh subagent per phase or per task, with review between steps
2. **Inline Execution** - execute the phases in this session in order with checkpoints
