from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid


def verify_code_implementation_binding(
    repo_root: str | Path,
    *,
    expected_digest: str = "",
) -> dict[str, str]:
    """Validate the release-bound v2 provider contract before live replay."""

    from eimemory.adapters.hermes.code_implementation import (
        BINDING_ID,
        CAPABILITY_ID,
        IMPLEMENTATION_DIGEST,
        REVISION_ID,
        implementation_digest,
    )

    actual = implementation_digest(Path(repo_root).expanduser().resolve(strict=True))
    expected = str(expected_digest or IMPLEMENTATION_DIGEST or actual).strip().lower()
    if not expected or actual != expected:
        raise RuntimeError("code implementation digest mismatch")
    return {
        "capability_id": CAPABILITY_ID,
        "revision_id": REVISION_ID,
        "binding_id": BINDING_ID,
        "implementation_digest": actual,
    }


# These are diagnostic enums, not patterns accepting arbitrary server strings.
# The method is the requested provider route; it does not prove an RPC was sent.
_RPC_METHODS = {
    "eimemory_status": "adapter.status",
    "eimemory_remember": "adapter.remember",
    "eimemory_recall": "adapter.prefetch",
    "eimemory_verify_outcome": "adapter.record_terminal",
}
_RPC_REASONS = frozenset({
    "configuration_missing", "configuration_invalid", "invalid_request",
    "timeout", "connection_error", "http_error", "response_too_large",
    "invalid_response", "transport_error", "adapter_unavailable", "circuit_open",
})
_RPC_ERRORS = frozenset({
    "invalid_request", "unknown_method", "unauthorized", "attestation_unauthorized",
    "capability_evidence_unauthorized", "original_proactive_release_unverified",
    "effect_signal_acceptance_decision_rejected", "internal_error", "request_timeout",
    "hermes_expected_sources_mismatch", "hermes_response_scope_mismatch",
})


def _safe_rpc_diagnostic(payload: dict, *, method: str, attempt: int) -> dict:
    """Project only fixed codes and typed scalars from one observed tool reply."""
    safe = {"method": method if method in _RPC_METHODS.values() else "unknown",
            "attempt": attempt}
    for name in ("ok", "bypassed"):
        if type(payload.get(name)) is bool:
            safe[name] = payload[name]
    diagnostic = payload.get("diagnostic")
    diagnostic = diagnostic if isinstance(diagnostic, dict) else {}
    reason = diagnostic.get("reason")
    if not isinstance(reason, str) or reason not in _RPC_REASONS:
        reason = payload.get("error")
    if isinstance(reason, str) and reason in _RPC_REASONS:
        safe["reason"] = reason
    status = diagnostic.get("http_status")
    if type(status) is int and 100 <= status <= 599:
        safe["http_status"] = status
    nested = payload.get("result")
    nested = nested if isinstance(nested, dict) else {}
    for code in (diagnostic.get("rpc_error"), payload.get("error"), nested.get("error")):
        if isinstance(code, str) and code in _RPC_ERRORS:
            safe["rpc_error"] = code
            break
    return safe


class _RPCFailure(RuntimeError):
    def __init__(self, message: str, diagnostics: list[dict], *, retryable: bool = False):
        self.message = message
        self.diagnostics = diagnostics
        self.retryable = retryable
        super().__init__(message + "; diagnostics=" + json.dumps(diagnostics, sort_keys=True))


