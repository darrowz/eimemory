from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
import os
import subprocess
import threading
import time
from typing import Any

from .completion_timing import (safe_timing, safe_child_timing, measure,
    FAILURE_SCHEMA, MAX_FAILURE_BYTES, failure_category)


@dataclass(frozen=True, slots=True)
class LLMResult:
    text: str
    provider_id: str
    model_id: str
    diagnostics: dict[str, Any] | None = field(default=None, compare=False, hash=False, repr=False)


class CommandCompletionError(RuntimeError):
    """Fixed message and safe metadata only, never child output or exception text."""
    def __init__(self, category=''):
        super().__init__('command_completion_failed')
        self.failure_category = failure_category(category)


def _failure_frame(raw):
    """Accept one bounded complete JSON frame on stdout of a NONZERO child only.

    Never scan stderr or extract JSON fragments from mixed logs. Invalid frames
    remain failures without bridge diagnostics. Duplicate keys are rejected.
    """
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_FAILURE_BYTES:
        return '', {}
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate_key')
            result[key] = value
        return result
    def reject_constant(_):
        raise ValueError('non_finite_json')
    try:
        data = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                          parse_constant=reject_constant)
        if (not isinstance(data, dict) or set(data) != {'schema', 'error', 'diagnostics'}
                or data['schema'] != FAILURE_SCHEMA
                or not failure_category(data['error'])
                or not isinstance(data['diagnostics'], dict)):
            return '', {}
        return data['error'], safe_child_timing(data['diagnostics'])
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return '', {}


class CommandLLMClient:
    """Provider-neutral JSON-stdin/JSON-stdout LLM command client."""

    def __init__(self, argv: list[str] | tuple[str, ...], *, timeout_seconds: int = 90) -> None:
        normalized = tuple(str(item) for item in argv)
        if not normalized or any(not item.strip() for item in normalized):
            raise ValueError("LLM command argv is empty")
        self.argv = normalized
        self.timeout_seconds = max(1, min(600, int(timeout_seconds)))
        self._prepared_process = None
        self._prepared_spawn_ms = None

    def prepare(self) -> None:
        """Measure Popen now and carry it with the prestarted process.

        This does not assert child readiness. Preparation may overlap retrieval;
        the carried spawn duration is not additive to the request critical path.
        No new call sites or Python prewarming policy are introduced here.
        """
        if self._prepared_process is not None:
            raise RuntimeError('LLM command already prepared')
        # Popen returning is NOT interpreter-import completion or provider readiness.
        timing = {}
        try:
            with measure(timing, 'command_spawn_ms'):
                process = subprocess.Popen(
                    list(self.argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except Exception as exc:
            exc.completion_timing = safe_timing(timing)
            raise
        self._prepared_process = process
        self._prepared_spawn_ms = timing['command_spawn_ms']

    def close(self) -> None:
        process, self._prepared_process = self._prepared_process, None
        self._prepared_spawn_ms = None
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait()
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()

    def complete(self, *, system_prompt: str, user_prompt: str, json_mode: bool = False) -> LLMResult:
        timings = {}
        try:
            result = self._complete(system_prompt=system_prompt, user_prompt=user_prompt,
                                    json_mode=json_mode, timings=timings)
        except Exception as exc:
            # Parent timeout/kill reports only stages actually observed by parent.
            exc.completion_timing = safe_timing(timings)
            raise
        return replace(result, diagnostics=safe_timing(timings))

    def _complete(self, *, system_prompt, user_prompt, json_mode, timings):
        request = json.dumps(
            {
                "system_prompt": str(system_prompt),
                "user_prompt": str(user_prompt),
                "json_mode": bool(json_mode),
                "deadline_unix_ms": int((time.time() + self.timeout_seconds) * 1000),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        process, self._prepared_process = self._prepared_process, None
        spawn_ms, self._prepared_spawn_ms = self._prepared_spawn_ms, None
        completed = run_bounded_command(
            list(self.argv),
            request.encode("utf-8"),
            timeout_seconds=self.timeout_seconds,
            timings=timings,
            **({'prepared_process': process, 'prepared_spawn_ms': spawn_ms}
               if process is not None else {}),
        )
        if completed[0] != 0:
            category, bridge_timing = _failure_frame(completed[1])
            timings.update(bridge_timing)
            # A diagnostic frame is never an answer, even when it contains timings.
            raise CommandCompletionError(category)
        with measure(timings, 'command_decode_ms'):
            stdout = completed[1].decode("utf-8")
            if not stdout:
                raise ValueError("LLM command returned an empty or oversized response")
            payload = json.loads(stdout)
            if not isinstance(payload, dict):
                raise ValueError("LLM command response must be an object")
            timings.update(safe_child_timing(payload.get('diagnostics')))
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
    timeout_seconds: float,
    prepared_process: Any = None,
    prepared_spawn_ms: float | None = None,
    timings: dict | None = None,
) -> tuple[int, bytes, bytes]:
    timings = timings if timings is not None else {}
    timings['command_prepared'] = prepared_process is not None
    if prepared_process is not None:
        # Missing prestart measurement stays absent; never time a reference lookup.
        timings.update(safe_timing({'command_spawn_ms': prepared_spawn_ms}))
    if len(request) > _MAX_COMMAND_STREAM_BYTES:
        if prepared_process is not None:
            if prepared_process.poll() is None:
                prepared_process.kill()
            prepared_process.wait()
            for stream in (prepared_process.stdin, prepared_process.stdout, prepared_process.stderr):
                if stream is not None:
                    stream.close()
        raise ValueError("LLM command request is oversized")
    if prepared_process is not None:
        process = prepared_process
    else:
        with measure(timings, 'command_spawn_ms'):
            process = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    io_started = time.monotonic()
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
    timings['command_io_ms'] = (time.monotonic() - io_started) * 1000
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
