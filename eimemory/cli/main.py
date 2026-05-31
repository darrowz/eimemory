from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from eimemory.adapters.eibrain.rpc_server import EIBrainRPCServer
from eimemory.adapters.openclaw.hooks import OpenClawMemoryHooks
from eimemory.adapters.openclaw.qmd_compat import main as qmd_main
from eimemory.api.runtime import Runtime
from eimemory.compatibility.migration_helpers import (
    build_review_report,
    backup_create,
    backup_verify,
    export_records,
    import_candidates,
    import_records,
    scan_migration_source,
)
from eimemory.config.loader import load_settings
from eimemory.identity import canonical_hongtu_user_id, hongtu_scope
from eimemory.identity_ops import identity_report, repair_hongtu_identity
from eimemory.knowledge.compiler import compile_paper_knowledge
from eimemory.governance.console import write_evolution_console
from eimemory.governance.snapshot import build_governance_snapshot
from eimemory.ei_bridge.openclaw_runtime import handle_openclaw_feishu_event
from eimemory.scheduler.jobs import run_nightly_jobs


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="eimemory")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init")

    ingest = sub.add_parser("ingest")
    ingest.add_argument("text")
    ingest.add_argument("--title", default="CLI ingest")
    ingest.add_argument("--memory-type", default="fact")

    recall = sub.add_parser("recall")
    recall.add_argument("query")
    recall.add_argument("--view", choices=["claim_centered", "page_centered", "mixed", "contradiction", "freshness"], default="")

    paper = sub.add_parser("paper")
    paper_sub = paper.add_subparsers(dest="paper_command")

    paper_ingest = paper_sub.add_parser("ingest")
    paper_ingest.add_argument("--arxiv-id", default="")
    paper_ingest.add_argument("--doi", default="")
    paper_ingest.add_argument("--url", default="")
    paper_ingest.add_argument("--pdf-file", default="")
    paper_ingest.add_argument("--title", default="")
    paper_ingest.add_argument("--abstract", default="")

    paper_extract = paper_sub.add_parser("extract")
    paper_extract.add_argument("--paper-source-id", required=True)
    paper_extract.add_argument("--title", default="")
    paper_extract.add_argument("--abstract", default="")
    paper_extract.add_argument("--body", default="")

    paper_compile = paper_sub.add_parser("compile")
    paper_compile.add_argument("--paper-source-id", required=True)
    paper_compile.add_argument("--title", default="")

    source = sub.add_parser("source")
    source_sub = source.add_subparsers(dest="source_command")

    source_add = source_sub.add_parser("add")
    source_add.add_argument("--source-kind", required=True, choices=["paper", "news", "rss", "url", "manual"])
    source_add.add_argument("--title", default="")
    source_add.add_argument("--uri", default="")
    source_add.add_argument("--tag", action="append", default=[])
    source_add.add_argument("--enabled", action="store_true", default=True)
    source_add.add_argument("--disabled", action="store_false", dest="enabled")

    source_list = source_sub.add_parser("list")
    source_list.add_argument("--enabled-only", action="store_true")
    source_list.add_argument("--source-kind", default="")

    source_scan = source_sub.add_parser("scan")
    source_scan.add_argument("--persist", action="store_true")

    source_discover = source_sub.add_parser("discover")
    source_discover.add_argument("--persist", action="store_true")
    source_discover.add_argument("--gap", action="append", default=[])

    source_expand = source_sub.add_parser("expand")
    source_expand.add_argument("--apply", action="store_true")
    source_expand.add_argument("--max-apply", type=int, default=3)
    source_expand.add_argument("--min-score", type=float, default=0.7)

    intake = sub.add_parser("intake")
    intake_sub = intake.add_subparsers(dest="intake_command")

    intake_run = intake_sub.add_parser("run")
    intake_run.add_argument("--persist", action="store_true")
    intake_run.add_argument("--source-kind", choices=["paper", "news", "rss", "url", "manual"], default="")
    intake_run.add_argument("--limit", type=int, default=None)

    intake_report = intake_sub.add_parser("report")
    intake_report.add_argument("--source-kind", choices=["paper", "news", "rss", "url", "manual"], default="")
    intake_report.add_argument("--limit", type=int, default=None)

    intake_collect = intake_sub.add_parser("collect")
    intake_collect.add_argument("--source-kind", choices=["paper", "news", "rss", "url", "manual"], default="")
    intake_collect.add_argument("--limit", type=int, default=None)
    intake_collect.add_argument("--fetch", action="store_true")
    intake_collect.add_argument("--persist", action="store_true")

    intake_queue = intake_sub.add_parser("queue")
    intake_queue.add_argument("--status", action="append", default=[])
    intake_queue.add_argument("--limit", type=int, default=20)
    intake_queue.add_argument("--explain", action="store_true")

    intake_explain = intake_sub.add_parser("explain")
    intake_explain.add_argument("record_id", nargs="?")
    intake_explain.add_argument("--limit", type=int, default=0)

    intake_review = intake_sub.add_parser("review")
    intake_review.add_argument("record_id")
    intake_review.add_argument("decision", choices=["approve", "reject", "quarantine", "deprecate"])
    intake_review.add_argument("--reviewer", default="cli")
    intake_review.add_argument("--note", default="")

    intake_promote = intake_sub.add_parser("promote")
    intake_promote.add_argument("record_id")
    intake_promote.add_argument("--promoter", default="cli")
    intake_promote.add_argument("--note", default="")

    intake_merge = intake_sub.add_parser("merge")
    intake_merge.add_argument("source_record_id")
    intake_merge.add_argument("target_record_id")
    intake_merge.add_argument("--reviewer", default="cli")
    intake_merge.add_argument("--note", default="")

    intake_paper_promote = intake_sub.add_parser("paper-promote")
    intake_paper_promote.add_argument("record_id")

    intake_policy = intake_sub.add_parser("policy")
    intake_policy.add_argument("--gap", action="append", default=[])

    intake_pack = intake_sub.add_parser("pack")
    intake_pack_sub = intake_pack.add_subparsers(dest="pack_command")
    intake_pack_export = intake_pack_sub.add_parser("export")
    intake_pack_export.add_argument("path")
    intake_pack_export.add_argument("--include-candidates", action="store_true")
    intake_pack_import = intake_pack_sub.add_parser("import")
    intake_pack_import.add_argument("path")
    intake_pack_import.add_argument("--dry-run", action="store_true")

    export_cmd = sub.add_parser("export")
    export_cmd.add_argument("path")

    import_cmd = sub.add_parser("import")
    import_cmd.add_argument("path")

    backup = sub.add_parser("backup")
    backup_sub = backup.add_subparsers(dest="backup_command")

    backup_create_cmd = backup_sub.add_parser("create")
    backup_create_cmd.add_argument("path")

    backup_verify_cmd = backup_sub.add_parser("verify")
    backup_verify_cmd.add_argument("path")

    migrate = sub.add_parser("migrate")
    migrate_sub = migrate.add_subparsers(dest="migrate_command")

    migrate_scan = migrate_sub.add_parser("scan")
    migrate_scan.add_argument("path")

    migrate_import = migrate_sub.add_parser("import")
    migrate_import.add_argument("path")
    migrate_import.add_argument("--candidate-id", action="append", default=[])

    migrate_report = migrate_sub.add_parser("report")
    migrate_report.add_argument("path")
    migrate_report.add_argument("--output", required=True)

    brief = sub.add_parser("brief")
    brief_sub = brief.add_subparsers(dest="brief_command")
    brief_daily = brief_sub.add_parser("daily")
    brief_daily.add_argument("--date", default="")
    brief_daily.add_argument("--persist", action="store_true")
    brief_daily.add_argument("--channel", default="feishu")

    sub.add_parser("nightly")

    quality = sub.add_parser("quality")
    quality_sub = quality.add_subparsers(dest="quality_command")
    quality_sub.add_parser("stats")
    quality_repair = quality_sub.add_parser("repair")
    quality_repair.add_argument("--apply", action="store_true")

    identity = sub.add_parser("identity")
    identity_sub = identity.add_subparsers(dest="identity_command")
    identity_sub.add_parser("report")
    identity_repair = identity_sub.add_parser("repair")
    identity_repair.add_argument("--apply", action="store_true")
    identity_repair.add_argument("--limit", type=int, default=0)

    living = sub.add_parser("living")
    living_sub = living.add_subparsers(dest="living_command")
    living_enrich = living_sub.add_parser("enrich")
    living_enrich.add_argument("--limit", type=int, default=100)
    living_timeline = living_sub.add_parser("timeline")
    living_timeline.add_argument("--limit", type=int, default=100)
    living_posture = living_sub.add_parser("posture")
    living_posture.add_argument("query")
    living_posture.add_argument("--limit", type=int, default=5)

    reflect = sub.add_parser("reflect")
    reflect_sub = reflect.add_subparsers(dest="reflect_command")

    reflect_sub.add_parser("check")

    reflect_log = reflect_sub.add_parser("log")
    reflect_log.add_argument("tag")
    reflect_log.add_argument("miss")
    reflect_log.add_argument("fix")

    reflect_read = reflect_sub.add_parser("read")
    reflect_read.add_argument("count", nargs="?", default="5")

    reflect_sub.add_parser("stats")

    serve_rpc = sub.add_parser("serve-eibrain-rpc")
    serve_rpc.add_argument("--host", default="")
    serve_rpc.add_argument("--port", type=int, default=None)

    openclaw_hook = sub.add_parser("openclaw-hook")
    openclaw_hook.add_argument(
        "hook",
        choices=["message_received", "before_prompt_build", "agent_end", "task_end", "session_end"],
    )

    ei_bridge = sub.add_parser("ei-bridge")
    ei_bridge_sub = ei_bridge.add_subparsers(dest="ei_bridge_command")
    ei_bridge_sub.add_parser("feishu")

    governance = sub.add_parser("governance")
    governance_sub = governance.add_subparsers(dest="governance_command")

    governance_sub.add_parser("snapshot")

    governance_console = governance_sub.add_parser("console")
    governance_console.add_argument("--output", required=True)

    evolve = sub.add_parser("evolve")
    evolve_sub = evolve.add_subparsers(dest="evolve_command")

    evolve_evaluate = evolve_sub.add_parser("evaluate")
    evolve_evaluate.add_argument("dataset_json")
    evolve_evaluate.add_argument("--task-type", default="")
    evolve_evaluate.add_argument("--profile", default="balanced")

    evolve_promotions = evolve_sub.add_parser("promotions")
    evolve_promotions.add_argument("--min-pass-rate", type=float, default=0.8)

    evolve_loop = evolve_sub.add_parser("loop")
    evolve_loop.add_argument("--apply", action="store_true")
    evolve_loop.add_argument("--min-roi", type=float, default=0.0)
    evolve_loop.add_argument("--persist-report", action="store_true")

    evolve_autonomous = evolve_sub.add_parser("autonomous")
    evolve_autonomous.add_argument("--apply", action="store_true")
    evolve_autonomous.add_argument("--max-apply", type=int, default=3)
    evolve_autonomous.add_argument("--persist-report", action="store_true")
    evolve_autonomous.add_argument("--web-evidence-json", default="")
    evolve_autonomous.add_argument("--scope-agent", default="")
    evolve_autonomous.add_argument("--scope-workspace", default="")
    evolve_autonomous.add_argument("--scope-user", default="")

    evolve_web_scout = evolve_sub.add_parser("web-scout")
    evolve_web_scout.add_argument("--url", action="append", default=[])
    evolve_web_scout.add_argument("--evidence-json", default="")
    evolve_web_scout.add_argument("--timeout-seconds", type=int, default=8)
    evolve_web_scout.add_argument("--scope-agent", default="")
    evolve_web_scout.add_argument("--scope-workspace", default="")
    evolve_web_scout.add_argument("--scope-user", default="")

    eval_cmd = sub.add_parser("eval")
    eval_sub = eval_cmd.add_subparsers(dest="eval_command")
    eval_run = eval_sub.add_parser("run")
    eval_run.add_argument("dataset_json")
    eval_run.add_argument("--task-type", default="")
    eval_run.add_argument("--profile", default="balanced")
    eval_run.add_argument("--no-seed", action="store_true")
    eval_run.add_argument("--output", default="")
    eval_ci = eval_sub.add_parser("ci")
    eval_ci.add_argument("dataset_json")
    eval_ci.add_argument("--threshold", type=float, default=None)
    eval_ci.add_argument("--emit-incidents", action="store_true")
    eval_ci.add_argument("--output", default="")
    eval_longmem = eval_sub.add_parser("longmem")
    eval_longmem.add_argument("dataset_json")
    eval_longmem.add_argument("--mode", choices=["raw", "hybrid"], default="raw")
    eval_longmem.add_argument("--granularity", choices=["session", "turn", "chunk"], default="session")
    eval_longmem.add_argument("--limit", type=int, default=10)
    eval_longmem.add_argument("--output", default="")
    eval_longmem.add_argument("--persist-report", action="store_true")
    eval_living = eval_sub.add_parser("living")
    eval_living.add_argument("dataset_json")
    eval_living.add_argument("--output", default="")
    eval_living.add_argument("--persist-report", action="store_true")
    eval_actionable = eval_sub.add_parser("actionable")
    eval_actionable.add_argument("dataset_json")
    eval_actionable.add_argument("--output", default="")
    eval_actionable.add_argument("--persist-report", action="store_true")
    eval_production_recall = eval_sub.add_parser("production-recall")
    eval_production_recall.add_argument("dataset_json")
    eval_production_recall.add_argument("--output", default="")
    eval_production_recall.add_argument("--no-seed", action="store_true")
    return parser


