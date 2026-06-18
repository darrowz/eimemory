# eimemory Six-Bug Fix Batch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Fix six business-logic defects found during local audit, with narrow code changes and targeted tests for each behavioral contract.

**Architecture:** Keep changes local to existing modules. Do not introduce new services, new persistence formats, or background schedulers. Runtime/API fixes should preserve current return shapes while correcting success semantics. Governance fixes should fail closed where safety evidence is unavailable.

**Tech Stack:** Python 3.11+, pytest, existing eimemory package layout, existing CLI entry points.

---

## Scope

Fix these six issues:

1. Prompt L2 gate generation always passes because `not prompt_target or True` is a tautology.
2. `Runtime.collect_external_sources()` reports top-level success even when one or more source results failed.
3. `StateMachine` accepts unsafe `record_id` values that can escape the state root.
4. `_run_with_time_box()` returns on timeout while the worker can continue mutating state.
5. `compute_business_impact()` treats "no recall samples" as a negative regression.
6. LoCoMo evaluation drops turns emitted by the converter because `_turn_text()` ignores nested `messages`.

Do not push, merge, or create a PR from this plan without a separate user request.

## Files

- `eimemory/governance/autonomous_learning.py`
- `eimemory/api/runtime.py`
- `eimemory/cli/main.py`
- `eimemory/governance/state_machine.py`
- `eimemory/autonomous/loop.py`
- `eimemory/autonomous/business_feedback.py`
- `eimemory/evaluation/locomo.py`
- `eimemory/evaluation/_text.py` (new)
- `tests/test_prompt_l2_gate_bundle.py` (new or existing)
- `tests/test_runtime.py` or `tests/test_runtime_collect_aggregation.py`
- `tests/test_cli_intake_loop.py`
- `tests/test_state_machine.py` or `tests/test_state_machine_path_traversal.py`
- `tests/test_loop.py` or `tests/test_loop_hard_timebox.py`
- `tests/test_business_feedback.py`
- `tests/test_locomo_adapter.py`

## Task 1: Make Generated Prompt Gates Fail Closed

- [ ] In `eimemory/governance/autonomous_learning.py`, replace the generated prompt gate tautology with explicit prompt-target handling.

Use this shape:

```python
def _prompt_gate_result(*, prompt_target: bool, check_name: str) -> dict[str, Any]:
    if not prompt_target:
        return {
            "passed": None,
            "skipped": True,
            "reason": "not_prompt_target",
            "cases": 0,
        }
    return {
        "passed": False,
        "skipped": False,
        "reason": f"{check_name}_unavailable",
        "cases": 0,
    }
```

Then build `prompt_shadow_eval` and `prompt_injection_check` through that helper.

- [ ] Add or update tests in `tests/test_prompt_l2_gate_bundle.py`.

Minimum assertions:

```python
def test_generated_prompt_gate_fails_closed_for_prompt_targets():
    bundle = _gate_bundle_for_candidate(
        "prompt_policy",
        evidence=[],
        scope=ScopeRef.from_dict({"agent_id": "hongtu"}),
    )

    assert bundle["prompt_shadow_eval"]["passed"] is False
    assert bundle["prompt_injection_check"]["passed"] is False


def test_generated_prompt_gate_blocks_l2_promotion(runtime):
    candidate = distill_capability_candidate(
        runtime,
        experiment_id="exp_prompt_gate",
        promotion_target="prompt_policy",
        content={"kind": "prompt_policy"},
        evaluation={
            "verdict": "pass",
            "scores": {"recall": 0.9},
            "gate_bundle": _gate_bundle_for_candidate(
                "prompt_policy",
                evidence=[],
                scope=ScopeRef.from_dict({"agent_id": "hongtu"}),
            ),
        },
    )

    result = promote_candidate(
        runtime,
        candidate.candidate_id,
        eval_result=candidate.evaluation,
        health={"ok": True},
    )

    assert result.ok is False
    assert result.reason == "prompt_safety_gate"
```

- [ ] Run:

```powershell
python -m pytest tests/test_prompt_l2_gate_bundle.py -q
```

Expected: all tests pass.

## Task 2: Aggregate External Source Failures Correctly

- [ ] In `eimemory/api/runtime.py`, update `Runtime.collect_external_sources()` so the top-level `ok` is false when any source result has `ok is False`.

Keep existing per-source payloads, and add failure metadata without breaking callers:

