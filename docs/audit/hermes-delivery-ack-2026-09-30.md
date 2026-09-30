# Hermes delivery acknowledgement (1.14.18)

## Symptom (honrui, 1.14.17)
Hermes `research.task` decisions had 0 injected items (`ever_injected=0`). Production auto-review reported `no_candidate_delivered` and `semantic_judgment_not_delivered`, and host `used` feedback was never recorded.

## Root cause: Hermes host order (read-only, hermes-agent 2ef41d2b58)
1. `agent/turn_context.py`: `_collect_pre_llm_call_context` (the `pre_llm_call` plugin hook) runs before `_memory_turn_start_and_prefetch`, which calls `memory_manager.prefetch_all(flatten_message_text(original_user_message))`.
2. `prefetch_all` strips skill scaffolding and calls `provider.prefetch(query, session_id)` in a thread with a timeout. On timeout the host gets `""` and drops the result.
3. The fenced context is stamped into the current user message: the `api_content` sidecar for string content (skipped when nothing was injected), or an appended text part for list content.
4. `agent/turn_finalizer.py` fires `post_llm_call(user_message=original_user_message, assistant_response=..., turn_id=..., conversation_history=list(messages))`.
5. `run_agent.py` calls `queue_prefetch_all(user_text)` after a completed turn, as a warm-up for the **next** turn.

The adapter acknowledged delivery in `pre_llm_call`, before the current turn's prefetch existed, so no acknowledgement ever bound to the injected decision.

## Evidence rule (adapter)
- `pre_llm_call` binds the host turn id only.
- `post_llm_call`: `delivered = offered ∩ (citations in this turn's model-facing user bytes ∪ citations in the assistant response)`. It acknowledges `delivered` and sends `used = cited ∩ delivered`.
- Returned-but-not-injected context, foreign citations and failed acknowledgements never count as delivered or used.

## Host change
None required. The fix is in `eimemory/adapters/hermes/provider_core.py` and `integrations/hermes/eimemory_hook/__init__.py`.