def _print_error(error: str, exc: Exception) -> int:
    print(
        json.dumps(
            {
                "ok": False,
                "error": error,
                "detail": str(exc),
                "exception": exc.__class__.__name__,
            },
            ensure_ascii=False,
        )
    )
    return 2


def _cli_scope(parsed: object, *, defaults: dict) -> dict:
    values = dict(defaults)
    scope_agent = getattr(parsed, "scope_agent", "")
    scope_workspace = getattr(parsed, "scope_workspace", "")
    scope_user = getattr(parsed, "scope_user", "")
    if scope_agent:
        values["agent_id"] = scope_agent
    if scope_workspace:
        values["workspace_id"] = scope_workspace
    if scope_user:
        values["user_id"] = scope_user
    return {
        "tenant_id": str(values.get("tenant_id") or "default"),
        "agent_id": str(values.get("agent_id") or "cli"),
        "workspace_id": str(values.get("workspace_id") or ""),
        "user_id": canonical_hongtu_user_id(values.get("user_id")),
    }


def _load_web_hypotheses(raw: str) -> list[dict]:
    raw_text = str(raw or "").strip()
    if not raw_text:
        return []
    try:
        loaded = json.loads(raw_text)
    except json.JSONDecodeError:
        path = Path(raw_text)
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError("invalid_web_evidence_json") from exc
        try:
            loaded = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid_web_evidence_json") from exc
    if isinstance(loaded, dict) and isinstance(loaded.get("hypotheses"), list):
        loaded_payload = loaded["hypotheses"]
    elif isinstance(loaded, dict):
        loaded_payload = [loaded]
    elif isinstance(loaded, list):
        loaded_payload = loaded
    else:
        raise ValueError("invalid_web_evidence_json")

    return [dict(item) for item in loaded_payload if isinstance(item, dict)]


