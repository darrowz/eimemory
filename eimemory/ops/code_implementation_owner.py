"""Release-owned lifecycle owner for the production code-implementation provider.

The owner has one deliberately narrow write surface: it idempotently registers
the immutable v2 revision/binding and publishes a one-hour, live-health-backed
adapter advertisement.  Capability incubation remains owned by the existing
nightly job, and code-evolution effects remain owned by the policy/transaction
subsystem.  This module never creates an automation policy or removes a kill
switch.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
from hashlib import sha256
import os
from pathlib import Path
import stat
import subprocess
from typing import Any, Iterator

from eimemory.adapters.hermes import code_implementation as provider_module
from eimemory.api.runtime import Runtime
from eimemory.capabilities import code_implementation_bootstrap as bootstrap_module
from eimemory.core.clock import now_iso


CODE_IMPLEMENTATION_OWNER_SCHEMA = "code.implementation.owner.v1"
DEFAULT_EIMEMORY_ROOT = Path("/var/lib/eimemory")
EIMEMORY_ROOT_ENV = "EIMEMORY_ROOT"
ADVERTISEMENT_TTL_SECONDS = 3600
CODE_IMPLEMENTATION_REFRESH_SERVICE = "eimemory-code-implementation-refresh.service"
CODE_IMPLEMENTATION_REFRESH_TIMER = "eimemory-code-implementation-refresh.timer"
DEFAULT_KILL_SWITCH_PATH = Path("/etc/eimemory/code-evolution.disabled")
DEFAULT_AUTOMATION_POLICY_PATH = Path("/etc/eimemory/code-automation-policy.v2.json")
PRODUCTION_RUNTIME_SCOPE = {
    "tenant_id": "default",
    "agent_id": "hongtu",
    "workspace_id": "embodied",
    "user_id": "darrow",
}
_LOCK_RELATIVE_PATH = Path("state/code-implementation-refresh.lock")
_HEX64 = frozenset("0123456789abcdef")


class CodeImplementationOwnerError(RuntimeError):
    """The release-owned refresh cannot safely operate."""


class CodeImplementationOwnerBusy(CodeImplementationOwnerError):
    """Another refresh process owns the exclusive authority lock."""


def authority_root(environ: Mapping[str, str] | None = None) -> Path:
    """Resolve the sole provider authority, with the production path as default."""

    source = os.environ if environ is None else environ
    raw = str(source.get(EIMEMORY_ROOT_ENV) or DEFAULT_EIMEMORY_ROOT).strip()
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise CodeImplementationOwnerError("authority_root_must_be_absolute")
    return candidate.resolve(strict=False)


def refresh_code_implementation_owner(
    *,
    now: str = "",
    environ: Mapping[str, str] | None = None,
    runtime_factory: Callable[..., Any] = Runtime.create,
) -> dict[str, Any]:
    """Register v2 and publish one live advertisement under ``EIMEMORY_ROOT``.

    Failures are returned as bounded, credential-free reports.  CLI plumbing
    converts ``ok=false`` into a non-zero process exit status for systemd.
    """

    try:
        root = authority_root(environ)
        advertised_at, expires_at = _advertisement_window(now)
    except (CodeImplementationOwnerError, TypeError, ValueError) as exc:
        return _blocked_report("authority_invalid", detail=str(exc))

    try:
        with _owner_lock(root):
            runtime = runtime_factory(root=root)
            try:
                runtime_root = _runtime_root(runtime)
                if runtime_root != root:
                    return _blocked_report(
                        "authority_runtime_root_mismatch",
                        authority_root=str(root),
                        runtime_root=str(runtime_root),
                    )
                registration = bootstrap_module.register_code_implementation_v2(
                    runtime,
                    runtime_scope=PRODUCTION_RUNTIME_SCOPE,
                )
                if registration.get("ok") is not True:
                    return _blocked_report(
                        str(registration.get("reason") or "registration_failed"),
                        authority_root=str(root),
                        registration=dict(registration),
                    )
                advertisement = bootstrap_module.advertise_code_implementation_v2(
                    runtime,
                    runtime_scope=PRODUCTION_RUNTIME_SCOPE,
                    advertised_at=advertised_at,
                    expires_at=expires_at,
                )
                if advertisement.get("ok") is not True:
                    return _blocked_report(
                        str(advertisement.get("reason") or "advertisement_failed"),
                        authority_root=str(root),
                        registration=dict(registration),
                        advertisement=dict(advertisement),
                    )
                return {
                    "schema": CODE_IMPLEMENTATION_OWNER_SCHEMA,
                    "ok": True,
                    "status": "refreshed",
                    "authority_root": str(root),
                    "runtime_scope": dict(PRODUCTION_RUNTIME_SCOPE),
                    "capability_scope": "global",
                    "advertised_at": advertised_at,
                    "expires_at": expires_at,
                    "ttl_seconds": ADVERTISEMENT_TTL_SECONDS,
                    "registration": dict(registration),
                    "advertisement": dict(advertisement),
                    "manual_bootstrap": True,
                    "qualifying": False,
                }
            finally:
                _close_runtime(runtime)
    except CodeImplementationOwnerBusy:
        return _blocked_report("owner_lock_busy", authority_root=str(root))
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return _blocked_report(
            f"owner_refresh_failed:{type(exc).__name__}",
            authority_root=str(root),
        )


def inspect_code_implementation_owner(
    runtime: Any | None = None,
    *,
    checked_at: str = "",
    environ: Mapping[str, str] | None = None,
    runner: Callable[[list[str]], str] | None = None,
    kill_switch_path: str | Path = DEFAULT_KILL_SWITCH_PATH,
    automation_policy_path: str | Path = DEFAULT_AUTOMATION_POLICY_PATH,
    probe_provider: bool = True,
    runtime_factory: Callable[..., Any] = Runtime.create,
) -> dict[str, Any]:
    """Return a bounded operational view without reading policy contents."""

    try:
        checked = _normalized_timestamp(checked_at or now_iso())
    except ValueError as exc:
        return _blocked_report("inspection_timestamp_invalid", detail=str(exc))
    kill_path = Path(kill_switch_path)
    policy_path = Path(automation_policy_path)
    safety = {
        "kill_switch_path": str(kill_path),
        "kill_switch_present": _path_present(kill_path),
        "automation_policy_path": str(policy_path),
        "automation_policy_present": _path_present(policy_path),
    }
    safety["effects_fail_closed"] = bool(
        safety["kill_switch_present"] or not safety["automation_policy_present"]
    )
    timer_owner = _timer_owner_status(runner=runner)
    try:
        root = authority_root(environ)
    except CodeImplementationOwnerError as exc:
        return {
            "schema": CODE_IMPLEMENTATION_OWNER_SCHEMA,
            "ok": False,
            "status": "blocked",
            "reason": "authority_invalid",
            "detail": str(exc),
            "checked_at": checked,
            "safety": safety,
            "timer_owner": timer_owner,
        }

    owned_runtime = runtime is None
    if owned_runtime:
        if not root.is_dir():
            return _inspection_unavailable(
                root=root,
                runtime_root=None,
                checked_at=checked,
                reason="authority_root_unavailable",
                safety=safety,
                timer_owner=timer_owner,
            )
        try:
            runtime = runtime_factory(root=root)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return _inspection_unavailable(
                root=root,
                runtime_root=None,
                checked_at=checked,
                reason=f"authority_open_failed:{type(exc).__name__}",
                safety=safety,
                timer_owner=timer_owner,
            )
    try:
        runtime_root = _runtime_root(runtime)
        if runtime_root != root:
            return _inspection_unavailable(
                root=root,
                runtime_root=runtime_root,
                checked_at=checked,
                reason="authority_runtime_root_mismatch",
                safety=safety,
                timer_owner=timer_owner,
            )
        binding = _binding_status(runtime, checked_at=checked)
        advertisement = _advertisement_status(runtime, checked_at=checked)
        catalog = _catalog_status(runtime)
        provider_health = _provider_health(checked, probe=probe_provider)
        refresh_ready = bool(
            binding.get("exact_v2") is True
            and advertisement.get("fresh") is True
            and provider_health.get("ok") is True
        )
        timer_ready = bool(
            timer_owner["timer"].get("load_state") == "loaded"
            and timer_owner["timer"].get("active_state") in {"active", "activating"}
            and timer_owner["timer"].get("unit_file_state") == "enabled"
            and timer_owner["service"].get("result") != "failed"
        )
        provider_reader_ready = bool(refresh_ready and catalog.get("ready") is True)
        return {
            "schema": CODE_IMPLEMENTATION_OWNER_SCHEMA,
            "ok": bool(provider_reader_ready and timer_ready),
            "status": "ready" if provider_reader_ready and timer_ready else "waiting",
            "checked_at": checked,
            "authority": {
                "root": str(root),
                "source": (
                    EIMEMORY_ROOT_ENV
                    if (os.environ if environ is None else environ).get(EIMEMORY_ROOT_ENV)
                    else "production_default"
                ),
                "runtime_root": str(runtime_root),
                "matches_runtime": True,
            },
            "runtime_scope": dict(PRODUCTION_RUNTIME_SCOPE),
            "binding": binding,
            "provider_health": provider_health,
            "advertisement": advertisement,
            "catalog": catalog,
            "timer_owner": timer_owner,
            "safety": safety,
            "refresh_ready": refresh_ready,
            "provider_reader_ready": provider_reader_ready,
            "manual_bootstrap": True,
            "qualifying": False,
        }
    finally:
        if owned_runtime:
            _close_runtime(runtime)


def _binding_status(runtime: Any, *, checked_at: str) -> dict[str, Any]:
    try:
        context = runtime.capabilities.incubation_context(
            provider_module.CAPABILITY_ID,
            runtime_scope=PRODUCTION_RUNTIME_SCOPE,
            capability_scope="global",
            at_time=checked_at,
            limit=100,
        )
    except (RuntimeError, TypeError, ValueError) as exc:
        return {
            "exact_v2": False,
            "reason": f"binding_query_failed:{type(exc).__name__}",
        }
    revisions = [
        row
        for row in context.get("revisions") or ()
        if row.get("entity_id") == provider_module.REVISION_ID
    ]
    bindings = [
        row
        for row in context.get("bindings") or ()
        if row.get("entity_id") == provider_module.BINDING_ID
    ]
    if len(revisions) != 1 or len(bindings) != 1:
        return {
            "exact_v2": False,
            "reason": "binding_unavailable",
        }
    descriptor = dict(bindings[0].get("descriptor") or {})
    exact = bool(
        descriptor.get("binding_id") == provider_module.BINDING_ID
        and descriptor.get("capability_revision_id") == provider_module.REVISION_ID
        and descriptor.get("provider_kind") == provider_module.PROVIDER_KIND
        and descriptor.get("provider_instance_id") == provider_module.PROVIDER_INSTANCE_ID
        and descriptor.get("implementation_digest") == provider_module.IMPLEMENTATION_DIGEST
        and provider_module.OPERATION in tuple(descriptor.get("operations") or ())
    )
    return {
        "exact_v2": exact,
        "capability_id": provider_module.CAPABILITY_ID,
        "revision_id": provider_module.REVISION_ID,
        "binding_id": provider_module.BINDING_ID,
        "provider_kind": provider_module.PROVIDER_KIND,
        "provider_instance_id": provider_module.PROVIDER_INSTANCE_ID,
        "implementation_digest": str(descriptor.get("implementation_digest") or ""),
        "reason": "" if exact else "binding_identity_mismatch",
    }


def _advertisement_status(runtime: Any, *, checked_at: str) -> dict[str, Any]:
    try:
        advertisements = runtime.capabilities.list_adapter_advertisements(
            runtime_scope=PRODUCTION_RUNTIME_SCOPE,
            capability_scope="global",
            binding_id=provider_module.BINDING_ID,
            provider_kind=provider_module.PROVIDER_KIND,
            provider_instance_id=provider_module.PROVIDER_INSTANCE_ID,
            at_time=checked_at,
            fresh_at=checked_at,
            limit=32,
        )
    except (RuntimeError, TypeError, ValueError) as exc:
        return {
            "fresh": False,
            "reason": f"advertisement_query_failed:{type(exc).__name__}",
        }
    selected = provider_module.select_latest_code_implementation_advertisement(
        advertisements,
        implementation_digest_value=provider_module.IMPLEMENTATION_DIGEST,
    )
    if selected is None:
        return {"fresh": False, "reason": "fresh_advertisement_unavailable"}
    descriptor = dict(selected.get("descriptor") or {})
    freshness = dict(selected.get("freshness") or {})
    provenance = descriptor.get("provenance") if isinstance(descriptor.get("provenance"), Mapping) else {}
    return {
        "fresh": True,
        "advertisement_id": str(selected.get("entity_id") or ""),
        "advertisement_digest": str(selected.get("entity_digest") or ""),
        "advertised_at": str(freshness.get("advertised_at") or descriptor.get("advertised_at") or ""),
        "expires_at": str(freshness.get("expires_at") or descriptor.get("expires_at") or ""),
        "adapter_id": str(descriptor.get("adapter_id") or ""),
        "manual_bootstrap": provenance.get("manual_bootstrap") is True,
        "qualifying": provenance.get("qualifying") is True,
        "reason": "",
    }


def _catalog_status(runtime: Any) -> dict[str, Any]:
    from eimemory.evaluation.hongtu_code_implementation import (
        CATALOG_CASE_ID,
        CATALOG_EXECUTOR_ID,
    )

    catalog = getattr(runtime, "capability_catalog", None)
    case_present = False
    executor_present = False
    sealed = bool(getattr(catalog, "sealed", False))
    if catalog is not None:
        try:
            case_present = catalog.get_case(CATALOG_CASE_ID) is not None
            executor_present = catalog.describe_executor(CATALOG_EXECUTOR_ID) is not None
        except (RuntimeError, TypeError, ValueError):
            case_present = False
            executor_present = False
    structural_ready = bool(sealed and case_present and executor_present)
    snapshot = provider_module.code_implementation_catalog_activation_snapshot(
        runtime.capabilities,
        runtime_scope=PRODUCTION_RUNTIME_SCOPE,
        capability_scope="global",
    )
    if snapshot is None:
        return {
            "ready": False,
            "status": "waiting",
            "reason": (
                "catalog_lifecycle_receipts_pending"
                if structural_ready
                else "sealed_catalog_unavailable"
            ),
            "required_passes": 2,
            "valid_passes": 0,
            "case_id": CATALOG_CASE_ID,
            "sealed": sealed,
            "case_present": case_present,
            "executor_present": executor_present,
            "structural_ready": structural_ready,
            "bootstrap_error": str(getattr(runtime, "catalog_bootstrap_error", "") or ""),
        }
    ready = bool(structural_ready and int(snapshot.get("catalog_passes") or 0) >= 2)
    return {
        "ready": ready,
        "status": "ready" if ready else "waiting",
        "reason": "" if ready else "sealed_catalog_unavailable",
        "required_passes": 2,
        "valid_passes": int(snapshot.get("catalog_passes") or 0),
        "case_id": str(snapshot.get("catalog_case_id") or ""),
        "snapshot_digest": str(snapshot.get("catalog_snapshot_digest") or ""),
        "activation_state_digest": str(snapshot.get("activation_state_digest") or ""),
        "sealed": sealed,
        "case_present": case_present,
        "executor_present": executor_present,
        "structural_ready": structural_ready,
        "bootstrap_error": str(getattr(runtime, "catalog_bootstrap_error", "") or ""),
    }


def _provider_health(checked_at: str, *, probe: bool) -> dict[str, Any]:
    if not probe:
        # Skipping the socket call is useful for offline inspection, but it is
        # not live health evidence and must never make the owner report ready.
        return {
            "ok": False,
            "status": "not_probed",
            "reason": "provider_health_not_probed",
        }
    nonce = sha256(f"owner-status:{checked_at}".encode("utf-8")).hexdigest()[:32]
    try:
        health = bootstrap_module.CodeImplementationSocketClient().health(nonce=nonce)
    except provider_module.CodeImplementationError:
        return {"ok": False, "status": "unavailable", "reason": "provider_health_unavailable"}
    if health.get("ok") is not True:
        return {"ok": False, "status": "failed", "reason": "provider_health_failed"}
    return {
        "ok": True,
        "status": "live",
        "provider_instance_id": str(health.get("provider_instance_id") or ""),
        "implementation_digest": str(health.get("implementation_digest") or ""),
    }


def _timer_owner_status(*, runner: Callable[[list[str]], str] | None) -> dict[str, Any]:
    call = runner or _run_systemctl
    return {
        "timer": _unit_status(CODE_IMPLEMENTATION_REFRESH_TIMER, call=call),
        "service": _unit_status(CODE_IMPLEMENTATION_REFRESH_SERVICE, call=call),
        "retired_units": [
            "eimemory-code-implementation-bringup.service",
            "eimemory-code-implementation-advertise.service",
            "eimemory-code-implementation-advertise.timer",
        ],
    }


def _unit_status(unit: str, *, call: Callable[[list[str]], str]) -> dict[str, str]:
    args = [
        "systemctl",
        "--user",
        "show",
        unit,
        "--property=LoadState,ActiveState,SubState,UnitFileState,LastTriggerUSec,NextElapseUSecRealtime,Result",
        "--no-page",
    ]
    try:
        raw = call(args)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        return {
            "unit": unit,
            "load_state": "unknown",
            "active_state": "unknown",
            "error": type(exc).__name__,
        }
    values: dict[str, str] = {}
    for line in str(raw or "").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    return {
        "unit": unit,
        "load_state": values.get("LoadState", ""),
        "active_state": values.get("ActiveState", ""),
        "sub_state": values.get("SubState", ""),
        "unit_file_state": values.get("UnitFileState", ""),
        "last_trigger_at": values.get("LastTriggerUSec", ""),
        "next_elapse_at": values.get("NextElapseUSecRealtime", ""),
        "result": values.get("Result", ""),
    }


def _run_systemctl(args: list[str]) -> str:
    return subprocess.run(args, check=True, text=True, capture_output=True).stdout


def _inspection_unavailable(
    *,
    root: Path,
    runtime_root: Path | None,
    checked_at: str,
    reason: str,
    safety: Mapping[str, Any],
    timer_owner: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema": CODE_IMPLEMENTATION_OWNER_SCHEMA,
        "ok": False,
        "status": "blocked",
        "reason": reason,
        "checked_at": checked_at,
        "authority": {
            "root": str(root),
            "runtime_root": str(runtime_root or ""),
            "matches_runtime": False,
        },
        "timer_owner": dict(timer_owner),
        "safety": dict(safety),
        "refresh_ready": False,
        "provider_reader_ready": False,
        "manual_bootstrap": True,
        "qualifying": False,
    }


def _blocked_report(reason: str, **details: Any) -> dict[str, Any]:
    return {
        "schema": CODE_IMPLEMENTATION_OWNER_SCHEMA,
        "ok": False,
        "status": "blocked",
        "reason": str(reason),
        "manual_bootstrap": True,
        "qualifying": False,
        **details,
    }


def _advertisement_window(value: str) -> tuple[str, str]:
    parsed = datetime.fromisoformat(_normalized_timestamp(value or now_iso()).replace("Z", "+00:00"))
    advertised = parsed.astimezone(timezone.utc).replace(microsecond=0)
    expires = advertised + timedelta(seconds=ADVERTISEMENT_TTL_SECONDS)
    return _format_timestamp(advertised), _format_timestamp(expires)


def _normalized_timestamp(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("timestamp_required")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp_invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("timestamp_timezone_required")
    return _format_timestamp(parsed.astimezone(timezone.utc))


def _format_timestamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _runtime_root(runtime: Any) -> Path:
    raw = getattr(getattr(runtime, "store", None), "root", None)
    if raw is None:
        raise CodeImplementationOwnerError("runtime_root_unavailable")
    return Path(raw).resolve(strict=False)


def _close_runtime(runtime: Any) -> None:
    close = getattr(runtime, "close", None)
    if callable(close):
        close()


def _path_present(path: Path) -> bool:
    try:
        path.lstat()
    except OSError:
        return False
    return True


@contextmanager
def _owner_lock(root: Path) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True, mode=0o750)
    state_root = root / _LOCK_RELATIVE_PATH.parent
    state_root.mkdir(parents=True, exist_ok=True, mode=0o750)
    metadata = state_root.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or state_root.is_symlink():
        raise CodeImplementationOwnerError("owner_lock_directory_invalid")
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(state_root / _LOCK_RELATIVE_PATH.name, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CodeImplementationOwnerBusy("owner_lock_busy") from exc
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


__all__ = [
    "ADVERTISEMENT_TTL_SECONDS",
    "CODE_IMPLEMENTATION_OWNER_SCHEMA",
    "CODE_IMPLEMENTATION_REFRESH_SERVICE",
    "CODE_IMPLEMENTATION_REFRESH_TIMER",
    "DEFAULT_AUTOMATION_POLICY_PATH",
    "DEFAULT_EIMEMORY_ROOT",
    "DEFAULT_KILL_SWITCH_PATH",
    "PRODUCTION_RUNTIME_SCOPE",
    "authority_root",
    "inspect_code_implementation_owner",
    "refresh_code_implementation_owner",
]
