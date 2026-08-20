#!/usr/bin/env python3
"""Verify local Storage v2/L5 v3 migration state without changing it.

This verifier is deliberately local and read-only.  It does not infer a green
L5 state from service health, a package version, or a host fingerprint.  It
checks schema integrity, durable backfill state, export recovery, and the four
independent v3 assessment axes for one exact runtime scope.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from eimemory.api.runtime import Runtime
from eimemory.capabilities.registry import exact_runtime_scope
from eimemory.models.records import ScopeRef
from eimemory.storage.migrations.capability_v3 import (
    capability_v3_foreign_key_check,
    is_capability_v3_schema_ready,
)
from eimemory.storage.migrations.backfill_capability_v3 import (
    capability_v3_backfill_status,
    inspect_capability_v3_dual_write,
)
from eimemory.storage.runtime_store import RuntimeStore


VERIFY_SCHEMA = "deploy.verify_l5_v3_migration.v1"


def verify_l5_v3_migration(
    runtime: Runtime,
    *,
    profile_key: str,
    runtime_scope: ScopeRef | dict[str, Any],
    capability_scope: str = "global",
    require_backfill_complete: bool = False,
    require_ready_assessment: bool = False,
    require_dual_write_agreement: bool = False,
    dual_write_limit: int = 200,
    dual_write_cursor: str = "",
) -> dict[str, Any]:
    """Build a bounded migration/readiness report from a live local runtime.

    The report exposes the exact scoped cursor and one bounded central
    dual-write parity page.  It does not infer readiness from a service, host,
    package version, or partial shadow result.
    """

    try:
        scope = exact_runtime_scope(runtime_scope)
        conn = runtime.store.sqlite.conn
        schema_ready = is_capability_v3_schema_ready(conn)
        foreign_key_errors = capability_v3_foreign_key_check(conn) if schema_ready else []
        backfill_status = capability_v3_backfill_status(
            runtime,
            runtime_scope=scope,
            capability_scope=capability_scope,
        )
        backfill = dict(backfill_status.get("state") or {})
        dual_write = inspect_capability_v3_dual_write(
            runtime,
            runtime_scope=scope,
            capability_scope=capability_scope,
            limit=dual_write_limit,
            cursor=dual_write_cursor,
        )
        assessment = runtime.build_l5_assessment_v3(
            profile_key=profile_key,
            scope=scope,
            capability_scope=capability_scope,
            persist=False,
        )
        export_status = runtime.store.capability_export_status()
        phase_stats = (
            backfill_status.get("phase_stats")
            if isinstance(backfill_status.get("phase_stats"), dict)
            else {}
        )
        legacy_observation_phase = phase_stats.get("legacy_explicit_observation")
        explicit_observation_backfill_complete = bool(
            isinstance(legacy_observation_phase, dict)
            and legacy_observation_phase.get("status") == "completed"
        )
        full_backfill_complete = backfill_status.get("full_migration_complete") is True
        checks = {
            "schema_ready": schema_ready,
            "foreign_keys_clean": not foreign_key_errors,
            "capability_audit_export_ready": export_status.get("ok") is True,
            "backfill_status_available": backfill_status.get("status") not in {"failed", "blocked"},
            "dual_write_report_available": dual_write.get("status") not in {"failed", "blocked"},
            # A completed legacy-observation phase must not be mislabeled as
            # a complete historical entity-graph migration.
            "backfill_complete": full_backfill_complete if require_backfill_complete else True,
            "assessment_ready": assessment.get("ok") is True if require_ready_assessment else True,
            "dual_write_agreement": dual_write.get("ok") is True if require_dual_write_agreement else True,
        }
        failed = [name for name, passed in checks.items() if not passed]
        return {
            "schema": VERIFY_SCHEMA,
            "ok": not failed,
            "status": "ready" if not failed else "blocked",
            "reason": "" if not failed else "migration_verification_checks_failed",
            "checks": checks,
            "failed_checks": failed,
            "runtime_scope": {
                "tenant_id": scope.tenant_id,
                "agent_id": scope.agent_id,
                "workspace_id": scope.workspace_id,
                "user_id": scope.user_id,
            },
            "capability_scope": capability_scope,
            "profile_key": profile_key,
            "backfill": backfill,
            "backfill_status": backfill_status,
            "backfill_phase_stats": phase_stats,
            "explicit_observation_backfill_complete": explicit_observation_backfill_complete,
            "full_backfill_complete": full_backfill_complete,
            "foreign_key_errors": foreign_key_errors,
            "capability_audit_export": export_status,
            "dual_write": dual_write,
            "assessment": assessment,
            "axes": {
                "loop_maturity": assessment.get("loop_maturity"),
                "capability_readiness": assessment.get("capability_readiness"),
                "adapter_readiness": assessment.get("adapter_readiness"),
                "deployment_assurance": assessment.get("deployment_assurance"),
            },
        }
    except Exception as exc:
        return {
            "schema": VERIFY_SCHEMA,
            "ok": False,
            "status": "failed",
            "reason": "migration_verification_failed",
            "error": type(exc).__name__,
            "detail": str(exc)[:1_000],
        }


def _scope_from_args(args: argparse.Namespace) -> ScopeRef:
    return ScopeRef(
        tenant_id=str(args.tenant_id),
        agent_id=str(args.agent_id),
        workspace_id=str(args.workspace_id),
        user_id=str(args.user_id),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify eimemory L5 v3 migration and axes")
    parser.add_argument("--root", required=True, help="runtime storage root; never defaults to a production path")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--capability-scope", default="global")
    parser.add_argument("--tenant-id", default="default")
    parser.add_argument("--agent-id", default="")
    parser.add_argument("--workspace-id", default="")
    parser.add_argument("--user-id", default="")
    parser.add_argument("--require-backfill-complete", action="store_true")
    parser.add_argument("--require-ready-assessment", action="store_true")
    parser.add_argument("--require-dual-write-agreement", action="store_true")
    parser.add_argument("--dual-write-limit", type=int, default=200)
    parser.add_argument("--dual-write-cursor", default="")
    args = parser.parse_args(argv)
    try:
        root = Path(args.root).expanduser().resolve(strict=True)
        # Bypass Runtime.create so optional external candidate sources cannot
        # affect a migration-verification query.
        runtime = Runtime(RuntimeStore(root))
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "schema": VERIFY_SCHEMA,
                    "ok": False,
                    "status": "blocked",
                    "reason": "migration_verification_runtime_unavailable",
                    "error": type(exc).__name__,
                    "detail": str(exc)[:1_000],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    try:
        report = verify_l5_v3_migration(
            runtime,
            profile_key=str(args.profile),
            runtime_scope=_scope_from_args(args),
            capability_scope=str(args.capability_scope),
            require_backfill_complete=bool(args.require_backfill_complete),
            require_ready_assessment=bool(args.require_ready_assessment),
            require_dual_write_agreement=bool(args.require_dual_write_agreement),
            dual_write_limit=int(args.dual_write_limit),
            dual_write_cursor=str(args.dual_write_cursor),
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        report = {
            "schema": VERIFY_SCHEMA,
            "ok": False,
            "status": "failed",
            "reason": "migration_verification_invocation_failed",
            "error": type(exc).__name__,
            "detail": str(exc)[:1_000],
        }
    finally:
        runtime.close()
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, default=str))
    return 0 if report.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
