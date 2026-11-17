# Optional native input-origin companion

This is a separate source artifact. Installing eimemory does not install this
host patch, and this artifact asserts no production installation or acceptance.
The deployment owner must review the patch before a coordinated host switch.

## Exact source pair

The manifest pins two exact current preimages and one absent file. The copied
host HEAD is `2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19`; HEAD alone is insufficient.
The retained preimages include the existing lifecycle, snapshot and profile
boundary changes. Do not replace them with pristine upstream files.

The companion changes only:

- `gateway/run_turn.py`: bind the gateway's actual admitted event around the
  existing agent execution.
- `gateway/run_turn_runner.py`: bind the actual framed input on the agent worker.
- `gateway/eimemory_input_origin.py`: a new native capability module.

The last file must be absent, including no symlink or other unknown entry. Never
overwrite an existing file under that name. The separate lifecycle patch and
its five before/after hashes and historical 26-case replay are unchanged.
This companion advances the lifecycle postimage of `run_turn_runner.py`; a
composite verifier must use this companion's postimage for that file and the
original lifecycle postimages for the other four. Reverse this companion first
before testing or reversing the earlier lifecycle patch.

Immediately before application, re-read all actual preimage hashes and the new
file's absence, quiesce the owning gateway, run exact `git apply --check`, apply
the whole companion, and verify all three postimages. Restart is required.
The retained SHA readback is an observation at its timestamp, not authorization
to overwrite later source changes. Apply and rollback must preserve the native
profile configuration, sessions, queues and user memory.

## Provenance contract

`host_attested_external_input` means an admitted external platform input. It
does not prove that an actor is human, that an answer was delivered, or that a
task or answer was correct. `host_attested_internal_input` and `unknown` do not
receive natural-input learning credit.

The native producer requires the actual MessageEvent admission flag and stable
platform-message or canonical submission identity plus chat identity. It uses
the host's internal/heartbeat/machinery metadata and supported platform/source
fields. A plain `internal=False` or text marker never establishes external
provenance. API/legacy inputs without this evidence stay unknown. The host's
`event.edit_revision`, when supplied, is included in message identity. An
adapter that does not supply it conservatively shares the message's identity
across edits and may undercount inputs; this patch does not invent revisions.

The runtime profile/home is verified on the actual agent worker, after native
profile routing. The capability is bound to the exact visible input, session,
profile and native owner thread. Revocable shared leases prevent copied
ContextVars from retaining authority after the input/event ends. A request
kwargs dictionary is never accepted as a native receipt.

The plugin captures the capability at pre-call and requires the same live
receipt, explicit original host turn, query, scope and sources at post-call.
The server compares the canonical query digest and full decision namespace and
independently resolves the registered profile/home. Operator acceptance cannot
receive external credit. The RPC uses the existing private Hermes producer
credential, separate from the ordinary memory bearer, and signs stored metadata
with a separate domain of the existing receipt keyring. This reuses the existing
host trust boundary; it does not claim model-inaccessible same-user OS keys.

Enable collection explicitly with `EIMEMORY_CAPTURE_INPUT_ORIGIN=1`; default is
off. Missing companion, credentials, receipt key or trustworthy input identity
leaves provenance unknown and keeps ordinary memory behavior running.

## Storage and rollback

The new `adapter.attest_input_origin` method and
`proactive_input_origins_v1` table are independent of legacy effect signals.
The table is created only by the authenticated writer; read-only statistics
never create it. Each decision is append-only: identical claims replay, conflicts
are rejected, and invalid HMACs become unknown. Origin metadata follows its
authoritative decision's existing deletion lifecycle through a foreign key.

Origin retry files have their own versioned prefix/schema, 16-entry and 64 KiB
bounds, producer-key HMAC and interprocess locks. Every enqueue/ACK starts from
fresh signed disk state. A late ACK deletes only the exact sent payload; it
cannot remove a new item or revive an acknowledged cached item. Network calls
hold a separate consumer lock and run outside the disk mutation lock. Missing
keys, damaged signatures or token rotation leave origin retry unavailable;
they never contaminate the legacy effect queue. No raw question or answer is
stored in origin records/retry files.

An older eimemory reader ignores this new table and retry filename. No new phase
or label is added to the old effect-signal ledger. Historical decisions without
valid origin remain unknown; no text-based backfill is performed.

Statistics retain both non-acceptance candidate decision rows and unique
host-attested admitted inputs. Unique identity includes profile/home, exact
channel/scope/sources, platform, chat and message/edit identity. Offline learning
credits one earliest decision per admitted input, chosen before outcome labels.
The canonical decision is chosen across the complete retained exact namespace
before time-window filtering, so a retry cannot gain new credit in a later
trial or monitoring window. The window's candidate rows remain reportable.
Existing observational reports retain their candidate rows. Recall ordering,
refusal behavior and the signed `recall_policy_view` are unchanged. Future
learning/timed-trial evaluation may wait or follow its existing timeout rules
when older evidence lacks origin; deploying this behavior needs a review of any
existing trial state.

Collection currently occurs at the post-LLM callback. An admitted event that
never reaches that callback has no origin record and stays unknown. These
statistics describe recorded host-attested inputs, not all external traffic;
callback completion can introduce selection bias and is not task success.

## Callback and verification boundary

The inspected native `turn_finalizer._apply_output_hooks` emits post_llm_call
once after the tool loop, for a nonempty uninterrupted finalizer response. That
response can include an abnormal-exit explanation and precedes final surrogate
sanitization. It is not a platform-send receipt or persistent message-UID proof.
This companion adds no final-completion or delivery interface.

The isolated owner-seam tests apply the exact patch, execute its real added
owner statements across async-to-worker context propagation and exception
cleanup, and use a real isolated eimemory RPC bridge. They replace outer gateway
services and do not certify the full native gateway, platform transport or
production lifecycle. Full host tests must use the host's documented runner in
its complete dependency environment before deployment acceptance.

The new answer observation is journal-only v2. It keeps the complete pre-call
host-input digest separate from the bounded exact pre-query digest and the
runtime-normalized recall query digest. The full callback answer digest remains
an observation, not a verified task outcome. Old v1 logs remain historical.
