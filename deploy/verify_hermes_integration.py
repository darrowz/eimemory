from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid


def _rpc_result(raw: str, *, operation: str) -> dict:
    payload = json.loads(raw)
    if payload.get("ok") is not True or not isinstance(payload.get("result"), dict):
        raise RuntimeError(f"{operation} RPC transport failed")
    result = payload["result"]
    if result.get("ok") is not True:
        raise RuntimeError(f"{operation} RPC operation failed")
    return result


def _rpc_tool_result(
    provider,
    tool_name: str,
    args: dict,
    *,
    operation: str,
    attempts: int = 3,
) -> dict:
    if attempts < 1:
        raise ValueError("attempts must be positive")
    for attempt in range(1, attempts + 1):
        try:
            return _rpc_result(
                provider.handle_tool_call(tool_name, args),
                operation=operation,
            )
        except RuntimeError as exc:
            if "RPC transport failed" not in str(exc) or attempt >= attempts:
                raise
            time.sleep(0.25 * attempt)
    raise AssertionError("unreachable")


def verify_hermes_integration(
    *,
    repo_root: str | Path,
    commit: str,
    pytest_target: str,
    hermes_agent_root: str | Path | None = None,
    test_python: str | Path | None = None,
) -> dict:
    repo = Path(repo_root).expanduser().resolve(strict=True)
    agent_root = Path(
        hermes_agent_root
        or (Path(os.environ.get("HERMES_HOME", "")).expanduser() / "hermes-agent")
    ).resolve(strict=True)
    for import_root in (agent_root, repo):
        normalized = str(import_root)
        if normalized not in sys.path:
            sys.path.insert(0, normalized)

    from eimemory.adapters.hermes.provider_registry import get_hermes_provider
    from hermes_cli.plugins import discover_plugins, get_plugin_manager, invoke_hook
    from plugins.memory import load_memory_provider

    provider = load_memory_provider("eimemory")
    if provider is None:
        raise RuntimeError("official Hermes loader did not return eimemory provider")
    session_id = f"hermes-release-{commit[:12]}-{uuid.uuid4().hex[:8]}"
    provider.initialize(
        session_id,
        hermes_home=os.environ.get("HERMES_HOME", ""),
        platform="deployment-replay",
        agent_context="primary",
        agent_identity=os.environ.get("EIMEMORY_AGENT_ID", "hongtu"),
        agent_workspace=os.environ.get("EIMEMORY_WORKSPACE_ID", "embodied"),
        user_id=os.environ.get("EIMEMORY_USER_ID", "darrow"),
    )
    try:
        discover_plugins(force=True)
        manager = get_plugin_manager()
        hook_plugin = next(
            (row for row in manager.list_plugins() if row.get("name") == "eimemory-hook"),
            None,
        )
        if not hook_plugin or hook_plugin.get("enabled") is not True or hook_plugin.get("hooks") != 3:
            raise RuntimeError("Hermes hook plugin is not enabled with all official callbacks")
        if get_hermes_provider(session_id) is not provider:
            raise RuntimeError("Hermes hook registry is not bound to the MemoryManager provider")

        status = _rpc_tool_result(provider, "eimemory_status", {}, operation="status")
        if status.get("channel") != "hermes" or status.get("authority_mode") != "per_channel":
            raise RuntimeError("Hermes channel authority is not active")
        if status.get("attestation_available") is not True:
            raise RuntimeError("Hermes operator-separated attestation is unavailable")

        memory_text = f"Hermes release {commit[:12]} passed its official provider replay."
        remembered = _rpc_tool_result(
            provider,
            "eimemory_remember",
            {
                "text": memory_text,
                "event_id": f"hermes-release-replay:{commit}",
                "memory_type": "deployment_evidence",
                "title": "Hermes deployment replay",
            },
            operation="remember",
        )
        if remembered.get("authoritative") is not True:
            raise RuntimeError("Hermes memory write was not authoritative")
        recalled = _rpc_tool_result(
            provider,
            "eimemory_recall",
            {"query": f"Hermes release {commit[:12]} official provider replay", "limit": 8},
            operation="recall",
        )

        query = f"Verify Hermes deployment {commit[:12]}"
        provider.prefetch(query, session_id=session_id)
        turn_id = f"turn-{uuid.uuid4().hex}"
        invoke_hook(
            "pre_llm_call",
            session_id=session_id,
            user_message=query,
            conversation_history=[],
            is_first_turn=True,
            model="deployment-replay",
            platform="deployment-replay",
            turn_id=turn_id,
        )

        test_interpreter = Path(test_python or repo / ".venv" / "bin" / "python").expanduser()
        if not test_interpreter.is_file():
            raise RuntimeError("trusted pytest interpreter is missing")
        command = f"{test_interpreter} -B -m pytest -p no:cacheprovider {pytest_target} -q"
        test_env = dict(os.environ)
        test_env["PYTHONPATH"] = str(repo)
        test_env["PYTHONDONTWRITEBYTECODE"] = "1"
        started = time.monotonic()
        completed = subprocess.run(
            [
                str(test_interpreter),
                "-B",
                "-m",
                "pytest",
                "-p",
                "no:cacheprovider",
                pytest_target,
                "-q",
            ],
            cwd=repo,
            env=test_env,
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        output = (completed.stdout + "\n" + completed.stderr).strip()
        if completed.returncode != 0:
            raise RuntimeError("real Hermes deployment replay test failed")
        result_envelope = json.dumps(
            {"output": output, "exit_code": completed.returncode, "error": None},
            ensure_ascii=False,
            sort_keys=True,
        )
        invoke_hook(
            "post_tool_call",
            tool_name="terminal",
            args={"command": command},
            result=result_envelope,
            task_id=turn_id,
            session_id=session_id,
            turn_id=turn_id,
            api_request_id=turn_id,
            tool_call_id=f"call-{uuid.uuid4().hex}",
            duration_ms=duration_ms,
            status="success",
        )
        terminal = _rpc_tool_result(
            provider,
            "eimemory_verify_outcome",
            {"result": "Official Hermes deployment replay passed."},
            operation="verified outcome",
        )
        invoke_hook(
            "post_llm_call",
            session_id=session_id,
            user_message=query,
            assistant_response="Hermes deployment replay completed.",
            conversation_history=[],
            model="deployment-replay",
            platform="deployment-replay",
            turn_id=turn_id,
        )
        provider.sync_turn(
            query,
            "Hermes deployment replay completed.",
            session_id=session_id,
        )
        return {
            "ok": True,
            "provider_shared": True,
            "hook_count": 3,
            "attestation_available": True,
            "memory_authoritative": True,
            "recall_ok": recalled.get("ok") is True,
            "real_replay_exit_code": completed.returncode,
            "receipt_consumed": bool(terminal.get("outcome_trace")),
        }
    finally:
        provider.shutdown()


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the live official Hermes/eimemory closed loop.")
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--pytest-target", default="tests/test_hermes_plugin_package.py")
    parser.add_argument("--hermes-agent-root", default="")
    parser.add_argument("--test-python", default="")
    args = parser.parse_args()
    report = verify_hermes_integration(
        repo_root=args.repo_root,
        commit=args.commit,
        pytest_target=args.pytest_target,
        hermes_agent_root=args.hermes_agent_root or None,
        test_python=args.test_python or None,
    )
    print(json.dumps(report, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
