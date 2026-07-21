# eimemory for Hermes Agent

This standalone plugin implements Hermes Agent's native `MemoryProvider`
contract. It does not patch Hermes core. Copy this directory to
`$HERMES_HOME/plugins/eimemory`, install the matching `eimemory` Python
package in the Hermes environment, then select it in `config.yaml`:

```yaml
memory:
  provider: eimemory
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
sent. A session end is lifecycle evidence only. L5 task evidence requires the
`eimemory_verify_outcome` tool and an explicit verification string.
