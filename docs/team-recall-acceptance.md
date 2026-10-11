# Team recall acceptance

## Current evidence

The operator's October 11, 2026 readback reports a healthy shared service and
10/10 nightly smoke on **1.14.63 / `4fbcbbc9`**. Formal accepted cases remain
**0/15**. These results do not establish accurate recall in each colleague's
real Feishu conversation.

| Participant | Actual-conversation recall acceptance |
| --- | --- |
| 鸿泰 | Unverified |
| 鸿欣 | Unverified |
| 小马哥 | Unverified |

Earlier controlled checks for these participants returned empty results, but
their actual session identities and environments were not fully verified.
Those observations establish neither a current recall fault nor successful
recall. No current participant-level receipt was supplied. See the
[release-bound receipt history](acceptance-status.md).

## Verify the conversation boundary first

Use the participant's real Feishu conversation through its actual host adapter.
Record the resolved `tenant_id`, `agent_id`, `workspace_id` and `user_id`, host
channel and source authorization, plus the runtime release and deployed commit.
Check the effective client configuration and RPC destination, and verify the
memory store and any existing scope aliases or shared-read policy that apply.
Use authorized configuration and session evidence; a display name, host name,
or configured default user alone does not establish the effective identity.

Feishu is the message entry point. Its host can be Hermes or another adapter;
derive the actual host channel from the session rather than assigning `feishu`
as the evaluation channel. Operator CLI queries and shared-service smoke do not
substitute for the participant's conversation path.

Keep exact identities in the authorized private evidence store. Public summaries
should use participant aliases and receipt references, excluding conversation
text, memory bodies and credentials. An unresolved identity or environment
makes a check **not evaluable**, with a recorded reason; it cannot count as a
pass or establish a retrieval-algorithm failure.

## Required coverage for each participant

| Check | Required evidence and result |
| --- | --- |
| Natural questions about known memory | Independently confirm an existing, authorized target before the check. Ask ordinary paraphrases through the real conversation, without embedding record IDs or copying a title as the only query. Compare the delivered record IDs and ranks with the independently labelled target. |
| No-answer near neighbors | Use semantically related questions independently labelled as having no supported answer in that participant's authorized memory. Record any unsupported returns. An empty result alone does not certify a correct no-answer. |
| Cross-member permission boundaries | Exercise private-memory checks in both directions for every participant pair. Private items must remain unavailable to unauthorized participants. Where an existing shared-read policy permits access, verify that permitted sharing still works. Preserve current authorization. |
| Source readback | Through the same participant's authorized read path, verify returned record IDs, physical scopes, source IDs and provenance against the underlying permitted records. Confirm that the rendered memory came from those records; a response's source claim alone is insufficient. |

Record failures as well as passes. Keep empty results, wrong results, access
denials and identity failures distinguishable. Review a missing known target
through eligibility, scope/source filtering, deduplication, ranking and delivery
only after the actual conversation boundary is verified. Do not infer that an
offered candidate was delivered or used.

## Persist and assess results

For each case, retain the participant's exact authorized scope, host channel,
source policy, conversation capture reference, runtime identity, independent
label evidence, expected and delivered record references, ranks, permission
outcome and source-readback references. Persist a pass, fail or not-evaluable
verdict with diagnostic reasons. Summarize coverage and outcomes per participant
and per check; a pooled average must not hide an untested participant or an
access-boundary failure. Reuse existing capture and report fields where
available; this checklist does not introduce a new runtime report schema.

Deliberate functional acceptance must retain its generated classification.
For Codex MCP, the existing
[explicit recall workflow](explicit-recall-acceptance.md) produces
acceptance-only reports; it cannot turn controlled probes into natural
production gold. Use the actual host's supported capture path for other
adapters. Do not replay the original observation and replace its result with
a later successful lookup.

The formal production gate still requires trusted labels, verified release
authorization and **15 qualifying cases, including at least five from each of
OpenClaw, Codex and Hermes**. Three participant checks do not replace that
channel coverage. Conversely, the formal case count alone does not establish
coverage of every team member's actual identity. Keep the existing error-rate,
noise and ranking thresholds in the [evaluation guide](evaluation.md).

Until participant-level evidence covers all four checks, report: **shared
service available, fixed smoke passed; accurate natural-question recall under
each colleague's actual conversation identity remains unverified**. This guide
records the required coverage; it does not report an executed team acceptance.
