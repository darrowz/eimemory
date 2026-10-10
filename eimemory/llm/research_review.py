"""Explicit review routes, then the service user's installed Hermes runtime."""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
from collections.abc import Mapping

from .command_client import CommandLLMClient, llm_client_from_env, run_bounded_command

RUNTIME_ERROR = "research_review_hermes_runtime_unavailable"
CONFIG_ERROR = "research_review_hermes_configuration_invalid"
_BRIDGE = Path(__file__).with_name("hermes_review_command.py").resolve()


def _launcher_command(binary: str, environment: Mapping[str, str]) -> list[str] | None:
    code, out, _ = run_bounded_command(
        [binary, "--print-runtime-command", "--module", "runpy"], b"",
        timeout_seconds=10, environment=environment)
    if code != 0:
        return None  # Older installations have no machine-readable launcher API.
    try:
        argv = json.loads(out)
        if (not isinstance(argv, list) or len(argv) not in (4, 5)
                or not all(isinstance(value, str) and value.strip() for value in argv)
                or argv[1:-2] not in (["-I"], ["-I", "-B"])
                or argv[-2] != "-c"):
            raise ValueError
        source = argv[-1]
        entry = ast.parse(source).body[-1]
        call = entry.value if isinstance(entry, ast.Expr) else None
        if (not isinstance(call, ast.Call) or ast.unparse(call.func) != "runpy.run_module"
                or len(call.args) != 1 or ast.literal_eval(call.args[0]) != "runpy"
                or {item.arg: ast.literal_eval(item.value) for item in call.keywords}
                != {"run_name": "__main__", "alter_sys": True}):
            raise ValueError
        segment = ast.get_source_segment(source, entry)
        if not segment or not source.rstrip().endswith(segment):
            raise ValueError
        # Keep the host's interpreter, home, bootstrap and dependency lease intact.
        if "-B" not in argv[1:-2]:
            argv.insert(2, "-B")  # -I ignores PYTHONDONTWRITEBYTECODE; protect bootstrap imports too.
        argv[-1] = source.rstrip()[:-len(segment)] + f"runpy.run_path({str(_BRIDGE)!r}, run_name='__main__')"
        return argv
    except (ValueError, TypeError, SyntaxError, IndexError, AttributeError, RecursionError):
        raise RuntimeError(RUNTIME_ERROR) from None


def _source_runtime(root: Path) -> list[str] | None:
    if not all((root / item).is_file() for item in (
            "hermes_bootstrap.py", "hermes_cli/runtime_provider.py", "agent/auxiliary_client.py")):
        return None
    for directory in (".venv", "venv"):
        python = root / directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if python.is_file() and os.access(python, os.X_OK):
            return [str(python), "-I", "-B", "-c",
                    "import sys, runpy; " + f"sys.path.insert(0, {str(root.resolve())!r}); "
                    "import hermes_bootstrap; "
                    + f"runpy.run_path({str(_BRIDGE)!r}, run_name='__main__')"]
    return None


def _console_runtime(binary: str, environment: Mapping[str, str]) -> list[str] | None:
    # Python console scripts retain their own interpreter even in a relocated
    # service PATH. Do not parse or execute shell wrapper contents as Python.
    with Path(binary).open("rb") as handle:
        header = handle.read(8192).decode("utf-8", errors="replace")
    if not header.startswith("#!") or "hermes_cli.main" not in header:
        return None
    lines = header.splitlines()
    parts = shlex.split(lines[0][2:])
    if len(lines) > 1 and lines[1].startswith("'''exec' "):
        # distlib uses this shell/Python polyglot when the interpreter path has
        # spaces. Parse its literal argv; never evaluate the shell source.
        wrapped = shlex.split(lines[1][len("'''exec' "):])
        python = wrapped[0] if len(wrapped) == 3 and wrapped[1:] == ["$0", "$@"] else None
    elif len(parts) == 2 and Path(parts[0]).name == "env":
        python = shutil.which(parts[1], path=environment.get("PATH", os.defpath))
    else:
        python = parts[0] if len(parts) == 1 else None
    if not python or not Path(python).is_absolute() or "python" not in Path(python).name.lower():
        return None
    return [python, "-I", "-B", "-c", "import runpy; import hermes_bootstrap; "
            + f"runpy.run_path({str(_BRIDGE)!r}, run_name='__main__')"]


