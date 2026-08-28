# Channel-Neutral Release Acceptance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let any trusted external messaging adapter, including Hermes on non-Feishu platforms, provide the real post-deployment delivery evidence required by release closure and L5 lineage.

**Architecture:** Add a channel-neutral acceptance facade that normalizes trusted OpenClaw and Hermes ledgers into one release-bound evidence record. Add a Hermes gateway plugin bridge that registers a post-delivery callback for genuine inbound user turns and writes a durable normalized receipt only after Hermes marks its exact delivery obligation delivered. Rename the six-domain lineage channel from `channel.openclaw` to `channel.delivery` while retaining read compatibility for historical lineage.

**Tech Stack:** Python 3.11+, SQLite, Hermes plugin hooks, JSON atomic files, pytest, systemd path activation.

## Global Constraints

- New product contracts and current lineage use `channel.delivery`, never an adapter or platform name.
- A receipt binds to the exact current runtime commit and occurs after the current deployment receipt.
- Only genuine external user events followed by successful platform delivery qualify; local, synthetic, bot-authored, failed, stale, and wrong-release events fail closed.
- Existing OpenClaw Feishu delivery evidence remains supported through an adapter reader.
- The release-closure signal is a wake-up hint only; durable evidence is always reread and revalidated.
- Production reports retain the `channel_acceptance` field for compatibility.

---

### Task 1: Channel-Neutral Acceptance Facade

**Files:**
- Create: `eimemory/governance/external_channel_acceptance.py`
- Modify: `eimemory/governance/openclaw_channel_acceptance.py`
- Create: `tests/test_external_channel_acceptance.py`
- Modify: `tests/test_openclaw_channel_acceptance.py`

**Interfaces:**
- Produces: `record_external_channel_acceptance(runtime, *, scope, current_release, openclaw_state_path=..., external_state_path=...) -> dict[str, Any]`
- Produces: `validate_external_channel_acceptance(evidence, *, current_release) -> bool`
- Preserves the OpenClaw recorder and validator as compatibility wrappers.

- [ ] **Step 1: Write failing normalization tests** for Hermes/Telegram and Hermes/Weixin external-ledger entries. Assert they produce `external_channel_acceptance.v1` evidence from `eimemory.external_channel.acceptance` with only digested identifiers.
- [ ] **Step 2: Write failing rejection tests** for wrong commits, pre-deployment receipts, nonaccepted status, missing identifiers, local/deployment-replay platforms, invalid conversation kinds, malformed ledgers, and an OpenClaw Feishu compatibility case.
- [ ] **Step 3: Run RED:** `.venv/bin/python -m pytest -q tests/test_external_channel_acceptance.py tests/test_openclaw_channel_acceptance.py`. Expected: generic functions and Hermes support are missing.
- [ ] **Step 4: Implement the normalized candidate type and fixed-path readers:**

```python
@dataclass(frozen=True)
class ExternalDeliveryCandidate:
    transport_owner: str
    platform: str
    conversation_kind: str
    inbound_message_id: str
    delivery_receipt_id: str
    runtime_commit: str
    received_at_ms: int
    platform_accepted_at_ms: int
```

Normalize each trusted schema independently, discard invalid entries, select the newest exact-current-release post-deployment candidate, and persist only hashed identifiers.
- [ ] **Step 5: Route legacy OpenClaw APIs through the generic implementation** without allowing arbitrary additional trusted paths.
- [ ] **Step 6: Run GREEN** with the Step 3 command and commit the facade, wrappers, and tests.

### Task 2: Hermes Durable Delivery Receipt Bridge

**Files:**
- Create: `eimemory/adapters/hermes/channel_delivery.py`
- Modify: `integrations/hermes/eimemory_hook/__init__.py`
- Modify: `integrations/hermes/eimemory_hook/plugin.yaml`
- Create: `tests/test_hermes_channel_delivery.py`
- Modify: `tests/test_hermes_plugin_package.py`

**Interfaces:**
- Produces: `register_hermes_channel_delivery(event, gateway, *, state_path=..., signal_path=..., now=None) -> bool`
- Consumes Hermes `gateway._session_key_for_source(source)`, adapter `register_post_delivery_callback(session_key, callback)`, and the `$HERMES_HOME/state.db` `delivery_obligations` table.

- [ ] **Step 1: Write failing eligibility tests.** A non-bot external event with a platform message ID registers one callback; local, deployment-replay, bot-authored, missing-message-ID, and invalid conversation-kind events do not.
- [ ] **Step 2: Write failing obligation tests.** A temporary Hermes database proves `pending`, `attempting`, and `failed` never write evidence, while `delivered` writes the current commit, Hermes owner, platform, conversation kind, inbound ID, obligation ID, and ordered timestamps.
- [ ] **Step 3: Write failing signal/idempotency tests.** A qualifying callback atomically updates the existing closure signal, duplicate execution is idempotent, and all database/file failures remain nonblocking.
- [ ] **Step 4: Run RED:** `.venv/bin/python -m pytest -q tests/test_hermes_channel_delivery.py tests/test_hermes_plugin_package.py`. Expected: bridge and fourth hook are absent.
- [ ] **Step 5: Implement the bridge and thin plugin hook:**

```python
def pre_gateway_dispatch(event: Any, gateway: Any, **_kwargs: Any) -> None:
    register_hermes_channel_delivery(event, gateway)
```