def _rpc_result(
    raw: str, *, operation: str, method: str = "unknown", attempt: int = 1,
) -> dict:
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, RecursionError):
        payload = None
    if not isinstance(payload, dict):
        diagnostic = _safe_rpc_diagnostic({}, method=method, attempt=attempt)
        diagnostic["reason"] = "invalid_response"
        raise _RPCFailure(f"{operation} RPC transport failed", [diagnostic]) from None
    diagnostic = _safe_rpc_diagnostic(payload, method=method, attempt=attempt)
    if payload.get("ok") is not True or payload.get("bypassed") is True:
        bypassed = payload.get("bypassed") is True
        details = payload.get("diagnostic")
        details = details if isinstance(details, dict) else {}
        # An HTTP 4xx/business result is a refusal, not a retry permission.
        # Preserve the legacy bounded retry for an unclassified empty bypass,
        # but do not retry a provider-local adapter_unavailable without evidence.
        status = diagnostic.get("http_status", 0)
        nested = payload.get("result")
        refusal = (400 <= status < 500 or bool(details.get("rpc_error"))
                   or "rpc_error" in diagnostic
                   or (isinstance(nested, dict) and nested.get("ok") is False)
                   or payload.get("error") not in (None, "", "adapter_unavailable", "circuit_open"))
        retryable = bypassed and not refusal and (
            diagnostic.get("reason") in {"timeout", "connection_error", "circuit_open"}
            or (not details and not payload.get("error"))
        )
        failure_kind = "transport" if bypassed and not refusal else "operation"
        raise _RPCFailure(f"{operation} RPC {failure_kind} failed", [diagnostic],
                          retryable=retryable)
    if not isinstance(payload.get("result"), dict):
        raise _RPCFailure(f"{operation} RPC transport failed", [diagnostic])
    result = payload["result"]
    if result.get("ok") is not True:
        raise _RPCFailure(f"{operation} RPC operation failed", [diagnostic])
    return result


def _invoke_rpc_tool(provider, tool_name: str, args: dict, *, operation: str, attempt: int) -> str:
    try:
        return provider.handle_tool_call(tool_name, args)
    except Exception:
        # The invocation happened, but no reply was observed. Do not fabricate
        # ok/bypassed/status, or expose the exception (including its cause).
        diagnostic = _safe_rpc_diagnostic(
            {}, method=_RPC_METHODS.get(tool_name, "unknown"), attempt=attempt,
        )
        raise _RPCFailure(f"{operation} RPC local invocation failed", [diagnostic]) from None


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
    failures = []
    for attempt in range(1, attempts + 1):
        try:
            raw = _invoke_rpc_tool(provider, tool_name, args, operation=operation, attempt=attempt)
            return _rpc_result(
                raw, operation=operation, method=_RPC_METHODS.get(tool_name, "unknown"), attempt=attempt,
            )
        except _RPCFailure as exc:
            failures.extend(exc.diagnostics)
            if not exc.retryable or attempt >= attempts:
                raise _RPCFailure(exc.message, failures) from None
            time.sleep(0.25 * attempt)
    raise AssertionError("unreachable")


def _timed_recall_tool_result(provider, args: dict, *, timeout_seconds: float) -> tuple[dict, dict]:
    """One explicit recall through the official tool path, timed, never retried.

    A retry after a client timeout would hit a warm server cache and hide the
    latency a real Hermes tool call sees, so latency is part of acceptance.
    """

    started = time.monotonic()
    raw = _invoke_rpc_tool(provider, "eimemory_recall", args, operation="recall", attempt=1)
    elapsed = time.monotonic() - started
    latency = {
        "recall_latency_ms": int(elapsed * 1000),
        "recall_timeout_seconds": float(timeout_seconds),
        "recall_within_timeout": elapsed <= float(timeout_seconds),
    }
    try:
        result = _rpc_result(raw, operation="recall", method="adapter.prefetch", attempt=1)
    except _RPCFailure as exc:
        diagnostic = exc.diagnostics[-1]
        if diagnostic.get("bypassed") is True:
            reason = diagnostic.get("reason", "transport")
            raise _RPCFailure(
                f"recall tool call failed ({reason}) after {latency['recall_latency_ms']}ms "
                f"with timeout {timeout_seconds:g}s", exc.diagnostics,
            ) from None
        raise
    if not latency["recall_within_timeout"]:
        raise RuntimeError(
            f"recall tool call took {latency['recall_latency_ms']}ms, over its {timeout_seconds:g}s timeout"
        )
    return result, latency


def _deploy_agent() -> str:
    from eimemory.identity import HONGTU_AGENT_ID  # env-configurable legacy default

    return os.environ.get("EIMEMORY_AGENT_ID") or os.environ.get("EIMEMORY_DEPLOY_SCOPE_AGENT") or HONGTU_AGENT_ID


def _deploy_workspace() -> str:
    from eimemory.identity import HONGTU_WORKSPACE_ID  # env-configurable legacy default

    return (os.environ.get("EIMEMORY_WORKSPACE_ID") or os.environ.get("EIMEMORY_DEPLOY_SCOPE_WORKSPACE")
            or HONGTU_WORKSPACE_ID)


