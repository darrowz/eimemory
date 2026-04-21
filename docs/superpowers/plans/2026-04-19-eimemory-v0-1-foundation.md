# EIMemory v0.1 Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a fresh `eimemory` v0.1 foundation package with unified models, local storage, recall API, and initial OpenClaw plus eibrain adapters.

**Architecture:** `eimemory` is a package-first memory and evolution core. Persistent state uses a JSONL append log plus a SQLite materialized store, while runtime consumers call a stable Python API for ingest, recall, observe, feedback, and active policy lookup.

**Tech Stack:** Python 3.14, pytest, sqlite3, json, dataclasses, pathlib

---

## File Structure

- Create: `docs/superpowers/plans/2026-04-19-eimemory-v0-1-foundation.md`
- Create: `pyproject.toml`
- Create: `eimemory/__init__.py`
- Create: `eimemory/version.py`
- Create: `eimemory/core/ids.py`
- Create: `eimemory/core/clock.py`
- Create: `eimemory/core/errors.py`
- Create: `eimemory/models/records.py`
- Create: `eimemory/models/reports.py`
- Create: `eimemory/config/defaults.py`
- Create: `eimemory/storage/jsonl.py`
- Create: `eimemory/storage/sqlite_store.py`
- Create: `eimemory/storage/runtime_store.py`
- Create: `eimemory/api/memory.py`
- Create: `eimemory/api/evolution.py`
- Create: `eimemory/api/runtime.py`
- Create: `eimemory/adapters/eibrain/sdk.py`
- Create: `eimemory/adapters/openclaw/hooks.py`
- Create: `eimemory/cli/main.py`
- Create: `tests/test_models.py`
- Create: `tests/test_storage.py`
- Create: `tests/test_runtime.py`
- Create: `tests/test_adapters.py`

### Task 1: Package Scaffold

**Files:**
- Create: `pyproject.toml`
- Create: `eimemory/__init__.py`
- Create: `eimemory/version.py`
- Test: `tests/test_runtime.py`

- [ ] **Step 1: Write the failing package import test**
- [ ] **Step 2: Run `pytest tests/test_runtime.py -q` and confirm import failure**
- [ ] **Step 3: Add package metadata and exports**
- [ ] **Step 4: Run `pytest tests/test_runtime.py -q` and confirm import passes**

### Task 2: Unified Record Models

**Files:**
- Create: `eimemory/core/ids.py`
- Create: `eimemory/core/clock.py`
- Create: `eimemory/core/errors.py`
- Create: `eimemory/models/records.py`
- Create: `eimemory/models/reports.py`
- Test: `tests/test_models.py`

- [ ] **Step 1: Write failing tests for record envelopes, links, and recall bundles**
- [ ] **Step 2: Run `pytest tests/test_models.py -q` and confirm failures**
- [ ] **Step 3: Implement minimal dataclass models and helpers**
- [ ] **Step 4: Run `pytest tests/test_models.py -q` and confirm passes**

### Task 3: Local Storage Layer

**Files:**
- Create: `eimemory/config/defaults.py`
- Create: `eimemory/storage/jsonl.py`
- Create: `eimemory/storage/sqlite_store.py`
- Create: `eimemory/storage/runtime_store.py`
- Test: `tests/test_storage.py`

- [ ] **Step 1: Write failing tests for append, materialize, search, and active policy lookup**
- [ ] **Step 2: Run `pytest tests/test_storage.py -q` and confirm failures**
- [ ] **Step 3: Implement JSONL and SQLite storage with simple text search**
- [ ] **Step 4: Run `pytest tests/test_storage.py -q` and confirm passes**

### Task 4: Runtime APIs

**Files:**
- Create: `eimemory/api/memory.py`
- Create: `eimemory/api/evolution.py`
- Create: `eimemory/api/runtime.py`
- Test: `tests/test_runtime.py`

- [ ] **Step 1: Extend runtime tests for ingest, recall, observe, feedback, and policy**
- [ ] **Step 2: Run `pytest tests/test_runtime.py -q` and confirm failures**
- [ ] **Step 3: Implement API classes over the storage layer**
- [ ] **Step 4: Run `pytest tests/test_runtime.py -q` and confirm passes**

### Task 5: Initial Adapters

**Files:**
- Create: `eimemory/adapters/eibrain/sdk.py`
- Create: `eimemory/adapters/openclaw/hooks.py`
- Create: `eimemory/cli/main.py`
- Test: `tests/test_adapters.py`

- [ ] **Step 1: Write failing tests for eibrain SDK calls and OpenClaw hook behavior**
- [ ] **Step 2: Run `pytest tests/test_adapters.py -q` and confirm failures**
- [ ] **Step 3: Implement adapter shims that call the stable runtime API**
- [ ] **Step 4: Run `pytest tests/test_adapters.py -q` and confirm passes**

### Task 6: Full Verification

**Files:**
- Test: `tests/test_models.py`
- Test: `tests/test_storage.py`
- Test: `tests/test_runtime.py`
- Test: `tests/test_adapters.py`

- [ ] **Step 1: Run full test suite**
- [ ] **Step 2: Read output and fix any remaining failures**
- [ ] **Step 3: Re-run full suite until clean**
- [ ] **Step 4: Summarize file responsibilities and integration points**
