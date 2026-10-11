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

## Acceptance scope and evidence counts

Keep these conclusions separate:

| Acceptance scope | What must be covered | Limit of the conclusion |
| --- | --- | --- |
| Team's actual use | Each participant's verified conversation identity and all four checks through the hosts they actually use | Covers the named participants and actual usage tested |
| Mainline formal dataset gate | Trusted, release-authorized cases under `production_recall_gate_policy.v5`, totaling at least 3 cases and 3 labels | Covers the dataset's observed host channels and exact scope; does not certify all enabled or installed hosts |
| Cross-client product acceptance | The explicitly declared product/client scope, with evidence for each claimed supported client and its required use cases | Untested clients remain outside the accepted coverage; a Hermes-only dataset cannot certify them |

The mainline v5 contract derives `required_channels` from channels observed in
the dataset and sets `required_per_channel=0`. A qualifying dataset containing
only Hermes can meet its coverage prerequisite; it does not need samples from
unused OpenClaw or Codex clients. Every other label, authorization, ranking and
quality requirement still applies. Version 1.14.64 changes the minimum from
15 to 3 at the operator's explicit request. The supplied production readback
remains 1.14.63 under v4; its results and historical denominator are unchanged.
The previous wording requiring five cases from all three clients was incorrect
for v4 as well. Neither v4 nor v5 has a fixed per-client quota.

Observed channels are not an enabled-host inventory. Confirm the actual team
and product scope from authorized session/configuration evidence and compare
it with tested coverage. Do not omit an enabled host or participant and report
the whole declared scope as accepted. A team that truly uses only Hermes can
assess that actual usage separately while leaving cross-client product coverage
unverified. This document does not claim that the team's full enabled-host
inventory has already been established.

The number **3** is the new minimum evidence floor, not a statistical
accuracy guarantee or a member quota. Labels identify relevant records and
their grades for each query; they are not another three independent query trials.
Every eligible case needs at least one valid accepted label, so three valid cases
already imply at least three labels. The separate label-count check also exposes
missing evidence; many labels on one case cannot replace missing cases or
cover another unlabelled case. See the
[count interpretation](evaluation.md#what-the-case-and-label-minimum-establishes).

The floor applies within one dataset's existing exact scope. Cases from other
members' scopes cannot be pooled to pass that scope's gate. Member-specific
team results remain separate; no new cross-user authority is granted.

Keep the existing error-rate, noise and ranking thresholds in the
[evaluation guide](evaluation.md). Reaching the count floor does not establish
each participant's coverage or create a trusted verdict for a controlled probe.

## Using existing memory for four checks

Existing memories can supply the target records without inserting fictional
memories. First verify that each target is active, supported by its source and
readable under the participant's actual identity. Keep the target's exact
record reference, physical scope, source and independently reviewed query-label
evidence in the private case packet.

Use the same target set for these related checks:

| Check | Use of existing memory | Evidence kept separately |
| --- | --- | --- |
| Known-memory natural questions | Ask paraphrases of the supported fact through the real host | Expected record labels, actual delivered references and ranks |
| Source readback | Read back each returned record through that participant's authorized path | Physical scope, source and provenance confirmation |
| No-answer near neighbors | Ask a related question with an unsupported detail, independently verify that no authorized memory supports it | A no-answer judgment and observed unsupported returns; no invented positive record label |
| Cross-member permissions | Use a private target as a negative contrast under other participants, and existing shared targets as permitted contrasts | Identity, existing access policy and observed access outcome for each direction |

Three trusted positive case labels can support the positive-case minimum; they
are relevance judgments, not the names of three check types. Natural questions
and source readback can share a case. No-answer and permission contrasts need
their own probes and outcomes; three successful lookups alone do not cover all
four checks. Do not substitute another participant's private case or label when
the current participant has no independently confirmed target.

Questions automatically derived from memory are controlled acceptance probes,
even when they sound natural. They can support functional checks or a separately
labelled memory benchmark; they cannot fill the natural-production case count
merely because the expected record is already known. Formal cases still need
actual production conversation captures, trusted label evidence and verified
release authorization. This is a procedure for authorized operators, not a
claim that a generator or team test has been run.

Until participant-level evidence covers all four checks, report: **shared
service available, fixed smoke passed; accurate natural-question recall under
each colleague's actual conversation identity remains unverified**. This guide
records the required coverage; it does not report an executed team acceptance.