def _initialize_deployment_replay(provider, session_id: str) -> None:
    scope = {
        "tenant_id": os.environ.get("EIMEMORY_TENANT_ID") or "default",
        "agent_id": _deploy_agent(),
        "workspace_id": _deploy_workspace(),
        "user_id": os.environ.get("EIMEMORY_USER_ID") or os.environ.get("USER") or "operator",
    }
    provider.initialize(
        session_id,
        hermes_home=os.environ.get("HERMES_HOME", ""),
        platform="deployment-replay",
        agent_context="primary",
        agent_identity=scope["agent_id"],
        # The native request label selects its registered profile. The expected
        # scope below still checks every coordinate after the existing Hermes
        # channel normalization, including a job's base workspace spelling.
        agent_workspace="hermes",
        user_id=scope["user_id"],
        expected_scope=scope,
    )


def verify_hermes_integration(
    *,
    repo_root: str | Path,
    commit: str,
    pytest_target: str,
    hermes_agent_root: str | Path | None = None,
    test_python: str | Path | None = None,
    expected_implementation_digest: str = "",
) -> dict:
    repo = Path(repo_root).expanduser().resolve(strict=True)
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    code_implementation = verify_code_implementation_binding(
        repo,
        expected_digest=expected_implementation_digest,
    )
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
    _initialize_deployment_replay(provider, session_id)
    try:
        discover_plugins(force=True)
        manager = get_plugin_manager()
        hook_plugin = next(
            (row for row in manager.list_plugins() if row.get("name") == "eimemory-hook"),
            None,
        )
        execution_capture = bool(
            hook_plugin and hook_plugin.get("middleware", 0)
            and manager._middleware.get("tool_execution")
        )
        minimum_hooks = 3 if execution_capture else 4
        if not hook_plugin or hook_plugin.get("enabled") is not True or hook_plugin.get("hooks", 0) < minimum_hooks:
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
        from eimemory.core.budgets import explicit_recall_timeout_seconds

        recalled, recall_latency = _timed_recall_tool_result(
            provider,
            {"query": f"Hermes release {commit[:12]} official provider replay", "limit": 8},
            timeout_seconds=explicit_recall_timeout_seconds(),
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
        command = f"python -B -m pytest -p no:cacheprovider {pytest_target} -q"
        test_env = dict(os.environ)
        test_env["PYTHONPATH"] = str(repo)
        test_env["PYTHONDONTWRITEBYTECODE"] = "1"
        test_env["PATH"] = str(test_interpreter.parent) + os.pathsep + test_env.get("PATH", "")
        completed = None
        def execute_test(_args):
            nonlocal completed
            if _args != {"command": command} or completed is not None:
                raise RuntimeError("Hermes replay invocation was rewritten or repeated")
            completed = subprocess.run(
                [
                    "python",
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
            return result_envelope

        call_context = dict(
            task_id=turn_id, session_id=session_id, turn_id=turn_id,
            api_request_id=turn_id, tool_call_id=f"call-{uuid.uuid4().hex}",
        )
        started = time.monotonic()
        if execution_capture:
            from hermes_cli.middleware import run_tool_execution_middleware
            run_tool_execution_middleware(
                "terminal", {"command": command}, execute_test, **call_context,
            )
        else:
            result_envelope = execute_test({"command": command})
            invoke_hook(
                "post_tool_call", tool_name="terminal", args={"command": command},
                result=result_envelope, duration_ms=int((time.monotonic() - started) * 1000),
                status="success", **call_context,
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
            "hook_count": hook_plugin.get("hooks"),
            "attestation_available": True,
            "memory_authoritative": True,
            "recall_ok": recalled.get("ok") is True,
            **recall_latency,
            "real_replay_exit_code": completed.returncode,
            "receipt_consumed": bool(terminal.get("outcome_trace")),
            "code_implementation": code_implementation,
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
    parser.add_argument("--expected-implementation-digest", default="")
    args = parser.parse_args()
    report = verify_hermes_integration(
        repo_root=args.repo_root,
        commit=args.commit,
        pytest_target=args.pytest_target,
        hermes_agent_root=args.hermes_agent_root or None,
        test_python=args.test_python or None,
        expected_implementation_digest=args.expected_implementation_digest,
    )
    print(json.dumps(report, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
