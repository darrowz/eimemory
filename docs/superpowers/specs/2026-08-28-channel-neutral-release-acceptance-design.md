# Channel-Neutral Release Acceptance Design

## Problem

Release closure currently treats one concrete adapter and transport as the
product boundary. The lineage domain is `channel.openclaw`, the recorder reads
only `openclaw_reply_delivery_state.json`, and a qualifying session must contain
`:feishu:direct:`. A release therefore cannot close when a real user talks to
the same product through Hermes or through a non-Feishu messaging platform.

The product requirement is broader: a current release must prove that a real
inbound user turn produced a response accepted by an external messaging
platform. OpenClaw, Hermes, Feishu, Telegram, Discord, Slack, WhatsApp, Weixin,
and future adapters are delivery implementations, not lineage domains.

## Decision

Rename the lineage domain to `channel.delivery` and introduce a channel-neutral
acceptance facade. The facade normalizes receipts from trusted adapter ledgers
and records one release-bound `external_channel_acceptance.v1` evidence record.

The existing OpenClaw ledger remains a supported input for compatibility.
Hermes gains a release-bound plugin callback that observes a genuine inbound
gateway event, waits until Hermes' durable delivery obligation is marked
`delivered`, and then appends a normalized receipt to the external-channel
ledger. No manual CLI assertion or synthetic deployment replay can create a
qualifying receipt.

## Alternatives Considered

1. Add `channel.hermes` beside `channel.openclaw`. This is small but repeats the
   same coupling and requires another product-domain change for every adapter.
2. Use a channel-neutral lineage domain with adapter-specific receipt readers.
   This is the selected approach because it fixes the product boundary while
   preserving each adapter's native delivery semantics and rollback isolation.
3. Require every gateway to call a new central receipt RPC. This is a possible
   later convergence path, but it couples this release to coordinated changes
   in every external gateway and is unnecessary for the immediate defect.

## Evidence Contract

A normalized delivery candidate contains:

- `transport_owner`: the runtime that owns delivery, initially `openclaw` or
  `hermes`;
- `platform`: the external messaging platform, such as `feishu`, `telegram`,
  `discord`, `slack`, `whatsapp`, or `weixin`;
- `conversation_kind`: `direct`, `group`, `channel`, `thread`, or `forum`;
- the inbound event identifier and durable delivery receipt identifier;
- `runtime_commit`;
- `received_at_ms` and `platform_accepted_at_ms`.

The recorder accepts a candidate only when all of the following hold:

1. its runtime commit is the exact current deployed commit;
2. both timestamps are positive and acceptance is not before receipt;
3. the inbound event was received after the current deployment receipt;
4. the transport, platform, conversation kind, inbound identifier, and
   delivery identifier are non-empty and structurally valid;
5. the source ledger has the exact registered schema and is a regular file;
6. the candidate was produced by a trusted adapter path, not by a CLI-provided
   arbitrary path or user-authored evidence record.

The persisted learning record stores only digests of channel, conversation,
inbound, and delivery identifiers. It is authorized for `channel.delivery` and
is revalidated against the exact current release authority.

## Adapter Flows

### OpenClaw

The facade translates `openclaw_reply_delivery.v2` entries into normalized
candidates. Existing requirements for `platform_accepted`, delivery message ID,
runtime commit, timestamps, and a real messaging session remain. The Feishu
session marker determines `platform=feishu` and `conversation_kind=direct`; the
product contract no longer contains those literals.

### Hermes

The release-bound `eimemory-hook` registers `pre_gateway_dispatch`. For a real
non-bot external message with an inbound platform message ID, it resolves the
Hermes session key and registers a one-shot post-delivery callback on the
owning adapter. After the response pipeline finishes, the callback queries the
exact Hermes delivery obligation created for that session after the inbound
event. Only `state=delivered` qualifies; Hermes sets this state only after the
platform adapter returns a successful `SendResult`.

The callback writes `external_channel_delivery.v1` atomically and updates the
existing low-frequency release-closure signal. It never records local, API,
deployment-replay, bot-authored, missing-message-ID, failed, pending, or
attempting events.

## Lineage and Compatibility

The six lineage domains become:

1. `memory.recall`
2. `memory.governance`
3. `channel.delivery`
4. `storage.integrity`
5. `deployment.runtime`
6. `code.evolution`

New release lineage records emit only `channel.delivery`. Historical
`channel.openclaw` records remain readable as legacy lineage input but cannot
satisfy a new current-release gate by inheritance when channel-related code
changed. The new current-release evidence must use the channel-neutral source
and validator.

Release-closure report field names remain `channel_acceptance` so operational
consumers do not need a report-schema migration. Runtime keeps the old
`record_openclaw_channel_acceptance` method as a compatibility wrapper while
all production closure paths call `record_external_channel_acceptance`.

## Failure Handling

- Missing or malformed adapter ledgers fail closed.
- A candidate from the wrong release or from before deployment is ignored.
- Hermes ledger/database read or external-ledger write failures never block the
  user reply; they leave release closure pending and emit bounded diagnostics.
- Multiple adapters may produce candidates; the newest valid current-release
  candidate wins deterministically.
- The signal is only a wake-up hint. The reconciler rereads and revalidates the
  durable receipt, so a forged or stale signal cannot close the release.

## Verification

Tests must prove:

- a current-release Hermes delivery satisfies channel acceptance;
- non-Feishu Hermes platforms qualify;
- OpenClaw Feishu remains compatible;
- local, bot-authored, pre-deployment, wrong-commit, pending/failed, and missing
  identifier rows do not qualify;
- lineage accepts only the new authorized source for `channel.delivery`;
- release closure and pending reconciliation use the generic recorder;
- the Hermes hook writes a receipt only after a durable delivered obligation;
- deployment installs and verifies the fourth Hermes hook;
- affected suites and the full repository suite pass before deployment.

## Acceptance Criteria

1. No current product contract or lineage domain is named after OpenClaw,
   Hermes, Feishu, or another specific adapter/platform.
2. A real post-deployment Hermes user turn on any supported external messaging
   platform can generate the evidence needed by release closure.
3. A real post-deployment OpenClaw Feishu turn continues to work.
4. Synthetic, local, failed, stale, wrong-release, and user-authored evidence
   remains non-qualifying.
5. Release closure automatically resumes from the durable signal and records
   `channel.delivery` gate evidence.
