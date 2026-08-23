# EIMemory Project-Wide Audit Baseline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a reproducible maintained-source inventory, a clean full-suite baseline, and an evidence-backed business-flow defect/cleanup ledger that determines the subsequent TDD repair batches.

**Architecture:** Add one development-only source-audit utility that parses every tracked maintained source and reports its disposition, line coverage, callable spans, entry-point signals, and risky constructs without changing runtime state. Combine that structural inventory with executable business-flow suites and manual owner/terminal-state review in one commit-bound audit report. This batch diagnoses and classifies; it does not guess at repairs before counterexamples exist.

**Tech Stack:** Python 3.11+, standard-library `ast`, `argparse`, `dataclasses`, `json`, `pathlib`, `subprocess`, pytest, Git, Bash syntax checks, existing package/deployment tests.

## Global Constraints

- Synthetic data proves mechanism only and must never enter production state or L5 maturity inputs.
- Real production evolution consumes only real scoped business events and terminal outcomes.
- Every tracked maintained production/deployment source must receive an audit disposition.
- Do not delete a source or test until Python imports, dynamic launchers, package entry points, systemd/subprocess references, public contracts, persisted compatibility, and external integrations have all been checked.
- Preserve security, durability, release identity, rollback, host integration, and L5 evidence gates.
- Bind the final audit report to one full Git commit and explicitly distinguish sandbox/environment failures from product failures.
- Use focused diagnostics once a failure is observed; do not repeat an identical failing command without changing the diagnostic or environment.

---

## File Structure

- `scripts/audit_business_closure.py`: tracked-source discovery, syntax/AST parsing, disposition, risk-signal inventory, coverage validation, JSON/Markdown rendering.
- `tests/test_business_closure_source_audit.py`: utility unit tests and repository-wide coverage contract.
- `docs/audit/project-wide-business-closure-2026-08-23.md`: commit-bound structural inventory, baseline results, ten business-flow reviews, confirmed defects, deletion candidates, and next TDD batch order.
- `docs/superpowers/specs/2026-08-23-eimemory-project-wide-business-closure-design.md`: approved authority and release semantics; read-only input to this batch.

### Task 1: Add the maintained-source audit utility

**Files:**
- Create: `scripts/audit_business_closure.py`
- Test: `tests/test_business_closure_source_audit.py`

**Interfaces:**
- Consumes: repository root and optional explicit tracked relative paths.
- Produces: `AuditItem`, `audit_paths(repo_root, paths) -> tuple[AuditItem, ...]`, `tracked_maintained_paths(repo_root) -> tuple[Path, ...]`, `validate_complete(items) -> None`, and CLI JSON/Markdown output.

- [ ] **Step 1: Write the failing unit tests**

```python
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = spec_from_file_location(
    "audit_business_closure",
    ROOT / "scripts" / "audit_business_closure.py",
)
assert SPEC is not None and SPEC.loader is not None
AUDIT = module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


def test_audit_paths_parses_every_python_line_and_callable(tmp_path: Path) -> None:
    source = tmp_path / "eimemory" / "api" / "sample.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def public(value: str) -> str:\n"
        "    if not value:\n"
        "        raise ValueError('value required')\n"
        "    return value\n",
        encoding="utf-8",
    )

    (item,) = AUDIT.audit_paths(tmp_path, (Path("eimemory/api/sample.py"),))

    assert item.disposition == "entry_or_adapter"
    assert item.total_lines == 4
    assert item.parsed_lines == 4
    assert item.callables == (("public", 1, 4),)
    assert item.syntax_status == "ok"


def test_audit_paths_rejects_invalid_python(tmp_path: Path) -> None:
    source = tmp_path / "eimemory" / "storage" / "broken.py"
    source.parent.mkdir(parents=True)
    source.write_text("def broken(:\n", encoding="utf-8")

    (item,) = AUDIT.audit_paths(tmp_path, (Path("eimemory/storage/broken.py"),))

    assert item.syntax_status == "invalid"
    assert item.disposition == "business_owner"
    try:
        AUDIT.validate_complete((item,))
    except ValueError as exc:
        assert "invalid syntax" in str(exc)
    else:
        raise AssertionError("invalid maintained source passed validation")


def test_repository_inventory_has_no_unclassified_maintained_source() -> None:
    paths = AUDIT.tracked_maintained_paths(ROOT)
    items = AUDIT.audit_paths(ROOT, paths)

    AUDIT.validate_complete(items)
    assert paths
    assert {item.disposition for item in items} >= {
        "business_owner",
        "entry_or_adapter",
        "shared_contract",
        "operational_gate",
        "compatibility_surface",
    }
```

