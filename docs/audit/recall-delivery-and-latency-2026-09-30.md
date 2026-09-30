# Recall delivery and latency on honrui (1.14.20)

## 1. "Verified evidence suppressed at control after dedup"
Real decision `pd:89eab526e5d56116c18e9d6da50fd3b2` (1.14.19, hermes, `research.task`, `control_cohort=1`):
- 3 selected → 3 authorized → 3 passed confidence → **session dedupe removed 2** → 1 rendered.
- That 1 fell in the control arm, so it was suppressed (item state `suppressed`, confidence 0.5).

**Control arm (correct by design).** `control_percent=10` is a deterministic hash over (channel, scope, session, query digest, policy). Control decisions deliver only mandatory items and persist voluntary ones as `suppressed`, which gives the uplift baseline. On 1.14.19 the arm drew 2 of 6 decisions, which is within noise for n=6.

**Session dedupe (defect, fixed).** The dedupe query counted every item ever attached to a decision in the session, in any state. Session `20260928_163859_439adbdd` has run since 09-28. Its earlier items were never injected (the acknowledgement gap fixed in 1.14.18) or were control-suppressed, yet they blocked re-offering. Dedupe now counts only delivered items (`ever_injected=1`, `injected`/`used`/`rejected`) and items in flight in open decisions.

## 2. `eimemory_recall` timeout
Measured config (RPC process environment and gateway units):
- RPC `EIMEMORY_RECALL_BUDGET_SECONDS=8`, set in the RPC unit only; gateways cannot see it.
- No LLM timeout set, so the verifier transport timeout is 90s.
- Gateways `EIMEMORY_ADAPTER_TIMEOUT_SECONDS=11.5`.
- Hermes tool deadlines are ≥120s, so the host is not the limiter.
- Failure ledger: 25 recent `adapter.prefetch` failures with `diagnostic.reason=timeout`.

Latency composition: collection ≤ 8s, then a verifier call that may start before the deadline and run up to 90s, then a 0.75s authority read, then persistence. The client gives up at 11.5s.

Fix: verifier ceiling 12s, and a dedicated explicit-recall client at 30s (≥ 8 + 12 + 1.25 = 21.25s) with its own circuit breaker. Deploy acceptance times one real tool call.

## 3. Strict code-evolution terminal receipts = 0
Terminal receipts are written only when a `CodeEvolutionTransaction` finishes observation. On honrui:
- `/etc/eimemory/code-automation-policy.v2.json` is absent.
- `code_evolution_transactions` is empty.

1.14.19 clears the release-lineage code.evolution gate with a signed automatic authorization. It does not, and must not, fabricate transactions or terminal receipts. A nonzero count needs a real transaction: an issued v2 policy (`eimemory learn code-evolution-policy-issue … --install-path /etc/eimemory/code-automation-policy.v2.json`), a registered incident, and the full pipeline including the 8h observation.

## Open breakpoint
The Hermes host bounds proactive `prefetch_all` at 8.0s (`_EXTERNAL_PREFETCH_TIMEOUT_S`), but the RPC recall budget is also 8s plus verification, so a verified proactive recall can still arrive after the host has given up. The evidence rule from 1.14.18 keeps this honest (not delivered). Aligning it needs a decision: a proactive-specific budget below the host timeout, or a host timeout change.