def _discover(environment: Mapping[str, str]) -> list[str] | None:
    explicit_root = str(environment.get("EIMEMORY_HERMES_AGENT_ROOT") or "").strip()
    explicit_binary = str(environment.get("EIMEMORY_HERMES_BIN") or "").strip()
    if explicit_root and not explicit_binary:
        root = Path(explicit_root).expanduser()
        binary = shutil.which("hermes", path=str(root / ".hermes/bin"))
        argv = _launcher_command(binary, environment) if binary else None
        if argv is None:
            argv = _source_runtime(root)
        if argv is None:
            raise RuntimeError(RUNTIME_ERROR)
        return argv
    home = Path(environment.get("HOME") or environment.get("USERPROFILE") or Path.home())
    hermes_home = Path(environment.get("HERMES_HOME") or home / ".hermes").expanduser()
    path = environment.get("PATH", os.defpath)
    binary = shutil.which(str(Path(explicit_binary).expanduser()) if explicit_binary else "hermes", path=path)
    if explicit_binary and not binary:
        raise RuntimeError(RUNTIME_ERROR)
    if not binary:
        for directory in (home / ".local/bin", hermes_home / "hermes-agent/.hermes/bin"):
            candidate = shutil.which("hermes", path=str(directory))
            if candidate:
                binary = candidate
                break
    if binary:
        argv = _console_runtime(binary, environment)
        if argv is not None:
            return argv
        argv = _launcher_command(binary, environment)
        if argv is not None:
            return argv
        # A legacy source launcher may be a symlink into its checkout/venv.
        for parent in list(Path(binary).resolve().parents)[:4]:
            argv = _source_runtime(parent)
            if argv is not None:
                return argv
        if explicit_binary:
            raise RuntimeError(RUNTIME_ERROR)
    for root in (hermes_home / "hermes-agent", hermes_home):
        argv = _source_runtime(root)
        if argv is not None:
            return argv
    if binary:
        raise RuntimeError(RUNTIME_ERROR)
    return None


class HermesReviewClient(CommandLLMClient):
    def check_configuration(self) -> None:
        try:
            code, out, _ = run_bounded_command(
                list(self.argv), b'{"configuration_check":true}', timeout_seconds=10,
                environment=self._environment)
            payload = json.loads(out)
            if code == 0 and payload == {"configuration_ok": True, "error": ""}:
                return
            error = CONFIG_ERROR if payload == {"configuration_ok": False, "error": CONFIG_ERROR} else RUNTIME_ERROR
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
            error = RUNTIME_ERROR
        raise RuntimeError(error)


def research_review_client(*, environment: Mapping[str, str] | None = None) -> CommandLLMClient | None:
    client = llm_client_from_env("research_review", environment=environment)
    if client is not None:
        return client  # A selected command's error never triggers discovery/fallback.
    settings = dict(os.environ if environment is None else environment)
    # Alias only for the isolated child; neither global environment nor config is rewritten.
    if settings.get("EIMEMORY_HERMES_HOME"):
        settings["HERMES_HOME"] = settings["EIMEMORY_HERMES_HOME"]
    try:
        argv = _discover(settings)
    except Exception:
        raise RuntimeError(RUNTIME_ERROR) from None
    if argv is None:
        return None
    raw_timeout = settings.get("EIMEMORY_RESEARCH_REVIEW_LLM_TIMEOUT_SECONDS") or settings.get("EIMEMORY_LLM_TIMEOUT_SECONDS") or "90"
    try:
        timeout = int(raw_timeout)
    except ValueError:
        timeout = 90
    return HermesReviewClient(argv, timeout_seconds=timeout, environment=settings)


def research_review_configuration(environment: Mapping[str, str]) -> dict:
    error = ""
    try:
        client = research_review_client(environment=environment)
        if client is None:
            error = "research_review_llm_unconfigured"
        elif isinstance(client, HermesReviewClient):
            client.check_configuration()
    except ValueError:
        error = "research_review_llm_configuration_invalid"
    except RuntimeError as exc:
        error = str(exc) if str(exc) in (RUNTIME_ERROR, CONFIG_ERROR) else RUNTIME_ERROR
    return {"configuration_ok": not error, "error": error, "provider_verified": False}