- [ ] **Step 2: Run the tests and verify the utility is absent**

Run: `.venv/bin/python -m pytest -q tests/test_business_closure_source_audit.py`

Expected: collection fails because `scripts/audit_business_closure.py` does not exist.

- [ ] **Step 3: Implement the audit data contract and disposition rules**

```python
@dataclass(frozen=True, slots=True)
class AuditItem:
    path: str
    disposition: str
    total_lines: int
    parsed_lines: int
    syntax_status: str
    callables: tuple[tuple[str, int, int], ...]
    imports: tuple[str, ...]
    entry_signals: tuple[str, ...]
    risk_signals: tuple[str, ...]


_MAINTAINED_PREFIXES = (
    "eimemory/",
    "deploy/",
    "integrations/",
    "scripts/",
    ".github/workflows/",
)
_MAINTAINED_SUFFIXES = {
    ".py", ".sh", ".js", ".json", ".toml", ".yaml", ".yml",
    ".service", ".timer", ".path", ".conf", ".example",
}


def _disposition(path: Path) -> str:
    value = path.as_posix()
    if value.startswith(("deploy/", ".github/workflows/", "eimemory/ops/")):
        return "operational_gate"
    if value.startswith("eimemory/compatibility/") or "qmd_compat" in value:
        return "compatibility_surface"
    if value.startswith((
        "eimemory/api/", "eimemory/adapters/", "eimemory/cli/",
        "eimemory/ei_bridge/", "integrations/", "scripts/",
    )):
        return "entry_or_adapter"
    if value.startswith((
        "eimemory/autonomous/", "eimemory/capabilities/",
        "eimemory/evaluation/", "eimemory/experience/",
        "eimemory/governance/", "eimemory/intake/", "eimemory/knowledge/",
        "eimemory/living/", "eimemory/raw/", "eimemory/recall/",
        "eimemory/retrieval/", "eimemory/scheduler/", "eimemory/storage/",
    )):
        return "business_owner"
    return "shared_contract"
```

The implementation must use `git ls-files -z` for tracked discovery, parse
Python with `ast.parse`, record all `FunctionDef`, `AsyncFunctionDef`, and
`ClassDef` spans using `lineno`/`end_lineno`, validate JSON with `json.loads`,
and mark non-Python inputs for their later native syntax command. It must not
execute imports or read runtime/production state.

- [ ] **Step 4: Implement validation and deterministic renderers**

```python
def validate_complete(items: Iterable[AuditItem]) -> None:
    values = tuple(items)
    invalid = sorted(item.path for item in values if item.syntax_status == "invalid")
    uncovered = sorted(
        item.path for item in values
        if item.total_lines != item.parsed_lines or not item.disposition
    )
    if invalid or uncovered:
        parts = []
        if invalid:
            parts.append("invalid syntax: " + ", ".join(invalid))
        if uncovered:
            parts.append("uncovered source: " + ", ".join(uncovered))
        raise ValueError("; ".join(parts))
```

CLI arguments are exactly `--repo-root`, `--format {json,markdown}`, and
`--output`. JSON uses sorted keys and Markdown reports totals by disposition,
syntax status, suffix, entry signal, and risk signal followed by one row per
tracked source. Output is deterministic for a fixed tree.

