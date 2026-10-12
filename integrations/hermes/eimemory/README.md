# eimemory for Hermes Agent

This standalone plugin implements Hermes Agent's native `MemoryProvider`
contract. It does not patch Hermes core. Install this directory together with
`integrations/hermes/eimemory_hook`. The production installer creates
release-bound links under `$HERMES_HOME/plugins`; for a manual installation,
copy the provider to `$HERMES_HOME/plugins/eimemory`, copy the hook to
`$HERMES_HOME/plugins/eimemory_hook`, and install the matching `eimemory` Python package in
the Hermes environment. Select the provider in `config.yaml`:

```yaml
memory:
  provider: eimemory
plugins:
  enabled:
    - eimemory-hook
```

Configure the authenticated runtime in the Hermes profile environment:

```text
EIMEMORY_RPC_URL=http://honxin:8091/
EIMEMORY_RPC_TOKEN=<strong RPC bearer token>
EIMEMORY_TENANT_ID=default
EIMEMORY_AGENT_ID=hongtu
EIMEMORY_WORKSPACE_ID=embodied
EIMEMORY_USER_ID=<user identity>
```

Hermes memory is authoritative only inside `authority_mode=per_channel` and a
scope such as `embodied::channel::hermes`. OpenClaw and Codex cannot recall it.
The provider supports prefetch, bounded background turn sync, built-in memory
mirroring, pre-compression context, session lifecycle, session switching, and
four explicit tools for recall, remember, verified outcome, and status.

Calls are fail-open with short timeouts, a single bounded writer, a single
prefetch worker, bounded cache, and a bounded failure ledger. Service failure
never blocks Hermes. The provider deliberately ignores the full conversation history
supplied to session/compression hooks; only bounded completed turns are
sent. A session end is lifecycle evidence only. An explicit verification
string is diagnostic, not trusted L5 proof.

Trusted Hermes task evidence is fail-closed behind the operator-separated
host profile. The host plugin receives a private
`EIMEMORY_HERMES_ATTESTATION_TOKEN_FILE` plus
`EIMEMORY_ATTESTATION_HOST_PROFILE=operator-separated-v1`, loads the token
once, and removes producer configuration from the inherited environment before
ordinary child tools can run. `EIMEMORY_ADAPTER_RECEIPT_HANDOFF_FILE` stores
only bounded receipt IDs; the protected runtime database remains authoritative
for the exact pending set and atomic terminal claim. Without this separated
profile, memory and recall keep working, `attestation_available` is false, and
Hermes tasks do not count toward L5.

The packaged session registry binds official host callbacks to the exact
MemoryManager-owned provider instance without forcing one global provider
across concurrent gateway sessions.

## Validation boundary

The operator's latest reported nightly is 1.14.67 / `4257d23c`
(`ref_5df3562505ca`), which ran 23:02:39–23:11:25 and exited 0.
Its durable report and original logs omit `production_recall_collection`,
so this run's new/existing case counts, new pending IDs and collection
skip reasons are unknown. `recall_semantic_relevance.new_count=9` counts
new semantic observations, not new production cases.

The last detailed business readback is 1.14.66: 0/3 accepted cases and
zero passing reviews. No new deployment receipt, participant-level acceptance
or verification of the 1.14.67 review-reference projection was supplied.
Mainline 1.14.69 adds controlled checks from authorized existing memories,
reviewed knowledge applicability for exact capability gaps, and governed
Hermes v2 maintenance contexts. No production execution or business acceptance
of those paths is reported; quality and authority requirements remain unchanged.

Hook registration and execution success do not certify natural-query quality
or capability maturity. See [receipt-based status](../../../docs/acceptance-status.md),
[evaluation](../../../docs/evaluation.md) and [team acceptance](../../../docs/team-recall-acceptance.md).
For repeated questions, new 1.14.66 decisions retain removed candidate IDs and
qualifying historical references; existing count-only decisions cannot be backfilled.
