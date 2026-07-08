# Task 1 Report: OpenClaw Terminal Failure Gate

## Scope

- `tests/test_openclaw_outcome_hooks.py`
- `eimemory/adapters/openclaw/hooks.py`

## RED

Command:

```powershell
python -m pytest tests/test_openclaw_outcome_hooks.py::test_openclaw_agent_end_rate_limit_cooldown_success_is_bad_trace -q
```

Output:

```text
F                                                                        [100%]
================================== FAILURES ===================================
______ test_openclaw_agent_end_rate_limit_cooldown_success_is_bad_trace _______

tmp_path = WindowsPath('C:/Users/maiph/AppData/Local/Temp/pytest-of-maiph/pytest-0/test_openclaw_agent_end_rate_l0')
monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x0000013AE6F348A0>

    def test_openclaw_agent_end_rate_limit_cooldown_success_is_bad_trace(tmp_path, monkeypatch) -> None:
        runtime = Runtime.create(root=tmp_path)
        hooks = OpenClawMemoryHooks(runtime)
        traces: list[dict] = []

        def fake_record_outcome_trace(payload: dict, *, scope: dict) -> dict:
            traces.append(payload)
            return {"id": "trace-rate-limit"}

        monkeypatch.setattr(runtime, "record_outcome_trace", fake_record_outcome_trace, raising=False)

        result = hooks.on_agent_end(
            {
                "session_id": "sess-rate-limit",
                "agent_id": "main",
                "workspace_id": "repo-x",
                "user_id": "darrow",
                "query": "summarize current state",
                "task_context": {"task_type": "chat.reply", "bridge_status": "cooldown"},
                "outcome": {
                    "success": True,
                    "verified": True,
                    "notes": "OpenAI rate limit cooldown active; fallback response used.",
                },
            }
        )

>       assert result["outcome"]["outcome"] == "bad"
E       AssertionError: assert 'good' == 'bad'
E
E         - bad
E         + good

tests\test_openclaw_outcome_hooks.py:348: AssertionError
=========================== short test summary info ============================
FAILED tests/test_openclaw_outcome_hooks.py::test_openclaw_agent_end_rate_limit_cooldown_success_is_bad_trace
1 failed in 0.52s
```

## GREEN

Focused regression:

```powershell
python -m pytest tests/test_openclaw_outcome_hooks.py::test_openclaw_agent_end_rate_limit_cooldown_success_is_bad_trace -q
```

```text
.                                                                        [100%]
1 passed in 0.24s
```

Focused file:

```powershell
python -m pytest tests/test_openclaw_outcome_hooks.py -q
```

```text
..........                                                               [100%]
10 passed in 0.64s
```

## Files Changed

- `tests/test_openclaw_outcome_hooks.py`
- `eimemory/adapters/openclaw/hooks.py`
- `.superpowers/sdd/task-1-report.md`

## Implementation Notes

- Added the exact Task 1 regression for a `success=True` agent end event carrying cooldown/rate-limit diagnostics.
- Added a minimal terminal failure classifier in the OpenClaw hooks path.
- Threaded `failure_class` into both terminal outcome payloads and outcome-trace payloads.
- Forced classified terminal failures to record as `bad` with `source_trust="system_diagnostic"` even when upstream success/verification flags are present.

## Self-Review

- Kept the change inside the requested hook and test files.
- Confirmed the focused test failed before implementation and the focused test file is green after implementation.
- Checked the scoped diff for unrelated churn.

## Concerns

- The classifier is intentionally small and keyword-based. Task 1 now covers the specified cooldown/rate-limit case and plumbs the broader failure classes, but timeout/context-overflow/model/bridge variants do not yet have explicit tests in this file.