```python
failed_sources = [
    str(result.get("source_id", ""))
    for result in results
    if isinstance(result, dict) and result.get("ok") is False
]
all_ok = not failed_sources

return {
    "ok": all_ok,
    "source_count": len(results),
    "results": results,
    "failed_sources": failed_sources,
    "error_count": len(failed_sources),
}
```

- [ ] In `eimemory/cli/main.py`, update the `intake collect` command so it exits nonzero when `report["ok"]` is false.

Use this return contract:

```python
return 0 if report.get("ok", True) else 1
```

- [ ] Add runtime tests for mixed success/failure and all-success cases.

Use a fake source fetcher rather than live network calls.

- [ ] Add a CLI test in `tests/test_cli_intake_loop.py` by monkeypatching `Runtime.collect_external_sources()` to return `{"ok": False, ...}` and asserting the command returns `1`.

- [ ] Run:

```powershell
python -m pytest tests/test_runtime.py tests/test_cli_intake_loop.py -q
```

Expected: all tests pass.

## Task 3: Validate State Machine Record IDs

- [ ] In `eimemory/governance/state_machine.py`, add record-id validation before building paths.

Use a strict validator and a real path containment check. Do not use string-prefix checks for containment.

```python
_RECORD_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
_FORBIDDEN_RECORD_ID_FRAGMENTS = ("..", "/", "\\", "\x00")


def _validate_record_id(record_id: str) -> str:
    if not isinstance(record_id, str) or not record_id:
        raise ValueError("record_id must be a non-empty string")
    if any(fragment in record_id for fragment in _FORBIDDEN_RECORD_ID_FRAGMENTS):
        raise ValueError("record_id contains unsafe path fragments")
    if not _RECORD_ID_PATTERN.fullmatch(record_id):
        raise ValueError("record_id contains unsupported characters")
    return record_id


def _assert_inside_root(root: Path, target: Path) -> None:
    resolved_root = root.resolve()
    resolved_target = target.resolve()
    try:
        resolved_target.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("record path escapes state root") from exc
```

- [ ] Call `_validate_record_id(record_id)` in `_path()` before creating the filename.

- [ ] Call `_assert_inside_root(self.root, path)` before reading or writing JSON files.

- [ ] Add tests that reject:

```python
["../escape", "..\\escape", "/absolute", "nested/name", "", "a" * 201]
```

- [ ] Add a positive test for existing valid IDs such as:

```python
["exp-123", "capability.v1", "agent:hongtu"]
```

- [ ] Run:

```powershell
python -m pytest tests/test_state_machine.py -q
```

Expected: all tests pass.

## Task 4: Make Time-Box Timeout Stop Side Effects

- [ ] In `eimemory/autonomous/loop.py`, replace the daemon-thread implementation of `_run_with_time_box()` with a child-process implementation for hard time boxes.

Contract:

- Return the worker result when it finishes before the timeout.
- Propagate worker failure as an exception with the original exception type name and message in the error text.
- On timeout, terminate the child process, join it, and raise `TimeoutError`.
- If the callable cannot be started in a child process, fail closed with `TypeError` rather than falling back to a thread.

Implementation outline:

```python
def _time_box_worker(fn: Callable[[], Any], queue: Any) -> None:
    try:
        queue.put(("ok", fn()))
    except BaseException as exc:
        queue.put(("error", type(exc).__name__, str(exc)))
```

Then in `_run_with_time_box()`:

```python
queue = multiprocessing.Queue(maxsize=1)
process = multiprocessing.Process(target=_time_box_worker, args=(fn, queue))
try:
    process.start()
except Exception as exc:
    raise TypeError("time-boxed functions must be child-process compatible") from exc

process.join(seconds)
if process.is_alive():
    process.terminate()
    process.join()
    raise TimeoutError(f"experiment exceeded time box of {seconds:.2f}s")
```

Read from the queue after successful completion and raise `RuntimeError` for worker-side errors:

```python
status, *payload = queue.get_nowait()
if status == "ok":
    return payload[0]
raise RuntimeError(f"time-boxed function failed: {payload[0]}: {payload[1]}")
```

- [ ] Add tests in `tests/test_loop.py` or `tests/test_loop_hard_timebox.py`.

Required tests:

1. A fast top-level function returns its value.
2. A top-level function that raises produces an error containing the worker exception type and message.
3. A slow top-level function that writes a "before" marker, sleeps longer than the time box, then writes an "after" marker is terminated before writing "after".

Use filesystem markers under `tmp_path` for the side-effect test.

- [ ] Run:

```powershell
python -m pytest tests/test_loop.py -q
```

Expected: all tests pass.

