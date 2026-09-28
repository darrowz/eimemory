# Absorb note — eimemory closure-incident pack `3a87bf3` (2026-09-28)

## Base / apply

- **Exact baseline:** `3a87bf3b8d464e64fda5025fbf05b4df772d0b75` / 1.14.3
- **Pack:** `/workspace/audit-absorb-20260928/eimemory-closure-incident-3a87bf3`
- **Repo:** box-only `/workspace/eimemory` (no Hongxin / DESKTOP sync or deploy)
- **All `base_blob` values matched HEAD** before apply; worktree was clean.

## Why fragment / `updated_files` apply (not unified `git apply` alone)

`verify_and_apply.py --apply` fails postcheck on
`eimemory/governance/release/release_impact.py`:

- Pack **fragments** `03-00-before.txt` / `03-00-after.txt` require inserting the
  `DOMAIN_PATHS` augmentation **immediately before** `IGNORED_PATH_PREFIXES`
  (~line 267).
- The pack-internal **unified** patch hunk for that file has only blank-line
  context and `git apply` fuzz-inserts the block after `_normalized_version_module`
  (~line 537) instead.
- Manifest **postcheck compares expected bytes from `updated_files` +
  replacements**, so a successful fuzzy unified apply still mismatches
  `release_impact.py`.

**Source of truth for this absorb:** copy bytes from `updated_files/...` (verify
`updated_sha256`) and apply each `replacements` before→after exactly once
(`count=1`) on the HEAD blob. Unified `git apply --check` was run for
documentation only and is expected to fail against an already-applied
fragment tree; it was **not** used as the content authority.

## Files (17)

| Path | Method |
|------|--------|
| `deploy/summarize_release_closure.py` | `updated_file` |
| `deploy/record_release_closure_incident.py` | `updated_file` |
| `eimemory/ops/release_closure_failure.py` | `updated_file` |
| `eimemory/governance/evolution/system_code_repair.py` | `updated_file` |
| `eimemory/governance/evolution/code_evolution_test_plans.py` | `updated_file` |
| `tests/test_release_closure_failure.py` | `updated_file` |
| `tests/test_governance_env.py` | `updated_file` |
| `eimemory/governance/release/closure_verdict.py` | added (`updated_file`) |
| `eimemory/ops/closure_capture.py` | added (`updated_file`) |
| `tests/test_closure_pipeline_contract.py` | added |
| `tests/test_closure_capture_pipeline.py` | added |
| `tests/test_closure_repair_routing.py` | added |
| `tests/test_closure_pipeline_runtime.py` | added |
| `deploy/install_immutable_release.sh` | replacements (4) |
| `tests/test_deployment_tools.py` | replacements (1) + absorb harness update |
| `eimemory/governance/evolution/code_automation_policy_issue.py` | replacements (1) |
| `eimemory/governance/release/release_impact.py` | replacements (1) → DOMAIN_PATHS before `IGNORED_PATH_PREFIXES` |

## Absorb-local test adaptations (not in pack bytes)

Pack verification ran an isolated harness (124 passed, 1 deselected) and did
**not** execute full-repo `test_system_code_repair.py` / the old installer
outcome harness against the new recorder path. For README focused suite on
the real checkout:

1. **`tests/test_system_code_repair.py`** — routing harness gains `scope` /
   `record_id` / `store.append`; digest-mismatch no longer expects fake
   `idle` (AUDIT §7); allowed_files path uses post-1.14.0
   `governance/evolution/system_code_repair.py`.
2. **`tests/test_deployment_tools.py::test_installer_reports_business_closure_outcome_without_unqualified_completion`**
   — stubs `record_release_closure_incident.py` + `capture_saved` instead of
   the removed summarizer-only fake.

## Tests run (this absorb)

```text
pytest -q -p no:cacheprovider --strict-markers \
  tests/test_closure_pipeline_contract.py \
  tests/test_closure_capture_pipeline.py \
  tests/test_closure_repair_routing.py \
  tests/test_closure_pipeline_runtime.py \
  tests/test_release_closure_failure.py \
  tests/test_governance_env.py \
  tests/test_system_code_repair.py
→ 153 passed

pytest … test_immutable_release_installer_commits_after_technical_health_before_business_validation
pytest … test_installer_reports_business_closure_outcome_without_unqualified_completion
pytest … tests/test_code_automation_policy_issue.py
→ passed

git diff --check → clean
```

Pack-equivalent deselected Runtime subset (for comparison with validation.json):
124 passed, 1 deselected (`not test_release_closure_failure_persistence_is_idempotent`).

## Release

- Version bump **1.14.3 → 1.14.4**
- No Hongxin / DESKTOP deploy

## Confirmation

`release_impact.py`: `DOMAIN_PATHS` augmentation for
`closure_verdict.py` / `closure_capture.py` / `release_closure_failure.py`
sits immediately before `IGNORED_PATH_PREFIXES` (lines ~269–276).
