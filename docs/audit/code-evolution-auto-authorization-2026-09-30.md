# Automatic code.evolution authorization (1.14.19)

## Why
honrui closure for 1.14.17 was blocked only by `code.evolution: strict_code_evolution_receipt_required`. The strict receipt needs a `CodeEvolutionTransaction`, which only the code-evolution pipeline can produce (a one-shot v2 policy at `/etc/eimemory/code-automation-policy.v2.json` plus focused, regression and full-suite verification receipts). A human-authored release cannot honestly obtain one. The user has declared self-evolution auto-authorized.

## Contract
- **Authority:** `code-evolution-auto-authorizer`, class `automatic`, `operator_authorization: false`. The record source is `eimemory.code_evolution.auto_authorization`. This is separate from the strict transaction path and from any operator identity.
- **Signature:** HMAC over the evidence-receipt keyring (`EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY`, the keyring file, or `/etc/eimemory/evidence-receipt.env`).
- **Bound fields:** policy version, scope, release commit, version, deployment receipt, session, ancestor commit and receipt, changed domains, and the changed evolution paths with their digest.
- **Minting:** during `record_release_lineage`, after both receipts verify. It requires no unknown production paths, the flag on and the kill switch absent. It is idempotent per (receipt, ancestor).
- **Acceptance:** the code.evolution gate first checks the strict receipt, then the automatic authorization. Verification recomputes every bound field.
- **Revocation and off switch:** `eimemory learn code-evolution-auto-authorization-revoke`, or `EIMEMORY_CODE_EVOLUTION_AUTO_AUTHORIZATION=0`. Either one fails closed on revalidation.
- **Unchanged:** all other domain gates, acceptance checks and thresholds.