- [ ] **Step 5: Run focused and repository coverage tests**

Run: `.venv/bin/python -m pytest -q tests/test_business_closure_source_audit.py`

Expected: all tests pass and repository validation reports zero unclassified or syntax-invalid maintained sources.

- [ ] **Step 6: Commit the audit utility**

```bash
git add scripts/audit_business_closure.py tests/test_business_closure_source_audit.py
git commit -m "test: inventory maintained business sources"
```

### Task 2: Capture a clean executable baseline

**Files:**
- Create: `docs/audit/project-wide-business-closure-2026-08-23.md`

**Interfaces:**
- Consumes: exact audit commit, isolated test environment, current package tree, and live read-only production identity.
- Produces: a baseline section that distinguishes product failures, sandbox limits, external prerequisites, and passing contracts.

- [ ] **Step 1: Generate the exact source inventory outside the report tree**

Run:

```bash
.venv/bin/python scripts/audit_business_closure.py \
  --repo-root . --format json --output .tmp/business-closure-source-audit.json
```

Expected: exit zero; JSON contains every tracked maintained source exactly once; `validate_complete` finds no invalid/uncovered file.

- [ ] **Step 2: Validate non-Python maintained syntax**

Run:

```bash
bash -n deploy/install_immutable_release.sh \
  deploy/check_user_systemd_owner.sh \
  deploy/discover_python_runtime_units.sh \
  deploy/systemd/eimemory-l5-effect-review.sh \
  deploy/systemd/eimemory-rpc-cleanup-port.sh \
  deploy/systemd/hermes-gateway-eimemory.sh
.venv/bin/python -m compileall -q eimemory deploy integrations scripts tests
```

Expected: both commands exit zero.

- [ ] **Step 3: Run package, CLI, and entry-point smoke checks**

Run:

```bash
.venv/bin/python -m build --wheel --outdir .tmp/dist
.venv/bin/python -c "from importlib.metadata import entry_points; from eimemory import Runtime; assert Runtime; assert any(ep.name == 'hongtu' for ep in entry_points(group='eimemory.capability_catalog.bootstrap.v1'))"
.venv/bin/eimemory --help
.venv/bin/eimemory doctor --help
```

Expected: wheel builds; package imports; trusted Hongtu catalog entry point and both CLI parsers are available.

- [ ] **Step 4: Run the full baseline once with uncontrolled proposer/policy inputs removed**

Run:

```bash
env -u EIMEMORY_CODE_PATCH_LLM_COMMAND \
    -u EIMEMORY_LLM_COMMAND \
    -u EIMEMORY_CODE_AUTOMATION_POLICY_JSON \
    -u PYTHONPATH \
    .venv/bin/python -m pytest -q
```

Expected: the command completes. Record the exact pass/skip/fail counts and
duration. For each failure, run its exact node with `-vv` once and classify it
as product, sandbox/quota, host prerequisite, or test defect. Do not edit source
inside this task.

- [ ] **Step 5: Create the baseline report with fixed evidence sections**

The document must contain these headings and no prospective pass claims:

```markdown
# Project-Wide Business Closure Audit — 2026-08-23

## Authority and simulation boundary
## Source and entry-point coverage
## Clean executable baseline
## Business-flow closure matrix
## Confirmed product defects
## Environment or prerequisite failures
## Dead-code, test, and deployment candidates
## L5 and code-evolution production evidence
## Ordered repair batches
## Final exact-commit verification
```

Under source coverage, copy the inventory counts and list every maintained
entry signal. Under baseline, record commands, exact commit, version, Python,
platform, results, and failure classifications. Keep raw large logs in `.tmp`;
the report contains bounded evidence and precise test node IDs.

- [ ] **Step 6: Commit the baseline evidence**

```bash
git add docs/audit/project-wide-business-closure-2026-08-23.md
git commit -m "docs: record project-wide closure baseline"
```

### Task 3: Audit the ten maintained business-flow families

**Files:**
- Modify: `docs/audit/project-wide-business-closure-2026-08-23.md`