## Task 5: Make No-Data Business Feedback Neutral

- [ ] In `eimemory/autonomous/business_feedback.py`, update `compute_business_impact()` so empty recall samples do not synthesize a regression.

Use this contract:

```python
if not recall_samples:
    return {
        "dimension": "business_impact",
        "average_recall": None,
        "baseline": baseline_recall,
        "delta": None,
        "sample_count": 0,
        "no_data": True,
    }
```

For non-empty samples, preserve the current numeric average and delta behavior and set `"no_data": False`.

- [ ] Search for current production consumers before editing downstream behavior:

```powershell
rg -n "compute_business_impact|business_impact|average_recall|no_data" eimemory tests scripts
```

At audit time there was no production rollback caller for this metric. If a caller appears, update it to treat `no_data` as neutral/inconclusive, not as a regression.

- [ ] Update tests in `tests/test_business_feedback.py`.

Required assertions:

```python
impact = compute_business_impact([], baseline_recall=0.6)
assert impact["average_recall"] is None
assert impact["delta"] is None
assert impact["sample_count"] == 0
assert impact["no_data"] is True
```

Also assert non-empty samples still compute numeric values.

- [ ] Run:

```powershell
python -m pytest tests/test_business_feedback.py -q
```

Expected: all tests pass.

## Task 6: Read Nested LoCoMo Messages

- [ ] Add `eimemory/evaluation/_text.py` with a small recursive text extractor that supports the converter output shape.

Required behavior:

- If a turn has `content`, `text`, or `message`, return that value as text.
- If a turn has `messages`, recursively extract text from each message item.
- Prefix message text with the role/speaker when present.
- Ignore missing or empty content.
- Return a single newline-joined string.

Implementation outline:

```python
def extract_text_from_turn(turn: Mapping[str, Any]) -> str:
    direct = turn.get("content", turn.get("text", turn.get("message")))
    if direct:
        return _with_speaker(turn, str(direct))

    messages = turn.get("messages")
    if isinstance(messages, Sequence) and not isinstance(messages, (str, bytes)):
        parts = [
            extract_text_from_turn(message)
            for message in messages
            if isinstance(message, Mapping)
        ]
        return "\n".join(part for part in parts if part)

    return ""
```

- [ ] In `eimemory/evaluation/locomo.py`, update `_turn_text()` to use `extract_text_from_turn()`.

- [ ] Keep LME behavior unchanged in this batch. Do not refactor LME unless a failing test demonstrates the same nested-message bug there.

- [ ] Add tests in `tests/test_locomo_adapter.py`.

Required fixture shape:

```python
{
    "haystack_sessions": [
        {
            "session_id": "session_1",
            "turns": [
                {
                    "turn_id": "dia_1",
                    "messages": [
                        {"role": "speaker_a", "content": "Caroline moved to Denver."},
                        {"role": "speaker_b", "content": "She started a robotics club."},
                    ],
                }
            ],
        }
    ],
    "qa": [
        {
            "question_id": "q1",
            "question": "Where did Caroline move?",
            "answer": "Denver",
            "evidence": ["dia_1"],
        }
    ],
}
```

Assertions:

```python
dataset = load_locomo_dataset(path)
assert "Caroline moved to Denver" in dataset.sessions[0].chunks[0].text

result = run_locomo_evaluation(index, dataset)
assert result.queries[0].returned_ids
assert result.queries[0].rank is not None
```

- [ ] Run:

```powershell
python -m pytest tests/test_locomo_adapter.py -q
```

Expected: all tests pass.

## Verification

- [ ] Run focused tests for all six fixes:

```powershell
python -m pytest `
  tests/test_prompt_l2_gate_bundle.py `
  tests/test_runtime.py `
  tests/test_cli_intake_loop.py `
  tests/test_state_machine.py `
  tests/test_loop.py `
  tests/test_business_feedback.py `
  tests/test_locomo_adapter.py `
  -q
```

Expected: all selected tests pass.

- [ ] Run syntax verification:

```powershell
python -m compileall eimemory scripts
```

Expected: compile succeeds.

- [ ] Optionally run the full suite if time permits:

```powershell
python -m pytest -q
```

If the full suite times out, record the timeout duration and include the focused-test results in the handoff.

## Handoff Notes

- Report each fixed bug with the file changed and the test that covers it.
- Report any existing unrelated worktree changes separately.
- Do not include generated baseline files unless they are intentionally part of the requested deliverable.
- Do not push to `origin`, merge branches, or open a PR unless the user explicitly asks for that next step.
