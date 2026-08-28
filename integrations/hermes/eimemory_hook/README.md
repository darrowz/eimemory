# eimemory Hermes hook bridge

This plugin registers official Hermes host callbacks (`pre_gateway_dispatch`,
`pre_llm_call`, `post_llm_call`, `post_tool_call`) and resolves the exact
MemoryManager-owned `eimemory` provider through the packaged, session-scoped
registry.

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