**Interfaces:**
- Consumes: source inventory, `docs/architecture.md`, `docs/modules.md`, CLI/RPC/adapter/deploy entry points, and existing focused suites.
- Produces: one reviewed input-to-terminal-state row per maintained flow and concrete counterexamples for every non-closed row.

- [ ] **Step 1: Build each flow row from authoritative owners**

Use this exact row contract:

```markdown
| Flow | Ingress | Authority/scope | Durable transition | Terminal success | Failure/rollback | Executable evidence | Status |
```

Populate it for the ten flow families in design section 3. Each cell names
actual modules, functions/commands, record kinds/tables, and terminal statuses;
do not use an unspecified remainder, a delegated-coverage claim, or a test
count without node/file names.

- [ ] **Step 2: Run storage/data-plane flow evidence**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_storage.py tests/test_storage_deploy.py \
  tests/test_storage_cli.py tests/test_storage_maintenance.py \
  tests/test_runtime_store_concurrency.py \
  tests/test_recall_engine.py tests/test_recall_fusion.py \
  tests/test_postgres_vector_sync.py tests/test_postgres_vector_source.py
```

Expected: record exact results. Any failure becomes a confirmed counterexample only after its focused `-vv` diagnostic proves a product invariant is violated.

- [ ] **Step 3: Run intake/knowledge/experience flow evidence**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_active_intake_platform.py tests/test_source_registry.py \
  tests/test_knowledge_ingest.py tests/test_knowledge_refresh.py \
  tests/test_paper_intake.py tests/test_paper_pdf_pipeline.py \
  tests/test_experience_outcome.py tests/test_experience_bridge.py \
  tests/contract/test_autonomous_learning_measured_closure.py
```

Expected: record exact results and counterexamples using the same rule.

- [ ] **Step 4: Run capability/learning/L5 flow evidence**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_capability_storage_v3.py tests/test_capability_state_projector.py \
  tests/test_capability_incubation.py \
  tests/test_autonomous_learning_integration.py \
  tests/test_autonomous_learning_state.py \
  tests/test_full_autonomous_learning_loop.py \
  tests/test_promotion_manager.py tests/test_code_evolution_transactions.py \
  tests/test_code_evolution_recovery.py tests/test_l5_v3_release_independence.py \
  tests/test_l5_readiness.py tests/test_release_closure.py \
  tests/test_l5_closure_rehearsal.py
```

Expected: record exact results and keep mechanism evidence separate from live production proof.

- [ ] **Step 5: Run integration and release flow evidence**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_adapters.py tests/test_hermes_adapter.py \
  tests/test_openclaw_outcome_hooks.py tests/test_runtime_adapter_rpc.py \
  tests/test_platform.py tests/test_deployment_tools.py \
  tests/test_production_recall_bootstrap_deploy.py
```

Expected: record exact results and tie deployment results to immutable identity/rollback contracts.

- [ ] **Step 6: Manually review high-risk terminal paths**

For every owner named in the matrix, inspect all exception exits and terminal
status writes, then search each status/record kind for a downstream consumer:

```bash
rg -n "except |raise |return |status|verdict|rollback|recover|quarantine|retry|failed|blocked" \
  eimemory/storage eimemory/knowledge eimemory/evaluation \
  eimemory/governance eimemory/adapters eimemory/ops deploy
```

The report records only concrete mismatches such as “producer writes X while
the sole consumer accepts Y”, “partial write precedes validation”, or “terminal
state has no consumer/escalation”. Broad matches without a violated flow are not
defects.

- [ ] **Step 7: Commit the completed flow matrix**

```bash
git add docs/audit/project-wide-business-closure-2026-08-23.md
git commit -m "docs: map maintained business closure flows"
```

### Task 4: Prove cleanup candidates and order repair batches

**Files:**
- Modify: `docs/audit/project-wide-business-closure-2026-08-23.md`