The callback queries only delivered obligations for the exact session created after the inbound event and writes through atomic replace with mode `0600`.
- [ ] **Step 6: Run GREEN** with the Step 4 command and commit the bridge, plugin metadata, and tests.

### Task 3: Runtime and Release-Closure Orchestration

**Files:**
- Modify: `eimemory/api/runtime.py`
- Modify: `eimemory/governance/release_closure.py`
- Modify: `eimemory/governance/release_closure_pending.py`
- Modify: `tests/test_release_closure.py`

**Interfaces:**
- Produces `Runtime.record_external_channel_acceptance(...)`.
- Preserves `Runtime.record_openclaw_channel_acceptance(...)` as a wrapper.

- [ ] **Step 1: Write failing closure tests** whose fake runtime exposes only the generic recorder. Assert initial closure and pending reconciliation accept Hermes evidence while retaining `channel_acceptance` report keys and the nonpolling waiting state.
- [ ] **Step 2: Run RED:** `.venv/bin/python -m pytest -q tests/test_release_closure.py`. Expected: production still calls the OpenClaw-specific method.
- [ ] **Step 3: Add the generic runtime method and change both closure paths** to call it with fixed defaults while preserving existing report/error contracts.
- [ ] **Step 4: Run GREEN** with the Step 2 command and commit runtime/orchestration changes.

### Task 4: Six-Domain Lineage Migration

**Files:**
- Modify: `eimemory/governance/release_lineage.py`
- Modify: `eimemory/governance/release_closure.py`
- Modify: `tests/test_release_lineage.py`
- Modify: `tests/test_release_closure.py`

**Interfaces:**
- Changes current domain `channel.openclaw` to `channel.delivery`.
- Authorizes `eimemory.external_channel.acceptance` and validates with `validate_external_channel_acceptance`.

- [ ] **Step 1: Write failing six-domain tests** asserting exactly six names, `channel.delivery` present, `channel.openclaw` absent, and closure evidence emitted under the generic key.
- [ ] **Step 2: Write failing source and historical-read tests.** The new domain accepts only generic current evidence; old `channel.openclaw` lineage remains auditable but cannot satisfy a changed current domain.
- [ ] **Step 3: Run RED:** `.venv/bin/python -m pytest -q tests/test_release_lineage.py tests/test_release_closure.py`.
- [ ] **Step 4: Rename the domain throughout current contracts** and add bounded legacy-key normalization only when reading historical stored lineage. Never emit or authorize the legacy key for current evidence.
- [ ] **Step 5: Run GREEN** with the Step 3 command and commit lineage migration.

### Task 5: Installer and Production Verification

**Files:**
- Modify: `deploy/verify_hermes_integration.py`
- Modify: `tests/test_hermes_deployment.py`
- Modify: `tests/test_deployment_tools.py`
- Modify: `deploy/systemd/README.md`

**Interfaces:**
- Hermes deployment verification requires four callbacks including `pre_gateway_dispatch`.
- Signal remains `/var/lib/eimemory/state/release-closure-channel-receipt.signal`.

- [ ] **Step 1: Write failing verification tests** that reject three-hook Hermes integration and require the fourth callback plus adapter-neutral operational language.
- [ ] **Step 2: Run RED:** `.venv/bin/python -m pytest -q tests/test_hermes_deployment.py tests/test_deployment_tools.py`.
- [ ] **Step 3: Require four hooks, verify the manifest callback, keep the real provider replay, and update the systemd documentation.**
- [ ] **Step 4: Run GREEN** with the Step 2 command and commit deployment verification changes.

### Task 6: Release and Real Production Closure

**Files:**
- Modify synchronized version metadata in `pyproject.toml`, `eimemory/__init__.py`, Codex/Hermes/OpenClaw manifests.

**Interfaces:**
- Releases `1.11.36` from the exact tested commit.

- [ ] **Step 1: Run all affected suites:** `.venv/bin/python -m pytest -q tests/test_external_channel_acceptance.py tests/test_openclaw_channel_acceptance.py tests/test_hermes_channel_delivery.py tests/test_hermes_plugin_package.py tests/test_release_closure.py tests/test_release_lineage.py tests/test_hermes_deployment.py tests/test_deployment_tools.py`.
- [ ] **Step 2: Bump all synchronized version metadata to `1.11.36`** using the repository's bounded version mechanism and rerun package/version tests.
- [ ] **Step 3: Run full verification:** `.venv/bin/python -m pytest -q --strict-markers tests`, then `git diff --check` and `git status --short`. Expected: zero failures and only intended files.
- [ ] **Step 4: Review the full diff** for secrets, arbitrary-path trust, synthetic-evidence bypasses, and weakened release authority; then commit and push `master`.
- [ ] **Step 5: Deploy the exact commit** with the immutable installer and verify current link, health, user RPC ownership, plugin roots, Hermes four-hook integration, timers/path units, zero failed units, and no storage transaction.
- [ ] **Step 6: Process one real post-deployment Hermes user turn** on an enabled external platform. Verify the durable external receipt and automatic closure wake-up; never inject or edit evidence manually.
- [ ] **Step 7: Re-run release closure and L5 readiness.** Claim product L5 complete only if the independent code-evolution transaction and observation requirements also pass; otherwise report their exact remaining evidence-bound gaps.
