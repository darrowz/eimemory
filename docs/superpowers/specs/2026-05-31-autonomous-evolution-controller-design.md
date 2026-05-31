# Autonomous Evolution Controller Design

## Goal

Build a B-level autonomous evolution layer for eimemory: the system may automatically improve memory policies, recall profiles, source weights, intent patterns, replay datasets, and playbooks after local replay/evaluation proves the change is low risk. It must not automatically edit code, delete data, change production service configuration, or deploy.

## Product Boundary

The controller is an experiment and governance loop, not a general executor. It learns from local events and outcomes first, then optionally uses web evidence to generate hypotheses faster. External sources never become active policy directly. They become candidate hypotheses and replay cases, and local replay decides whether a safe patch can be applied.

Allowed automatic actions:

- Upsert intent patterns.
- Persist judgment/playbook policies.
- Activate low-risk rules that only affect eimemory retrieval policy.
- Adjust active rule `retrieval_policy.recall_profile`.
- Adjust active rule `retrieval_policy.source_weights`.
- Persist replay datasets and evolution reports.
- Quarantine or downweight noisy recall sources by policy, not by deleting records.

Blocked automatic actions:

- Code edits.
- Production deploy or service restart.
- Destructive data mutation.
- Credential, network, shell, or high-permission tool changes.
- External web knowledge promotion without local replay.

## Architecture

The new module is `eimemory.governance.autonomous_evolution`. It has five stages:

1. **Opportunity mining** reads event/outcome pairs, judgment reports, production recall eval reports, recall audits, rule-evolution reports, and optional web scout hypotheses.
2. **Replay synthesis** converts opportunities into deterministic replay cases with expected text, forbidden kinds/sources, policy expectations, and latency/pollution constraints.
3. **Experiment runner** compares the baseline against a proposed low-risk patch by running recall/policy replay before and after applying the proposal in an isolated temporary runtime when needed.
4. **Promotion gate** applies only patches that meet thresholds: pass rate >= 0.8, no forbidden hits, no latency regression beyond configured tolerance, prompt pollution not worse, and scope/task type constrained.
5. **Reporting** persists a reflection report that lists opportunities, replay cases, experiments, applied patches, blocked patches, and web hypotheses.

The web learning scout lives in `eimemory.governance.web_learning`. It accepts configured URLs or already-captured evidence records, stores raw evidence summaries, and emits candidate policies/replay cases. It does not apply patches.

## Data Contracts

### Opportunity

```json
{
  "id": "opp_...",
  "source": "event_outcome|judgment|production_recall|recall_audit|web_scout",
  "opportunity_type": "intent_policy|recall_profile|source_weight|noise_suppression|replay_dataset|playbook",
  "task_type": "chat.reply",
  "trigger": "repair: OpenClaw 没反应",
  "problem": "重复临时修复，缺少验证",
  "evidence_record_ids": ["evt_..."],
  "candidate_policy": "repair 请求先诊断日志、进程和最近变更，再低风险修复并验证",
  "risk_level": "low|medium|high",
  "confidence": 0.82
}
```

### Replay Case

```json
{
  "case_id": "case_...",
  "query": "OpenClaw 又没反应",
  "task_context": {
    "task_type": "chat.reply",
    "recall_profile": "precision"
  },
  "expected_text": ["先诊断日志", "验证"],
  "forbid_kinds": ["reflection"],
  "forbid_source_contains": ["openclaw.agent_end"],
  "success_criteria": "policy_match"
}
```

### Safe Patch

```json
{
  "patch_id": "patch_...",
  "patch_type": "intent_pattern|active_rule|source_weight|replay_dataset",
  "scope": {"tenant_id": "default", "agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"},
  "task_type": "chat.reply",
  "payload": {
    "pattern": "repair: OpenClaw 又没反应",
    "execution_policy": ["先诊断日志", "低风险修复", "验证"]
  },
  "risk_level": "low",
  "source_opportunity_ids": ["opp_..."]
}
```

## Web Learning Policy

Web learning is trigger-based. It runs only when configured by the operator or when local signals show a repeated failure, recall gap, production recall regression, or user-requested research direction.

Allowed source classes:

- Official docs.
- Academic papers.
- GitHub repositories, issues, and pull requests.
- High-quality technical articles with stable URLs and authorship.

Rejected source classes:

- SEO summaries without primary evidence.
- Marketing pages.
- Unattributed scraped content.
- Any source that cannot be stored as raw evidence with URL and retrieval time.

The scout produces `web_scout` opportunities with `risk_level=medium` by default. They can only become low-risk after local replay passes.

## Error Handling

The controller must be fail-closed. If opportunity mining, web fetch, replay, or patch application fails, it records a blocked patch and continues with other opportunities. A failed web source never blocks local evolution. An invalid safe patch is skipped with a reason.

## Testing Strategy

Tests should cover:

- Bad event outcomes become opportunities and replay cases.
- Good verified paths can become intent patterns.
- Production recall failures can propose precision recall profile/source weight patches.
- Web evidence creates hypotheses but does not directly apply policy.
- A patch applies only after replay passes.
- A patch blocks when forbidden hits, poor pass rate, or high risk is detected.
- Nightly includes an autonomous evolution summary.

## Rollout

Start with `apply=False` by default for CLI. Nightly may run with `apply=True` only for low-risk patches and must persist a report. Code patching and deployment remain out of scope until enough controller reports prove stable behavior.