**Interfaces:**
- Consumes: source audit, Git references, dynamic/deployment inventory, flow matrix, and test collection.
- Produces: zero or more proven deletion candidates and an ordered list of concrete defect batches with exact owners/tests.

- [ ] **Step 1: Check every suspected dead source across all reference classes**

For each candidate, record results for:

```bash
git grep -n -- '<module-or-path-stem>' -- eimemory deploy integrations scripts tests .github pyproject.toml
git log --oneline --all -- '<exact-path>'
```

Also check `pyproject.toml` entry points, `__main__`, systemd `ExecStart`, shell
subprocess invocations, plugin manifests, string-based imports, and documented
external compatibility. A candidate remains “retained” if any maintained
obligation exists.

- [ ] **Step 2: Map tests to production invariants**

For each deletion candidate test, name the production invariant and the
surviving test node that enforces the same boundary. If no equivalent survives,
the test is not removable even if its current implementation is deleted; move
or rewrite it at the authoritative owner in the later repair batch.

- [ ] **Step 3: Classify deployment helpers**

Every `deploy/` source receives exactly one of: installer transaction step,
runtime integration installer/verifier, secret/config provisioner, storage
migration/rollback, release evidence/health, managed unit, or proven dead.
Record its caller and failure consequence. Large size or single-caller status is
not proof of redundancy.

- [ ] **Step 4: Order confirmed repair batches by dependency and risk**

Use this fixed order, omitting empty groups:

1. authority, scope, trust, corruption, or irreversible-effect defects;
2. storage/idempotency/terminal-state business defects;
3. adapter and external delivery closure;
4. L5/code-evolution evidence and recovery closure;
5. proven dead-source/test/deployment deletion and consolidation;
6. isolated simulation, full verification, version/release/deployment.

Each listed defect must include an actual counterexample, authoritative owner,
expected invariant, exact current test node or new-test location, and affected
flow family. This becomes the input to the next TDD plan; speculative cleanup
does not enter the list.

- [ ] **Step 5: Self-review the audit against the design**

Check that every source disposition and flow family is present, synthetic/live
evidence is separated, all failures are classified, every deletion claim has
all reference classes checked, and every confirmed defect has an executable
counterexample. Search for forbidden placeholders:

```bash
rg -n 'TB''D|TO''DO|implement la''ter|covered else''where|et''c\.' \
  docs/audit/project-wide-business-closure-2026-08-23.md
```

Expected: no matches.

- [ ] **Step 6: Verify and commit the audit batch**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_business_closure_source_audit.py
.venv/bin/python scripts/audit_business_closure.py --repo-root . --format markdown
git diff --check
```

Expected: utility tests pass, audit validation exits zero, diff check passes.

Commit:

```bash
git add docs/audit/project-wide-business-closure-2026-08-23.md
git commit -m "docs: finalize project-wide closure audit"
```

### Task 5: Convert confirmed findings into TDD repair plans

**Files:**
- Create only the defect-specific spec/plan documents named by Task 4 findings.

**Interfaces:**
- Consumes: confirmed counterexamples and ordered repair batches.
- Produces: one independently testable TDD implementation plan per coupled defect group; no runtime edits in this task.

- [ ] **Step 1: Create one narrow design addendum per coupled defect group**

Each addendum copies the authoritative owner, violated invariant,
counterexample, state transitions, compatibility boundary, error behavior, and
acceptance tests directly from the audit. Unrelated flow families remain in
separate addenda.

- [ ] **Step 2: Create exact TDD tasks**

Each implementation task must name exact files/functions, show the failing
test, the expected red failure, the minimal owner-side repair, focused and
affected-family commands, and its commit message. No repair is scheduled
without a reproduced counterexample.

- [ ] **Step 3: Self-review plans and begin inline execution**

Run placeholder, spec-coverage, and interface consistency checks required by
the writing-plans skill. Because the user authorized uninterrupted execution
and did not request subagents, choose inline execution with the
`executing-plans` skill and proceed in dependency order without another choice
prompt.
