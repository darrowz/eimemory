# eimemory documentation

`eimemory` is a local-first memory and evidence-gated learning runtime for
long-running AI agents. This index covers the maintained product and operating
guides. The [project homepage](../README.md) gives the overview; the
[acceptance status](acceptance-status.md) records the latest reported production
result and its limits.

## Start and integrate

| Task | Guide |
| --- | --- |
| Install and try local memory | [Quick Start](QUICKSTART.md), [FAQ](../FAQ.md) |
| Understand boundaries and ownership | [Architecture](architecture.md), [modules](modules.md), [ADRs](adr/README.md) |
| Connect Codex | [Codex plugin](../integrations/codex/eimemory/README.md) |
| Connect Hermes | [Memory provider](../integrations/hermes/eimemory/README.md), [host hooks](../integrations/hermes/eimemory_hook/README.md) |
| Connect OpenClaw | [External bridge](../integrations/openclaw/eimemory-bridge/README.md) |
| Operate RPC and channel profiles | [Operations](operations.md) |

## Evaluate and deploy

| Task | Guide |
| --- | --- |
| Check latest reported acceptance | [Receipt-based status](acceptance-status.md) |
| Separate smoke, benchmarks and formal gates | [Evaluation](evaluation.md) |
| Check explicit and natural recall | [Explicit recall](explicit-recall-acceptance.md), [real-effect signals](deployment/real-effect-signals.md) |
| Configure semantic retrieval | [Semantic admission](deployment/semantic-admission.md), [vector candidates](postgres-vector-candidates.md) |
| Install and recover immutable releases | [Deployment](deployment.md), [systemd](../deploy/systemd/README.md) |
| Protect release sources | [Source protection](deployment/release-source-protection.md) |
| Assess capability maturity | [L5 v3 architecture](architecture.md#dynamic-l5-v3), [independent evidence](architecture/independent-evidence-v1.zh-CN.md) |
| Contribute or review changes | [Contributing](../CONTRIBUTING.md), [changelog](../CHANGELOG.md) |
| Compare approaches or describe the project | [Comparison](COMPARISON.md), [public positioning](github-recommendation.md) |

## Evidence boundaries

A successful process or known-item smoke does not certify business recall.
An empty memory benchmark is not a pass; a capability/code evaluation cannot
replace it. L5 readiness is specific to a profile, binding, scope and release.

Documents in `audit/`, `audits/`, `plans/` and `superpowers/`, and dated closure
reports, preserve historical evidence and design decisions. They are not a
current production certificate. Do not update their original measurements to
match a newer release; compare them with fresh persisted receipts instead.