def _living_enrich_report(runtime, scope: dict, *, limit: int) -> dict:
    return runtime.enrich_living_memory(scope=scope, limit=limit)


def _living_timeline_report(runtime, scope: dict, *, limit: int) -> dict:
    return runtime.build_living_timeline(scope=scope, limit=limit)


def _living_posture_report(runtime, scope: dict, *, query: str, limit: int) -> dict:
    return runtime.recommend_action_posture(query, scope=scope, limit=limit)


def main(argv: list[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    if args_list and args_list[0] == "qmd":
        return qmd_main(args_list[1:])
    parser = _build_parser()
    parsed = parser.parse_args(args_list)
    try:
        settings = load_settings()
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
        return _print_error("invalid_config", exc)
    runtime = Runtime.create(root=settings.root)
    scope = hongtu_scope(
        {
            "agent_id": settings.default_agent_id or "cli",
            "workspace_id": settings.default_workspace_id,
        }
    )
    if not parsed.command:
        print(
            json.dumps(
                {
                    "usage": "eimemory init|ingest|recall|paper|source|intake|export|import|backup|migrate|brief|nightly|quality|identity|living|reflect|governance|evolve|eval|serve-eibrain-rpc",
                }
            )
        )
        return 0
    if parsed.command == "serve-eibrain-rpc":
        host = parsed.host or settings.rpc_host
        port = int(parsed.port if parsed.port is not None else settings.rpc_port)
        server = EIBrainRPCServer(runtime, host=host, port=port)
        print(json.dumps({"ok": True, "host": server.address[0], "port": server.address[1]}, ensure_ascii=False))
        server.serve_forever()
        return 0

    if parsed.command == "init":
        runtime.store.root.mkdir(parents=True, exist_ok=True)
        (runtime.store.root / "state").mkdir(parents=True, exist_ok=True)
        print(json.dumps({"ok": True, "root": str(runtime.store.root)}, ensure_ascii=False, indent=2))
        return 0
    if parsed.command == "ingest":
        record = runtime.memory.ingest(
            text=parsed.text,
            memory_type=parsed.memory_type,
            title=parsed.title,
            scope=scope,
            source="cli",
        )
        print(json.dumps(record.to_dict(), ensure_ascii=False, indent=2))
        return 0
    if parsed.command == "recall":
        task_context = {"task_type": "cli.recall"}
        if parsed.view:
            task_context["recall_view"] = parsed.view
        bundle = runtime.memory.recall(
            query=parsed.query,
            scope=scope,
            task_context=task_context,
            limit=5,
        )
        print(json.dumps(bundle.to_dict(), ensure_ascii=False, indent=2))
        return 0
    if parsed.command == "paper":
        if parsed.paper_command == "ingest":
            paper_input = {
                "arxiv_id": parsed.arxiv_id,
                "doi": parsed.doi,
                "url": parsed.url,
                "pdf_file": parsed.pdf_file,
                "title": parsed.title,
                "abstract": parsed.abstract,
            }
            record = runtime.ingest_paper_source(
                {key: value for key, value in paper_input.items() if value},
                scope=scope,
            )
            print(json.dumps(record.to_dict(), ensure_ascii=False, indent=2))
            return 0
        if parsed.paper_command == "extract":
            source_record = runtime.store.get_by_id(parsed.paper_source_id, scope=scope)
            result = runtime.extract_paper_memory(
                {
                    "paper_source_id": parsed.paper_source_id,
                    "title": parsed.title or (source_record.title if source_record else ""),
                    "abstract": parsed.abstract or (source_record.summary if source_record else ""),
                    "body": parsed.body,
                    "provenance": {"paper_source_id": parsed.paper_source_id, "source": "cli.paper.extract"},
                },
                scope=scope,
            )
            print(json.dumps({"ok": True, "record_count": len(result.to_records(scope=scope))}, ensure_ascii=False, indent=2))
            return 0
        if parsed.paper_command == "compile":
            source_record = runtime.store.get_by_id(parsed.paper_source_id, scope=scope)
            title = parsed.title or (source_record.title if source_record else parsed.paper_source_id)
            claims = [
                record
                for record in runtime.store.list_records(kinds=["claim_card"], scope=scope, limit=1000)
                if str(record.provenance.get("paper_source_id") or record.meta.get("paper_source_id") or "") == parsed.paper_source_id
            ]
            entities = [
                record
                for record in runtime.store.list_records(kinds=["entity_record"], scope=scope, limit=1000)
                if str(record.provenance.get("paper_source_id") or record.meta.get("paper_source_id") or "") == parsed.paper_source_id
            ]
            result = compile_paper_knowledge(
                paper_source_id=parsed.paper_source_id,
                paper_title=title,
                claim_records=claims,
                entity_records=entities,
                provenance={"paper_source_id": parsed.paper_source_id, "source": "cli.paper.compile"},
            )
            records = result.to_records(scope=scope)
            for record in records:
                runtime.store.append(record)
            print(json.dumps({"ok": True, "record_count": len(records), "pages": [record.to_dict() for record in records]}, ensure_ascii=False, indent=2))
            return 0
        print(json.dumps({"usage": "eimemory paper ingest|extract|compile"}))
        return 0
    if parsed.command == "source":
        if parsed.source_command == "add":
            record = runtime.sources.add_source(
                {
                    "source_kind": parsed.source_kind,
                    "title": parsed.title,
                    "uri": parsed.uri,
                    "tags": list(parsed.tag or []),
                    "enabled": bool(parsed.enabled),
                }
            )
            print(json.dumps(record.to_dict(), ensure_ascii=False, indent=2))
            return 0
        if parsed.source_command == "list":
            sources = runtime.sources.list_sources(
                enabled=True if parsed.enabled_only else None,
                source_kind=parsed.source_kind or None,
            )
            print(json.dumps([item.to_dict() for item in sources], ensure_ascii=False, indent=2))
            return 0
        if parsed.source_command == "scan":
            report = runtime.sources.scan_sources(store=runtime.store, scope=scope, persist=bool(parsed.persist))
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        if parsed.source_command == "discover":
            report = runtime.discover_sources(
                scope=scope,
                persist=bool(parsed.persist),
                gap_queries=list(parsed.gap or []),
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        if parsed.source_command == "expand":
            if parsed.max_apply < 0:
                print(json.dumps({"ok": False, "error": "invalid_max_apply"}, ensure_ascii=False))
                return 2
            if parsed.min_score < 0.0 or parsed.min_score > 1.0:
                print(json.dumps({"ok": False, "error": "invalid_min_score"}, ensure_ascii=False))
                return 2
            report = runtime.expand_sources_autonomously(
                scope=scope,
                apply=bool(parsed.apply),
                max_apply=parsed.max_apply,
                min_score=parsed.min_score,
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        print(json.dumps({"usage": "eimemory source add|list|scan|discover|expand"}))
        return 0
    if parsed.command == "intake":
        if parsed.intake_command in {"run", "report"}:
            if parsed.limit is not None and parsed.limit <= 0:
                print(json.dumps({"ok": False, "error": "invalid_limit"}, ensure_ascii=False))
                return 2
            try:
                report = runtime.run_knowledge_intake(
                    scope=scope,
                    persist=bool(parsed.persist) if parsed.intake_command == "run" else False,
                    source_kind=parsed.source_kind or None,
                    limit=parsed.limit,
                )
            except ImportError as exc:
                print(json.dumps({"ok": False, "error": "knowledge_intake_loop_unavailable", "detail": str(exc)}, ensure_ascii=False))
                return 2
            except Exception as exc:
                print(json.dumps({"ok": False, "error": "knowledge_intake_loop_failed", "detail": str(exc)}, ensure_ascii=False))
                return 2
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report.get("ok", True) else 1
        if parsed.intake_command == "collect":
            if parsed.limit is not None and parsed.limit <= 0:
                print(json.dumps({"ok": False, "error": "invalid_limit"}, ensure_ascii=False))
                return 2
            report = runtime.collect_external_sources(
                source_kind=parsed.source_kind or None,
                limit=parsed.limit,
                fetch=bool(parsed.fetch),
                persist=bool(parsed.persist),
                scope=scope,
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        if parsed.intake_command == "queue":
            if parsed.limit <= 0:
                print(json.dumps({"ok": False, "error": "invalid_limit"}, ensure_ascii=False))
                return 2
            records = runtime.list_intake_review_queue(
                scope=scope,
                status=list(parsed.status or []) or None,
                limit=parsed.limit,
            )
            if parsed.explain:
                records = [
                    runtime.explain_intake_candidate(record_id=str(record["record_id"]), scope=scope)
                    for record in records
                ]
            print(json.dumps(records, ensure_ascii=False, indent=2))
            return 0
        if parsed.intake_command == "explain":
            if parsed.limit < 0:
                print(json.dumps({"ok": False, "error": "invalid_limit"}, ensure_ascii=False))
                return 2
            try:
                if parsed.record_id:
                    report = runtime.explain_intake_candidate(record_id=parsed.record_id, scope=scope)
                else:
                    limit = parsed.limit or 20
                    records = runtime.list_intake_review_queue(scope=scope, limit=limit)
                    report = [
                        runtime.explain_intake_candidate(record_id=str(record["record_id"]), scope=scope)
                        for record in records
                    ]
            except ValueError as exc:
                print(json.dumps({"ok": False, "error": "explain_failed", "detail": str(exc)}, ensure_ascii=False))
                return 2
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        if parsed.intake_command == "review":
            try:
                record = runtime.review_intake_candidate(
                    record_id=parsed.record_id,
                    decision=parsed.decision,
                    reviewer=parsed.reviewer,
                    note=parsed.note,
                    scope=scope,
                )
            except ValueError as exc:
                print(json.dumps({"ok": False, "error": "review_failed", "detail": str(exc)}, ensure_ascii=False))
                return 2
            print(json.dumps(record.to_dict(), ensure_ascii=False, indent=2))
            return 0
        if parsed.intake_command == "promote":
            try:
                record = runtime.promote_intake_candidate(
                    record_id=parsed.record_id,
                    promoter=parsed.promoter,
                    note=parsed.note,
                    scope=scope,
                )
            except ValueError as exc:
                print(json.dumps({"ok": False, "error": "promotion_failed", "detail": str(exc)}, ensure_ascii=False))
                return 2
            print(json.dumps(record.to_dict(), ensure_ascii=False, indent=2))
            return 0
        if parsed.intake_command == "merge":
            try:
                record = runtime.merge_intake_candidates(
                    source_record_id=parsed.source_record_id,
                    target_record_id=parsed.target_record_id,
                    reviewer=parsed.reviewer,
                    note=parsed.note,
                    scope=scope,
                )
            except ValueError as exc:
                print(json.dumps({"ok": False, "error": "merge_failed", "detail": str(exc)}, ensure_ascii=False))
                return 2
            print(json.dumps(record.to_dict(), ensure_ascii=False, indent=2))
            return 0
        if parsed.intake_command == "paper-promote":
            candidate = runtime.store.get_by_id(parsed.record_id, scope=scope)
            if candidate is None:
                print(json.dumps({"ok": False, "error": "candidate_not_found"}, ensure_ascii=False))
                return 2
            report = runtime.promote_paper_candidate(candidate, scope=scope)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report.get("ok") else 1
        if parsed.intake_command == "policy":
            report = runtime.collection_policy(scope=scope, topic_gaps=list(parsed.gap or []))
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        if parsed.intake_command == "pack":
            try:
                if parsed.pack_command == "export":
                    report = runtime.export_knowledge_pack(
                        parsed.path,
                        scope=scope,
                        include_candidates=bool(parsed.include_candidates),
                    )
                elif parsed.pack_command == "import":
                    report = runtime.import_knowledge_pack(
                        parsed.path,
                        scope=scope,
                        dry_run=bool(parsed.dry_run),
                    )
                else:
                    print(json.dumps({"usage": "eimemory intake pack export|import"}))
                    return 0
            except ValueError as exc:
                print(json.dumps({"ok": False, "error": "pack_failed", "detail": str(exc)}, ensure_ascii=False))
                return 2
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        print(json.dumps({"usage": "eimemory intake run|report|collect|queue|explain|review|promote|merge|paper-promote|policy|pack"}))
        return 0
    if parsed.command == "export":
        try:
            count = export_records(runtime, parsed.path)
        except Exception as exc:
            return _print_error("export_failed", exc)
        print(json.dumps({"ok": True, "count": count, "path": parsed.path}, ensure_ascii=False, indent=2))
        return 0
    if parsed.command == "import":
        try:
            count = import_records(runtime, parsed.path)
        except Exception as exc:
            return _print_error("import_failed", exc)
        print(json.dumps({"ok": True, "count": count, "path": parsed.path}, ensure_ascii=False, indent=2))
        return 0
    if parsed.command == "backup":
        try:
            if parsed.backup_command == "create":
                report = backup_create(runtime, parsed.path)
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return 0
            if parsed.backup_command == "verify":
                report = backup_verify(parsed.path)
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return 0 if report.get("ok") else 1
        except Exception as exc:
            return _print_error("backup_failed", exc)
        print(json.dumps({"usage": "eimemory backup create|verify"}))
        return 0
    if parsed.command == "migrate":
        try:
            if parsed.migrate_command == "scan":
                report = scan_migration_source(parsed.path)
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return 0
            if parsed.migrate_command == "import":
                report = scan_migration_source(parsed.path)
                imported = import_candidates(
                    runtime,
                    report["candidates"],
                    scope=scope,
                    candidate_ids=list(parsed.candidate_id or []),
                )
                print(json.dumps({"ok": True, "imported": imported, "path": parsed.path}, ensure_ascii=False, indent=2))
                return 0
            if parsed.migrate_command == "report":
                report = scan_migration_source(parsed.path)
                rendered = build_review_report(report)
                output_path = parsed.output
                with open(output_path, "w", encoding="utf-8") as handle:
                    handle.write(rendered)
                print(json.dumps({"ok": True, "output": output_path, "accepted_count": report["accepted_count"]}, ensure_ascii=False, indent=2))
                return 0
        except Exception as exc:
            return _print_error("migrate_failed", exc)
        print(json.dumps({"usage": "eimemory migrate scan|import|report"}))
        return 0
    if parsed.command == "brief":
        if parsed.brief_command == "daily":
            report = runtime.build_daily_brief(
                scope=scope,
                date=parsed.date or None,
                persist=bool(parsed.persist),
                channel=parsed.channel,
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        print(json.dumps({"usage": "eimemory brief daily"}))
        return 0
    if parsed.command == "nightly":
        report = run_nightly_jobs(
            runtime,
            scope=scope,
        )
        report["identity_repair"] = repair_hongtu_identity(runtime, apply=True)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if parsed.command == "quality":
        if parsed.quality_command == "stats":
            report = runtime.evolution.memory_quality_report(scope=scope)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        if parsed.quality_command == "repair":
            report = runtime.evolution.repair_memory_quality(
                scope=scope,
                apply=bool(parsed.apply),
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        print(json.dumps({"usage": "eimemory quality stats|repair"}))
        return 0
    if parsed.command == "identity":
        if parsed.identity_command == "report":
            report = identity_report(runtime)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        if parsed.identity_command == "repair":
            if parsed.limit < 0:
                print(json.dumps({"ok": False, "error": "invalid_limit"}, ensure_ascii=False))
                return 2
            report = repair_hongtu_identity(
                runtime,
                apply=bool(parsed.apply),
                limit=parsed.limit or None,
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        print(json.dumps({"usage": "eimemory identity report|repair"}))
        return 0
    if parsed.command == "living":
        if parsed.living_command == "enrich":
            report = _living_enrich_report(runtime, scope, limit=parsed.limit)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report.get("ok") else 2
        if parsed.living_command == "timeline":
            report = _living_timeline_report(runtime, scope, limit=parsed.limit)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report.get("ok") else 2
        if parsed.living_command == "posture":
            report = _living_posture_report(runtime, scope, query=parsed.query, limit=parsed.limit)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report.get("ok") else 2
        print(json.dumps({"usage": "eimemory living enrich|timeline|posture"}))
        return 0
    if parsed.command == "openclaw-hook":
        try:
            event = json.loads(sys.stdin.read() or "{}")
        except json.JSONDecodeError:
            print(json.dumps({"ok": False, "error": "invalid_json"}, ensure_ascii=False))
            return 2
        if not isinstance(event, dict):
            print(json.dumps({"ok": False, "error": "invalid_event"}, ensure_ascii=False))
            return 2
        hooks = OpenClawMemoryHooks(runtime)
        if parsed.hook == "message_received":
            payload = hooks.on_message_received(event)
        elif parsed.hook == "before_prompt_build":
            payload = hooks.before_prompt_build(event)
        elif parsed.hook == "agent_end":
            payload = hooks.on_agent_end(event)
        elif parsed.hook == "task_end":
            payload = hooks.on_task_end(event)
        else:
            payload = hooks.on_session_end(event)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if parsed.command == "ei-bridge":
        if parsed.ei_bridge_command == "feishu":
            try:
                event = json.loads(sys.stdin.read() or "{}")
            except json.JSONDecodeError:
                print(json.dumps({"ok": False, "error": "invalid_json"}, ensure_ascii=False))
                return 2
            if not isinstance(event, dict):
                print(json.dumps({"ok": False, "error": "invalid_event"}, ensure_ascii=False))
                return 2
            try:
                payload = handle_openclaw_feishu_event(event, runtime)
            except Exception as exc:
                return _print_error("ei_bridge_failed", exc)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        print(json.dumps({"usage": "eimemory ei-bridge feishu"}, ensure_ascii=False))
        return 0
    if parsed.command == "governance":
        if parsed.governance_command == "snapshot":
            snapshot = build_governance_snapshot(runtime, scope)
            print(json.dumps(snapshot, ensure_ascii=False, indent=2))
            return 0
        if parsed.governance_command == "console":
            snapshot = build_governance_snapshot(runtime, scope)
            output_path = Path(parsed.output)
            try:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                report = write_evolution_console(snapshot, output_path)
            except OSError as exc:
                print(json.dumps({"ok": False, "error": "console_write_failed", "detail": str(exc)}, ensure_ascii=False))
                return 2
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        print(json.dumps({"usage": "eimemory governance snapshot|console"}))
        return 0
    if parsed.command == "evolve":
        if parsed.evolve_command == "evaluate":
            try:
                with open(parsed.dataset_json, "r", encoding="utf-8") as handle:
                    dataset = json.load(handle)
            except OSError as exc:
                print(json.dumps({"ok": False, "error": "dataset_unreadable", "detail": str(exc)}, ensure_ascii=False))
                return 2
            except json.JSONDecodeError:
                print(json.dumps({"ok": False, "error": "invalid_dataset_json"}, ensure_ascii=False))
                return 2
            if not isinstance(dataset, list):
                print(json.dumps({"ok": False, "error": "dataset must be a list"}, ensure_ascii=False))
                return 2
            report = runtime.evolution.evaluate_recall_dataset(
                dataset=dataset,
                scope=scope,
                task_type=parsed.task_type,
                profile=parsed.profile,
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        if parsed.evolve_command == "promotions":
            min_pass_rate = parsed.min_pass_rate
            if min_pass_rate != min_pass_rate or min_pass_rate < 0.0 or min_pass_rate > 1.0:
                print(json.dumps({"ok": False, "error": "min_pass_rate_out_of_range"}, ensure_ascii=False))
                return 2
            report = runtime.evolution.promotion_candidates(
                scope=scope,
                min_pass_rate=min_pass_rate,
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        if parsed.evolve_command == "loop":
            min_roi = parsed.min_roi
            if min_roi != min_roi:
                print(json.dumps({"ok": False, "error": "invalid_min_roi"}, ensure_ascii=False))
                return 2
            report = runtime.run_rule_evolution(
                scope=scope,
                apply=bool(parsed.apply),
                min_roi=min_roi,
                persist_report=bool(parsed.persist_report),
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        if parsed.evolve_command == "autonomous":
            max_apply = int(parsed.max_apply)
            if max_apply < 0:
                print(json.dumps({"ok": False, "error": "invalid_max_apply"}, ensure_ascii=False))
                return 2
            try:
                web_evidence = _load_web_hypotheses(parsed.web_evidence_json)
            except ValueError as exc:
                print(
                    json.dumps(
                        {"ok": False, "error": "invalid_web_evidence_json", "detail": str(exc)},
                        ensure_ascii=False,
                    )
                )
                return 2
            report = runtime.run_autonomous_evolution(
                scope=_cli_scope(parsed, defaults=scope),
                apply=bool(parsed.apply),
                max_apply=max_apply,
                web_hypotheses=web_evidence,
                persist_report=bool(parsed.persist_report),
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        if parsed.evolve_command == "web-scout":
            timeout_seconds = max(1, int(parsed.timeout_seconds))
            try:
                evidence = _load_web_hypotheses(parsed.evidence_json)
            except ValueError as exc:
                print(
                    json.dumps(
                        {"ok": False, "error": "invalid_web_evidence_json", "detail": str(exc)},
                        ensure_ascii=False,
                    )
                )
                return 2
            report = runtime.scout_web_learning(
                scope=_cli_scope(parsed, defaults=scope),
                urls=list(parsed.url or []),
                evidence=evidence,
                timeout_seconds=timeout_seconds,
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        print(json.dumps({"usage": "eimemory evolve evaluate|promotions|loop|autonomous|web-scout"}))
        return 0
    if parsed.command == "eval":
        if parsed.eval_command == "run":
            try:
                with open(parsed.dataset_json, "r", encoding="utf-8") as handle:
                    dataset = json.load(handle)
            except OSError as exc:
                print(json.dumps({"ok": False, "error": "dataset_unreadable", "detail": str(exc)}, ensure_ascii=False))
                return 2
            except json.JSONDecodeError:
                print(json.dumps({"ok": False, "error": "invalid_dataset_json"}, ensure_ascii=False))
                return 2
            try:
                report = runtime.run_evaluation(
                    dataset,
                    scope=scope,
                    task_type=parsed.task_type,
                    profile=parsed.profile,
                    seed=not bool(parsed.no_seed),
                )
            except ValueError as exc:
                print(json.dumps({"ok": False, "error": "invalid_eval_dataset", "detail": str(exc)}, ensure_ascii=False))
                return 2
            if parsed.output:
                try:
                    output_path = Path(parsed.output)
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                except OSError as exc:
                    print(json.dumps({"ok": False, "error": "eval_output_failed", "detail": str(exc)}, ensure_ascii=False))
                    return 2
                report = {**report, "output": str(output_path)}
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report.get("ok") else 1
        if parsed.eval_command == "ci":
            try:
                with open(parsed.dataset_json, "r", encoding="utf-8") as handle:
                    dataset = json.load(handle)
            except OSError as exc:
                print(
                    json.dumps(
                        {"ok": False, "error": "dataset_unreadable", "detail": str(exc)},
                        ensure_ascii=False,
                    )
                )
                return 2
            except json.JSONDecodeError:
                print(json.dumps({"ok": False, "error": "invalid_dataset_json"}, ensure_ascii=False))
                return 2
            if parsed.threshold is not None and isinstance(dataset, dict):
                dataset = {**dataset, "threshold": parsed.threshold}
            try:
                report = runtime.run_memory_eval_ci(dataset, emit_incidents=bool(parsed.emit_incidents))
            except ValueError as exc:
                print(json.dumps({"ok": False, "error": "invalid_eval_dataset", "detail": str(exc)}, ensure_ascii=False))
                return 2
            if parsed.output:
                try:
                    output_path = Path(parsed.output)
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                except OSError as exc:
                    print(
                        json.dumps(
                            {"ok": False, "error": "eval_output_failed", "detail": str(exc)},
                            ensure_ascii=False,
                        )
                    )
                    return 2
                report = {**report, "output": str(output_path)}
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report.get("passed_threshold") else 1
        if parsed.eval_command == "longmem":
            try:
                with open(parsed.dataset_json, "r", encoding="utf-8") as handle:
                    dataset = json.load(handle)
            except OSError as exc:
                print(json.dumps({"ok": False, "error": "dataset_unreadable", "detail": str(exc)}, ensure_ascii=False))
                return 2
            except json.JSONDecodeError:
                print(json.dumps({"ok": False, "error": "invalid_dataset_json"}, ensure_ascii=False))
                return 2
            if parsed.limit <= 0:
                print(json.dumps({"ok": False, "error": "invalid_limit"}, ensure_ascii=False))
                return 2
            try:
                from eimemory.evaluation import run_longmemeval

                report = run_longmemeval(
                    runtime,
                    dataset,
                    mode=parsed.mode,
                    granularity=parsed.granularity,
                    limit=parsed.limit,
                    persist_report=bool(parsed.persist_report),
                )
            except ValueError as exc:
                print(json.dumps({"ok": False, "error": "invalid_eval_dataset", "detail": str(exc)}, ensure_ascii=False))
                return 2
            if parsed.output:
                try:
                    output_path = Path(parsed.output)
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                except OSError as exc:
                    print(json.dumps({"ok": False, "error": "eval_output_failed", "detail": str(exc)}, ensure_ascii=False))
                    return 2
                report = {**report, "output": str(output_path)}
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report.get("ok") else 1
        if parsed.eval_command == "living":
            try:
                with open(parsed.dataset_json, "r", encoding="utf-8") as handle:
                    dataset = json.load(handle)
            except OSError as exc:
                print(json.dumps({"ok": False, "error": "dataset_unreadable", "detail": str(exc)}, ensure_ascii=False))
                return 2
            except json.JSONDecodeError:
                print(json.dumps({"ok": False, "error": "invalid_dataset_json"}, ensure_ascii=False))
                return 2
            try:
                from eimemory.evaluation import run_livingmem_eval

                report = run_livingmem_eval(
                    runtime,
                    dataset,
                    persist_report=bool(parsed.persist_report),
                )
            except ValueError as exc:
                print(json.dumps({"ok": False, "error": "invalid_eval_dataset", "detail": str(exc)}, ensure_ascii=False))
                return 2
            if parsed.output:
                try:
                    output_path = Path(parsed.output)
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                except OSError as exc:
                    print(json.dumps({"ok": False, "error": "eval_output_failed", "detail": str(exc)}, ensure_ascii=False))
                    return 2
                report = {**report, "output": str(output_path)}
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report.get("ok") else 1
        if parsed.eval_command == "actionable":
            try:
                with open(parsed.dataset_json, "r", encoding="utf-8") as handle:
                    dataset = json.load(handle)
            except OSError as exc:
                print(json.dumps({"ok": False, "error": "dataset_unreadable", "detail": str(exc)}, ensure_ascii=False))
                return 2
            except json.JSONDecodeError:
                print(json.dumps({"ok": False, "error": "invalid_dataset_json"}, ensure_ascii=False))
                return 2
            try:
                from eimemory.evaluation import run_actionable_memory_eval

                report = run_actionable_memory_eval(
                    runtime,
                    dataset,
                    persist_report=bool(parsed.persist_report),
                )
            except ValueError as exc:
                print(json.dumps({"ok": False, "error": "invalid_eval_dataset", "detail": str(exc)}, ensure_ascii=False))
                return 2
            if parsed.output:
                try:
                    output_path = Path(parsed.output)
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                except OSError as exc:
                    print(
                        json.dumps(
                            {"ok": False, "error": "eval_output_failed", "detail": str(exc)},
                            ensure_ascii=False,
                        )
                    )
                    return 2
                report = {**report, "output": str(output_path)}
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report.get("ok") else 1
        if parsed.eval_command == "production-recall":
            try:
                with open(parsed.dataset_json, "r", encoding="utf-8") as handle:
                    dataset = json.load(handle)
            except OSError as exc:
                print(
                    json.dumps(
                        {"ok": False, "error": "dataset_unreadable", "detail": str(exc)},
                        ensure_ascii=False,
                    )
                )
                return 2
            except json.JSONDecodeError:
                print(json.dumps({"ok": False, "error": "invalid_dataset_json"}, ensure_ascii=False))
                return 2
            try:
                from eimemory.evaluation import run_production_recall_eval

                report = run_production_recall_eval(runtime, dataset, seed=not bool(parsed.no_seed))
            except ValueError as exc:
                print(json.dumps({"ok": False, "error": "invalid_eval_dataset", "detail": str(exc)}, ensure_ascii=False))
                return 2
            if parsed.output:
                try:
                    output_path = Path(parsed.output)
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                except OSError as exc:
                    print(
                        json.dumps(
                            {"ok": False, "error": "eval_output_failed", "detail": str(exc)},
                            ensure_ascii=False,
                        )
                    )
                    return 2
                report = {**report, "output": str(output_path)}
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report.get("ok") else 1
        print(json.dumps({"usage": "eimemory eval run|ci|longmem|living|actionable|production-recall"}))
        return 0
    if parsed.command == "reflect":
        if parsed.reflect_command == "check":
            report = runtime.evolution.reflection_check(scope=scope)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        if parsed.reflect_command == "log":
            record = runtime.evolution.log_reflection(
                tag=parsed.tag,
                miss=parsed.miss,
                fix=parsed.fix,
                scope=scope,
            )
            print(json.dumps(record.to_dict(), ensure_ascii=False, indent=2))
            return 0
        if parsed.reflect_command == "read":
            try:
                limit = int(parsed.count)
            except ValueError:
                print(json.dumps({"ok": False, "error": "invalid count"}, ensure_ascii=False))
                return 2
            if limit <= 0:
                print(json.dumps({"ok": False, "error": "invalid count"}, ensure_ascii=False))
                return 2
            records = runtime.evolution.read_reflections(scope=scope, limit=limit)
            print(json.dumps([record.to_dict() for record in records], ensure_ascii=False, indent=2))
            return 0
        if parsed.reflect_command == "stats":
            report = runtime.evolution.reflection_stats(scope=scope)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        print(json.dumps({"usage": "eimemory reflect check|log|read|stats"}))
        return 0
    print(json.dumps({"error": f"unknown command: {parsed.command}"}))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
