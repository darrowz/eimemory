# eimemory Hermes hook bridge

This plugin registers official Hermes host callbacks (`pre_gateway_dispatch`,
`pre_llm_call`, `post_llm_call`) and synchronous `tool_execution` middleware.
Older hosts without middleware use `post_tool_call` instead. It resolves the exact
MemoryManager-owned `eimemory` provider through the packaged, session-scoped
registry.

Execution middleware observes the actual result before returning it unchanged;
it avoids Hermes' suppression of overlapping `post_tool_call` callbacks. No
background queue or model-supplied receipt is used. When another execution
middleware can rewrite the invocation, or the execution chain cannot be inspected,
attestation fails closed. The legacy hook path still inherits the old host's
observer concurrency limitations.

`eimemory_status`, recall, and remember are not verification evidence. To close a
verified turn, run an actual supported verification command (for example
`python -B -m pytest -p no:cacheprovider tests/test_hermes_plugin_package.py -q`)
through the host terminal tool, then invoke `eimemory_verify_outcome` in the same
session. No eligible receipt or multiple unfinalized turns must remain a refusal.
The deployment verifier is a real subprocess/official-loader replay, not proof
that an already-running gateway has reloaded the plugin. Verify memory write/readback
and a fresh gateway turn separately after deployment and gateway restart.

`pre_gateway_dispatch` binds a genuine external inbound message to Hermes'
durable delivery obligation. Evidence is emitted only after Hermes records the
response as successfully delivered; local, replay, webhook, bot, and unbound
events are ignored.

Install this folder under `$HERMES_HOME/plugins` together with the existing
`eimemory` provider plugin so closed-loop evidence can be produced on real host
tool calls:

```text
$HERMES_HOME/plugins/eimemory
$HERMES_HOME/plugins/eimemory_hook
```

Enable only the general hook plugin; the provider is selected independently:

```bash
hermes config set memory.provider eimemory
hermes plugins enable eimemory-hook
```

Requirements for attested tool receipts:

1. `EIMEMORY_HERMES_ATTESTATION_TOKEN_FILE` points to a non-empty private token file.
2. `EIMEMORY_ATTESTATION_HOST_PROFILE=operator-separated-v1`.
3. Runtime and producer credentials are different.
4. `EIMEMORY_ADAPTER_RECEIPT_HANDOFF_FILE` is shared by the hook and provider.

When these are not set, memory reads/writes keep working and attestation is
reported as unavailable.
