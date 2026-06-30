# Graph-first Coding Memory Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship eimemory 1.7.6 as a graph-first coding memory contract with stable external tools and a closed observe -> graph -> recall -> replay loop.

**Architecture:** Add a focused `eimemory.governance.coding_memory_contract` module that turns coding observations into typed graph episodes, exposes graph path queries, and runs a deterministic replay gate over those paths. Runtime, RPC, and OpenClaw tools delegate to that module while preserving compatibility with existing ingest/event APIs.

**Tech Stack:** Python 3.11+, existing `RuntimeStore`, SQLite `memory_edges`, `RecordEnvelope`, `MemoryEdge`, pytest.

---

### Task 1: Coding Observation Contract

**Files:**
- Create: `eimemory/governance/coding_memory_contract.py`
- Modify: `eimemory/api/runtime.py`
- Test: `tests/test_coding_memory_contract.py`

- [ ] **Step 1: Write failing tests**

Create tests that call `runtime.observe_coding_memory()` with a coding session containing agent, project, files, tools, commands, errors, decisions, outcomes, and replay cases. Assert it persists one `task_episode` memory, writes typed graph node ids, and writes relation-specific edges such as `TOUCHED_FILE`, `FAILED_WITH`, `DECIDED_BECAUSE`, `VERIFIED_BY`, and `PREVENTED_BY_REPLAY`.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest -q tests/test_coding_memory_contract.py::test_memory_observe_projects_coding_session_to_typed_graph`

Expected: FAIL because `Runtime.observe_coding_memory` does not exist.

- [ ] **Step 3: Implement minimal observation module**

Implement `observe_coding_memory(runtime, observation, scope)` to validate dict input, normalize typed node ids, persist a `memory` record with `memory_type="coding_session"` and `report_type="coding_observation"`, and upsert typed `MemoryEdge` rows using existing edge types with relation metadata.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest -q tests/test_coding_memory_contract.py::test_memory_observe_projects_coding_session_to_typed_graph`

Expected: PASS.

### Task 2: Graph Query Contract

**Files:**
- Modify: `eimemory/governance/coding_memory_contract.py`
- Modify: `eimemory/api/runtime.py`
- Test: `tests/test_coding_memory_contract.py`

- [ ] **Step 1: Write failing tests**

Add a test that observes a coding session, then calls `runtime.query_coding_memory_graph(query="sqlite disk I/O error", scope=scope)`. Assert the response includes `paths`, `evidence_refs`, and relation names connecting error -> decision -> file -> replay/outcome.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest -q tests/test_coding_memory_contract.py::test_memory_graph_returns_evidence_paths_for_coding_query`

Expected: FAIL because graph query method does not exist.

- [ ] **Step 3: Implement graph query**

Implement `query_coding_memory_graph(runtime, query, scope, limit)` by reusing recall to find candidate coding observation records, then loading adjacent `memory_edges` and returning typed path objects with evidence ids and relation metadata.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest -q tests/test_coding_memory_contract.py::test_memory_graph_returns_evidence_paths_for_coding_query`

Expected: PASS.

### Task 3: Graph Replay Gate

**Files:**
- Modify: `eimemory/governance/coding_memory_contract.py`
- Modify: `eimemory/api/runtime.py`
- Test: `tests/test_coding_memory_contract.py`

- [ ] **Step 1: Write failing tests**

Add a test that calls `runtime.run_coding_graph_replay()` for a stored graph path. Assert a valid path passes, missing relation evidence fails, and a replay result record is persisted when requested.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest -q tests/test_coding_memory_contract.py::test_graph_replay_gate_persists_pass_or_fail_evidence`

Expected: FAIL because replay method does not exist.

- [ ] **Step 3: Implement replay gate**

Implement `run_coding_graph_replay(runtime, query, expected_relations, scope, persist)` to call graph query, verify each expected relation appears in at least one path, compute pass rate, and optionally persist a `replay_result` with `report_type="coding_graph_replay"`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest -q tests/test_coding_memory_contract.py::test_graph_replay_gate_persists_pass_or_fail_evidence`

Expected: PASS.

### Task 4: Stable External Tool Surface

**Files:**
- Modify: `eimemory/adapters/eibrain/rpc.py`
- Modify: `eimemory/adapters/openclaw/tools.py`
- Test: `tests/test_adapters.py`

- [ ] **Step 1: Write failing tests**

Add adapter tests for `memory.observe`, `memory.graph`, `memory.replay`, and `memory.audit`. Assert old methods still work and new methods return contract-versioned responses.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest -q tests/test_adapters.py::test_eibrain_rpc_exposes_graph_first_memory_contract tests/test_adapters.py::test_openclaw_tools_expose_stable_graph_first_memory_contract`

Expected: FAIL because methods are not exposed.

- [ ] **Step 3: Implement adapters**

Map `memory.observe` to `Runtime.observe_coding_memory`, `memory.graph` to `Runtime.query_coding_memory_graph`, `memory.replay` to `Runtime.run_coding_graph_replay`, and `memory.audit` to a read-only summary over health/version/ledger-facing graph records.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest -q tests/test_adapters.py::test_eibrain_rpc_exposes_graph_first_memory_contract tests/test_adapters.py::test_openclaw_tools_expose_stable_graph_first_memory_contract`

Expected: PASS.

### Task 5: Version, Docs, and Verification

**Files:**
- Modify: `pyproject.toml`
- Modify: `eimemory/version.py`
- Modify: `tests/test_version.py`
- Modify: `docs/architecture.md`

- [ ] **Step 1: Bump version**

Set package version to `1.7.6`.

- [ ] **Step 2: Update architecture docs**

Document the graph-first coding memory contract and stable external tool surface.

- [ ] **Step 3: Run focused verification**

Run: `python -m pytest -q tests/test_coding_memory_contract.py tests/test_adapters.py::test_eibrain_rpc_exposes_graph_first_memory_contract tests/test_adapters.py::test_openclaw_tools_expose_stable_graph_first_memory_contract tests/test_version.py`

Expected: PASS.

- [ ] **Step 4: Run compile check**

Run: `python -m compileall -q eimemory`

Expected: exit code 0.
