from __future__ import annotations

from dataclasses import dataclass
import json
import os
import subprocess
import threading
import time
from typing import Any


@dataclass(frozen=True, slots=True)
class LLMResult:
    text: str
    provider_id: str
    model_id: str


class CommandLLMClient:
    """Provider-neutral JSON-stdin/JSON-stdout LLM command client."""

    def __init__(self, argv: list[str] | tuple[str, ...], *, timeout_seconds: int = 90) -> None:
        normalized = tuple(str(item) for item in argv)
        if not normalized or any(not item.strip() for item in normalized):
            raise ValueError("LLM command argv is empty")
        self.argv = normalized
        self.timeout_seconds = max(1, min(600, int(timeout_seconds)))

    def complete(self, *, system_prompt: str, user_prompt: str, json_mode: bool = False) -> LLMResult:
        request = json.dumps(
            {
                "system_prompt": str(system_prompt),
                "user_prompt": str(user_prompt),
                "json_mode": bool(json_mode),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        completed = run_bounded_command(
            list(self.argv),
            request.encode("utf-8"),
            timeout_seconds=self.timeout_seconds,
        )
        if completed[0] != 0:
            raise RuntimeError(f"LLM command failed with exit code {completed[0]}")
        stdout = completed[1].decode("utf-8")
        if not stdout:
            raise ValueError("LLM command returned an empty or oversized response")
        payload = json.loads(stdout)
        if not isinstance(payload, dict):
            raise ValueError("LLM command response must be an object")
        text = str(payload.get("text") or "").strip()
        provider_id = str(payload.get("provider_id") or "").strip()
        model_id = str(payload.get("model_id") or "").strip()
        if not text or not provider_id or not model_id:
            raise ValueError("LLM command response requires text, provider_id, and model_id")
        return LLMResult(text=text, provider_id=provider_id, model_id=model_id)


_MAX_COMMAND_STREAM_BYTES = 2_000_000


def run_bounded_command(
    argv: list[str],
    request: bytes,
    *,
    timeout_seconds: int,
) -> tuple[int, bytes, bytes]:
    if len(request) > _MAX_COMMAND_STREAM_BYTES:
        raise ValueError("LLM command request is oversized")
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout = bytearray()
    stderr = bytearray()
    overflow = threading.Event()
    writer_error: list[BaseException] = []

    def read_stream(stream: Any, target: bytearray) -> None:
        try:
            while True:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    return
                if len(target) + len(chunk) > _MAX_COMMAND_STREAM_BYTES:
                    overflow.set()
                    return
                target.extend(chunk)
        finally:
            stream.close()

    def write_request() -> None:
        try:
            if process.stdin is not None:
                process.stdin.write(request)
                process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            writer_error.append(exc)
        finally:
            if process.stdin is not None:
                process.stdin.close()

    assert process.stdout is not None and process.stderr is not None
    threads = [
        threading.Thread(target=read_stream, args=(process.stdout, stdout), daemon=True),
        threading.Thread(target=read_stream, args=(process.stderr, stderr), daemon=True),
        threading.Thread(target=write_request, daemon=True),
    ]
    for thread in threads:
        thread.start()

    deadline = time.monotonic() + timeout_seconds
    timed_out = False
    while process.poll() is None:
        if overflow.wait(timeout=0.02):
            process.kill()
            break
        if time.monotonic() >= deadline:
            timed_out = True
            process.kill()
            break
    process.wait()
    for thread in threads:
        thread.join(timeout=1)
    if timed_out:
        raise subprocess.TimeoutExpired(argv, timeout_seconds)
    if overflow.is_set():
        raise ValueError("LLM command returned an oversized response")
    if writer_error and process.returncode == 0:
        raise RuntimeError("LLM command closed stdin before reading the request")
    return int(process.returncode), bytes(stdout), bytes(stderr)


def llm_client_from_env(feature: str = "") -> CommandLLMClient | None:
    prefix = str(feature or "").strip().upper().replace("-", "_")
    specific = f"EIMEMORY_{prefix}_LLM_COMMAND" if prefix else ""
    raw = str((os.environ.get(specific) if specific else "") or os.environ.get("EIMEMORY_LLM_COMMAND") or "").strip()
    if not raw:
        return None
    try:
        argv = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{specific or 'EIMEMORY_LLM_COMMAND'} must be a JSON argv array") from exc
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) and item.strip() for item in argv):
        raise ValueError(f"{specific or 'EIMEMORY_LLM_COMMAND'} must be a non-empty JSON argv array")
    timeout_name = f"EIMEMORY_{prefix}_LLM_TIMEOUT_SECONDS" if prefix else ""
    timeout_raw = (os.environ.get(timeout_name) if timeout_name else "") or os.environ.get("EIMEMORY_LLM_TIMEOUT_SECONDS") or "90"
    try:
        timeout = int(timeout_raw)
    except ValueError:
        timeout = 90
    return CommandLLMClient(argv, timeout_seconds=timeout)
