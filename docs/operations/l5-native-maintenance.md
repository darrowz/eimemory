# Native L5 maintenance

With `EIMEMORY_L5_LOOP_ENABLED=1` and `EIMEMORY_L5_LOOP_APPLY=1`, nightly uses the same bounded system detector, incident router and transaction recovery owner as `learn watch --apply`. A watch without `--apply` does not write incidents or resume effects. Existing transaction recovery remains available when a new detector or proposal fails.

After quality, capability evolution and code maintenance, nightly persists one native v4 reader envelope, verifies its archive and retains the current deployment receipt and release session. Capability selection may use the configured product scope only through the existing operator alias grant. Business quality and code transaction ownership stay on the exact report scope. A reliable capability snapshot is not a terminal repair receipt.

The runtime identity detector uses owner-bound `runtime_identity_drift.v2` identities. Time belongs to the observation, so concurrent observations of the same fault retain one immutable first incident. A separate current observation prevents a recovered or replaced fault from being proposed again. Historical v1 incidents remain historical. Missing or corrupt current authority blocks routing rather than implying recovery.

Source-specific quality and incident reads, and exact-owner repository ledger reads, apply their SQL filters before their result bound. A full sentinel page or corrupt physical/payload identity is an incomplete read. It cannot silently certify absence or fall back to another owner's evidence.

## Policy operator commands

Use the actual current native incident's SHA-256 digest and registered class, detector and protected test plan. `deploy/code-automation-policy.v2.json.example` describes a structure; its placeholder digests and historical timestamps are not deployable authority.

```sh
eimemory learn code-evolution-policy-issue \
  --incident-class deployment.runtime_commit_drift \
  --detector-id eimemory.runtime_identity_drift.v1 \
  --incident-digest "$CURRENT_NATIVE_INCIDENT_SHA256" \
  --test-plan-id deployment.runtime-identity-drift.v1 \
  --max-transactions 1 \
  --effects all-disabled

eimemory learn code-evolution-policy-validate \
  --path /etc/eimemory/code-automation-policy.v2.json \
  --incident-digest "$CURRENT_NATIVE_INCIDENT_SHA256" --json
```

Issuing without `--install-path` prints a policy. Installation uses a complete private temporary file and atomic replacement, rejects symlink paths, and requires a clean repository. The installed policy must be private and owned by the runtime user: a root-owned mode-600 file is not readable authority for a `darrow` runtime. Enable the intended effects explicitly through the existing protected operator policy. Validation uses the same native policy loader as the router and opens no runtime database. It validates policy authority; the router still checks the actual current incident, exact owner, repository, provider and one-shot limit.

`execution_ok`, `control_plane_ok` and `product_l5_complete` are separate results. A valid archived assessment awaiting an autonomous terminal receipt or its required observation window is reported in `l5_evidence_waits`. Execution errors, invalid existing proofs and negative evidence remain failures. Qualified historical business samples continue to count as business evidence; they do not invent an autonomous code transaction or shorten the native eight-hour observation requirement.

The installer verifies a persisted current-release receipt before dispatching maintenance. Installing or enabling timer metadata alone cannot start a first-install watcher. A receipt or native read-back failure blocks restoration and retains the deployment rollback boundary.

Readiness keeps the existing 200 ms RPC probe and 250 ms fragment deadlines. Active-memory authority heads use a covering keyset index; a busy reader pool waits for a returning reader within that same deadline. An already submitted backend worker retains single flight until it ends, including a deadline expiring before the Future wait begins. These repairs add no business samples, reset no failed evidence, and certify no L5 terminal receipt or observation period.

In 1.14.152, controlled qualification waits for a real read-only current-projection proof before sampling. Release replay and live gates can write memory, so the release workflow rechecks the vector projection and the authenticated listener after replay and after the full observation window. Both must identify the same authority revision and watermark. This bounded settling phase does not enlarge the original recall or readiness-probe budgets, retry an actual failed stability sample, or certify L5 completion.
