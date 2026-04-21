# EIMemory QMD Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a QMD-compatible CLI layer so OpenClaw can call `eimemory` through `memory.backend = "qmd"` style command contracts.

**Architecture:** Implement a lightweight QMD shim inside `eimemory` that stores collection metadata under XDG paths, builds a minimal `index.sqlite` with the `documents` table OpenClaw expects, and serves `collection`, `update`, `embed`, `search`, `query`, and `vsearch` commands. Keep the search engine simple and deterministic by indexing Markdown files into a local SQLite table and returning QMD-shaped JSON results.

**Tech Stack:** Python 3.11+, argparse, sqlite3, pathlib, pytest

---

### Task 1: Plan The Compatibility Surface

**Files:**
- Create: `C:\Users\maiph\Desktop\hypermemory\docs\superpowers\plans\2026-04-19-eimemory-qmd-compat.md`
- Modify: `C:\Users\maiph\Desktop\hypermemory\README.md`
- Test: `C:\Users\maiph\Desktop\hypermemory\tests\test_platform.py`

- [ ] **Step 1: Record the OpenClaw-facing command contract**

Document the supported commands and their expected arguments:

```text
eimemory qmd collection list --json
eimemory qmd collection add <path> --name <name> --mask <pattern>
eimemory qmd collection remove <name>
eimemory qmd update
eimemory qmd embed
eimemory qmd search <query> --json -n <limit> [-c <collection>]
eimemory qmd query <query> --json -n <limit> [-c <collection>]
eimemory qmd vsearch <query> --json -n <limit> [-c <collection>]
```

- [ ] **Step 2: Verify the current CLI does not already expose a QMD shim**

Run: `python -m pytest tests/test_platform.py -q`
Expected: PASS, with no tests covering `qmd` subcommands yet

- [ ] **Step 3: Commit the plan checkpoint**

```bash
git add docs/superpowers/plans/2026-04-19-eimemory-qmd-compat.md
git commit -m "docs: add qmd compatibility plan"
```

### Task 2: Add The QMD Compatibility Module

**Files:**
- Create: `C:\Users\maiph\Desktop\hypermemory\eimemory\adapters\openclaw\qmd_compat.py`
- Modify: `C:\Users\maiph\Desktop\hypermemory\eimemory\cli\main.py`
- Test: `C:\Users\maiph\Desktop\hypermemory\tests\test_platform.py`

- [ ] **Step 1: Write the failing test for collection lifecycle and JSON search output**

```python
def test_qmd_compat_collection_and_search(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    memory_dir = workspace / "memory"
    memory_dir.mkdir()
    note = memory_dir / "fact.md"
    note.write_text("# Fact\\n\\nServer smoke record\\n", encoding="utf-8")

    root = tmp_path / "runtime"
    env_root = str(root)

    assert cli_main(["qmd", "collection", "add", str(memory_dir), "--name", "memory-dir-main", "--mask", "**/*.md"]) == 0
    assert cli_main(["qmd", "update"]) == 0
    assert cli_main(["qmd", "search", "Server smoke", "--json", "-n", "5", "-c", "memory-dir-main"]) == 0
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_platform.py::test_qmd_compat_collection_and_search -v`
Expected: FAIL because `qmd` is not yet a supported CLI command

- [ ] **Step 3: Add the minimal QMD compatibility module**

Implement:

```python
class QmdCompatRuntime:
    def add_collection(self, path: str, name: str, pattern: str) -> None: ...
    def list_collections(self) -> list[dict[str, str]]: ...
    def remove_collection(self, name: str) -> None: ...
    def update_index(self) -> dict[str, int]: ...
    def search(self, query: str, limit: int, collections: list[str]) -> list[dict[str, object]]: ...
```

Key compatibility rules:

```python
CREATE TABLE IF NOT EXISTS documents (
    hash TEXT PRIMARY KEY,
    collection TEXT NOT NULL,
    path TEXT NOT NULL,
    content TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
)
```

And JSON result shape:

```python
{
    "docid": "<stable hash>",
    "collection": "<collection name>",
    "file": "fact.md",
    "snippet": "@@ -1,2\\n# Fact\\n\\nServer smoke record",
    "score": 1.0,
}
```

- [ ] **Step 4: Wire the new `qmd` command tree into the main CLI**

Add subcommands for:

```python
sub.add_parser("qmd")
```

and route nested commands to the compatibility module.

- [ ] **Step 5: Run the targeted test to verify it passes**

Run: `pytest tests/test_platform.py::test_qmd_compat_collection_and_search -v`
Expected: PASS

### Task 3: Harden Docs And Platform Coverage

**Files:**
- Modify: `C:\Users\maiph\Desktop\hypermemory\README.md`
- Modify: `C:\Users\maiph\Desktop\hypermemory\tests\test_platform.py`

- [ ] **Step 1: Add regression coverage for `query`/`vsearch` aliases and collection listing**

```python
def test_qmd_aliases_and_collection_listing(tmp_path):
    ...
    assert cli_main(["qmd", "collection", "list", "--json"]) == 0
    assert cli_main(["qmd", "query", "Server smoke", "--json", "-n", "3"]) == 0
    assert cli_main(["qmd", "vsearch", "Server smoke", "--json", "-n", "3"]) == 0
```

- [ ] **Step 2: Update README with OpenClaw configuration guidance**

Add a short setup section like:

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

- [ ] **Step 3: Run the full test suite**

Run: `python -m pytest tests -q`
Expected: PASS with all tests green

- [ ] **Step 4: Commit the compatibility feature**

```bash
git add eimemory/adapters/openclaw/qmd_compat.py eimemory/cli/main.py tests/test_platform.py README.md
git commit -m "feat: add qmd compatibility shim for openclaw"
```
