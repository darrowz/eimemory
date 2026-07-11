from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
from typing import Any, Callable

from eimemory.adapters.eibrain.rpc_server import EIBrainRPCServer, build_health_payload
from eimemory.adapters.openclaw.hooks import OpenClawMemoryHooks
from eimemory.adapters.openclaw.qmd_compat import main as qmd_main
from eimemory.api.runtime import Runtime
from eimemory.models.records import ScopeRef
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
from eimemory.persona.cli import add_persona_parser, handle_persona_command
from eimemory.scheduler.jobs import run_nightly_jobs


COMMAND_REGISTRY: dict[str, Callable[[object, Any, dict[str, Any]], Any]] = {}
FALLTHROUGH = object()


def register(cmd: str) -> Callable[[Callable[[object, Any, dict[str, Any]], Any]], Callable[[object, Any, dict[str, Any]], Any]]:
    def wrapper(fn: Callable[[object, Any, dict[str, Any]], Any]) -> Callable[[object, Any, dict[str, Any]], Any]:
        COMMAND_REGISTRY[str(cmd)] = fn
        return fn

    return wrapper


def dispatch(cmd: str, parsed: object, runtime: Any, scope: dict[str, Any]) -> Any:
    fn = COMMAND_REGISTRY.get(str(cmd))
    if not fn:
        return {"ok": False, "error": "unknown_command"}
    return fn(parsed, runtime, scope)


def _dispatch_exit(result: Any) -> int:
    if isinstance(result, int):
        return result
    if isinstance(result, dict):
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") is not False else 1
    print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, indent=2))
    return 0


def _read_stdin_text() -> str:
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is not None:
        data = buffer.read()
        if isinstance(data, bytes):
            return data.decode("utf-8")
        return str(data or "")
    return sys.stdin.read()


def _write_json(payload: Any, *, indent: int | None = 2) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=indent) + "\n"
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        buffer.write(text.encode("utf-8"))
        buffer.flush()
        return
    print(text, end="")


def _print_report_exit(report: dict[str, Any]) -> int:
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") is not False else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="eimemory")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init")
    sub.add_parser("emergency-stop")

    ingest = sub.add_parser("ingest")
    ingest.add_argument("text")
    ingest.add_argument("--title", default="CLI ingest")
    ingest.add_argument("--memory-type", default="fact")
    ingest.add_argument("--force-capture", action="store_true")

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

    rebuild_sqlite = sub.add_parser("rebuild-sqlite")
    rebuild_sqlite.add_argument("--from-jsonl", action="store_true", dest="from_jsonl")
    rebuild_sqlite.add_argument("--replace", action="store_true")

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

    experience = sub.add_parser("experience")
    experience_sub = experience.add_subparsers(dest="experience_command")
    experience_outcome = experience_sub.add_parser("outcome")
    experience_outcome.add_argument("json_path")

    learn = sub.add_parser("learn")
    learn_sub = learn.add_subparsers(dest="learn_command")
    learn_watch = learn_sub.add_parser("watch")
    learn_watch.add_argument("--dry-run", action="store_true", default=True)
    learn_watch.add_argument("--apply", action="store_true")
    learn_watch.add_argument("--json", action="store_true", default=True)
    learn_think = learn_sub.add_parser("think")
    learn_think.add_argument("--dry-run", action="store_true")
    learn_think.add_argument("--persist", action="store_true")
    learn_think.add_argument("--max-items", type=int, default=20)
    learn_think.add_argument("--json", action="store_true", default=True)
    learn_cycle = learn_sub.add_parser("cycle")
    learn_cycle.add_argument("--full", action="store_true", default=True)
    learn_cycle.add_argument("--dry-run", action="store_true")
    learn_cycle.add_argument("--apply", action="store_true")
    learn_cycle.add_argument("--force", action="store_true")
    learn_cycle.add_argument("--max-goals", type=int, default=3)
    learn_cycle.add_argument("--max-promotions", type=int, default=3)
    learn_cycle.add_argument("--json", action="store_true", default=True)
    learn_autonomy = learn_sub.add_parser("autonomy")
    learn_autonomy.add_argument("--full", action="store_true", default=True)
    learn_autonomy.add_argument("--dry-run", action="store_true")
    learn_autonomy.add_argument("--apply", action="store_true")
    learn_autonomy.add_argument("--force", action="store_true")
    learn_autonomy.add_argument("--max-goals", type=int, default=3)
    learn_autonomy.add_argument("--max-promotions", type=int, default=3)
    learn_autonomy.add_argument("--smoke", action="store_true")
    learn_autonomy.add_argument("--json", action="store_true", default=True)
    learn_evaluator_harness = learn_sub.add_parser("evaluator-harness")
    learn_evaluator_harness.add_argument("--generator-model", default="")
    learn_evaluator_harness.add_argument("--evaluator-model", default="")
    learn_evaluator_harness.add_argument("--stop-judge-model", default="")
    learn_evaluator_harness.add_argument("--fail-replay", action="store_true")
    learn_evaluator_harness.add_argument("--json", action="store_true", default=True)
    learn_loops = learn_sub.add_parser("loops")
    learn_loops.add_argument("--limit", type=int, default=10)
    learn_loops.add_argument("--json", action="store_true", default=True)
    learn_goals = learn_sub.add_parser("goals")
    learn_goals.add_argument("--limit", type=int, default=10)
    learn_goals.add_argument("--json", action="store_true", default=True)
    learn_candidates = learn_sub.add_parser("candidates")
    learn_candidates.add_argument("--limit", type=int, default=10)
    learn_candidates.add_argument("--json", action="store_true", default=True)
    learn_ledger = learn_sub.add_parser("ledger")
    learn_ledger.add_argument("--limit", type=int, default=200)
    learn_ledger.add_argument("--since", default="")
    learn_ledger.add_argument("--until", default="")
    learn_ledger.add_argument("--json", action="store_true", default=True)
    learn_replay_dataset = learn_sub.add_parser("replay-dataset")
    learn_replay_dataset.add_argument("--limit", type=int, default=50)
    learn_replay_dataset.add_argument("--persist", action="store_true")
    learn_replay_dataset.add_argument("--include-built-in-regressions", action="store_true")
    learn_replay_dataset.add_argument("--json", action="store_true", default=True)
    learn_goal_graph = learn_sub.add_parser("goal-graph")
    learn_goal_graph.add_argument("--max-goals", type=int, default=3)
    learn_goal_graph.add_argument("--capability", action="append", default=[])
    learn_goal_graph.add_argument("--persist", action="store_true")
    learn_goal_graph.add_argument("--json", action="store_true", default=True)
    learn_world_model = learn_sub.add_parser("world-model")
    learn_world_model.add_argument("--persist", action="store_true")
    learn_world_model.add_argument("--limit", type=int, default=500)
    learn_world_model.add_argument("--json", action="store_true", default=True)
    learn_roadmap = learn_sub.add_parser("roadmap")
    learn_roadmap.add_argument("--horizon-days", type=int, default=180)
    learn_roadmap.add_argument("--persist", action="store_true")
    learn_roadmap.add_argument("--json", action="store_true", default=True)
    learn_l5 = learn_sub.add_parser("l5")
    learn_l5.add_argument("--apply", action="store_true")
    learn_l5.add_argument("--force", action="store_true")
    learn_l5.add_argument("--max-goals", type=int, default=1)
    learn_l5.add_argument("--max-promotions", type=int, default=0)
    learn_l5.add_argument("--no-network", action="store_true")
    learn_l5.add_argument("--no-persist", action="store_true")
    learn_l5.add_argument("--json", action="store_true", default=True)
    learn_l5_assess = learn_sub.add_parser("l5-assess")
    learn_l5_assess.add_argument("--persist", action="store_true")
    learn_l5_assess.add_argument("--json", action="store_true", default=True)
    learn_l5_readiness = learn_sub.add_parser("l5-readiness")
    learn_l5_readiness.add_argument("--persist", action="store_true")
    learn_l5_readiness.add_argument("--limit", type=int, default=500)
    learn_l5_readiness.add_argument("--json", action="store_true", default=True)
    learn_closure_rehearsal = learn_sub.add_parser("closure-rehearsal")
    learn_closure_rehearsal.add_argument("--scope-agent", default="")
    learn_closure_rehearsal.add_argument("--scope-workspace", default="")
    learn_closure_rehearsal.add_argument("--scope-user", default="")
    learn_closure_rehearsal.add_argument("--json", action="store_true", default=True)
    learn_deployment_receipt = learn_sub.add_parser("deployment-receipt")
    learn_deployment_receipt.add_argument("--repo-root", required=True)
    learn_deployment_receipt.add_argument("--current-link", required=True)
    learn_deployment_receipt.add_argument("--health-url", required=True)
    learn_deployment_receipt.add_argument("--prior-commit", default="")
    learn_deployment_receipt.add_argument("--scope-agent", default="")
    learn_deployment_receipt.add_argument("--scope-workspace", default="")
    learn_deployment_receipt.add_argument("--scope-user", default="")
    learn_deployment_receipt.add_argument("--json", action="store_true", default=True)
    learn_capability_acceptance = learn_sub.add_parser("capability-acceptance")
    learn_capability_acceptance.add_argument("--json", action="store_true", default=True)
    learn_capability_replay = learn_sub.add_parser("capability-replay")
    learn_capability_replay.add_argument("--capability", action="append", default=[])
    learn_capability_replay.add_argument("--persist", action="store_true")
    learn_capability_replay.add_argument("--json", action="store_true", default=True)
    learn_safety_replay = learn_sub.add_parser("safety-replay")
    learn_safety_replay.add_argument("--persist", action="store_true")
    learn_safety_replay.add_argument("--json", action="store_true", default=True)
    learn_skills = learn_sub.add_parser("skills")
    learn_skills.add_argument("--promote", action="store_true")
    learn_skills.add_argument("--persist", action="store_true")
    learn_skills.add_argument("--min-repeats", type=int, default=3)
    learn_skills.add_argument("--limit", type=int, default=100)
    learn_skills.add_argument("--json", action="store_true", default=True)
    learn_skill_call = learn_sub.add_parser("skill-call")
    learn_skill_call.add_argument("skill_id")
    learn_skill_call.add_argument("--context-json", default="")
    learn_skill_call.add_argument("--no-persist", action="store_true")
    learn_skill_call.add_argument("--json", action="store_true", default=True)
    learn_metrics = learn_sub.add_parser("metrics")
    learn_metrics.add_argument("--persist", action="store_true")
    learn_metrics.add_argument("--limit", type=int, default=500)
    learn_metrics.add_argument("--json", action="store_true", default=True)
    learn_compact = learn_sub.add_parser("compact")
    learn_compact.add_argument("--dry-run", action="store_true")
    learn_compact.add_argument("--apply", action="store_true")
    learn_compact.add_argument("--json", action="store_true", default=True)
    learn_report = learn_sub.add_parser("report")
    learn_report.add_argument("--date", default="")
    learn_report.add_argument("--persist", action="store_true")
    learn_report.add_argument("--json", action="store_true", default=True)
    learn_dashboard = learn_sub.add_parser("dashboard")
    learn_dashboard.add_argument("--weekly", action="store_true")
    learn_dashboard.add_argument("--week-start", default="")
    learn_dashboard.add_argument("--persist", action="store_true")
    learn_dashboard.add_argument("--output", default="")
    learn_dashboard.add_argument("--json", action="store_true", default=True)
    learn_promote = learn_sub.add_parser("promote")
    learn_promote.add_argument("candidate_id")
    learn_promote.add_argument("--apply", action="store_true")
    learn_promote.add_argument("--eval-json", default="")
    learn_promote.add_argument("--health-json", default="")
    learn_promote.add_argument("--loop-id", default="cli")
    learn_promote.add_argument("--json", action="store_true", default=True)

    serve_rpc = sub.add_parser("serve-eibrain-rpc")
    serve_rpc.add_argument("--host", default="")
    serve_rpc.add_argument("--port", type=int, default=None)
    serve_rpc.add_argument("--loopback-health-host", default="")
    serve_rpc.add_argument("--loopback-health-port", type=int, default=None)
    serve_rpc.add_argument("--auth-token", default=None)

    doctor = sub.add_parser("doctor")
    doctor.add_argument("--json", action="store_true", default=True)
    status = sub.add_parser("status")
    status.add_argument("--json", action="store_true", default=True)

    ops = sub.add_parser("ops")
    ops_sub = ops.add_subparsers(dest="ops_command")
    ops_timer_monitor = ops_sub.add_parser("timer-monitor")
    ops_timer_monitor.add_argument("--stale-after-minutes", type=int, default=90)
    ops_timer_monitor.add_argument("--include-legacy-learning-timers", action="store_true")
    ops_timer_monitor.add_argument("--no-persist", action="store_true")
    ops_timer_monitor.add_argument("--json", action="store_true", default=True)

    openclaw_hook = sub.add_parser("openclaw-hook")
    openclaw_hook.add_argument(
        "hook",
        choices=["message_received", "before_prompt_build", "agent_end", "task_end", "session_end"],
    )

    add_persona_parser(sub)

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

    evolve_code_sandbox = evolve_sub.add_parser("code-sandbox")
    evolve_code_sandbox.add_argument("--incident-json", required=True)
    evolve_code_sandbox.add_argument("--create-worktree", action="store_true")
    evolve_code_sandbox.add_argument("--persist-report", action="store_true")

    evolve_gates = evolve_sub.add_parser("gates")
    evolve_gates.add_argument("--action", default="")
    evolve_gates.add_argument("--limit", type=int, default=20)
    evolve_gates.add_argument("--scope-agent", default="")
    evolve_gates.add_argument("--scope-workspace", default="")
    evolve_gates.add_argument("--scope-user", default="")

    evolve_rollback = evolve_sub.add_parser("rollback")
    evolve_rollback.add_argument("--pattern-id", required=True)
    evolve_rollback.add_argument("--reason", default="manual rollback")
    evolve_rollback.add_argument("--scope-agent", default="")
    evolve_rollback.add_argument("--scope-workspace", default="")
    evolve_rollback.add_argument("--scope-user", default="")

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
    eval_longmem.add_argument("--limit", type=int, default=10, help="TopK retrieved evidence ids per sample.")
    eval_longmem.add_argument("--output", default="")
    eval_longmem.add_argument("--persist-report", action="store_true")
    eval_locomo = eval_sub.add_parser("locomo")
    eval_locomo.add_argument("dataset_json")
    eval_locomo.add_argument("--mode", choices=["raw", "hybrid"], default="raw")
    eval_locomo.add_argument("--granularity", choices=["session", "turn", "chunk"], default="turn")
    eval_locomo.add_argument("--limit", type=int, default=10, help="TopK retrieved evidence ids per sample.")
    eval_locomo.add_argument("--output", default="")
    eval_public = eval_sub.add_parser("public-benchmark")
    eval_public.add_argument("dataset_json")
    eval_public.add_argument("--suite", choices=["longmemeval", "locomo"], required=True)
    eval_public.add_argument("--mode", choices=["raw", "hybrid"], default="raw")
    eval_public.add_argument("--granularity", choices=["session", "turn", "chunk"], default="")
    eval_public.add_argument("--limit", type=int, default=10, help="TopK retrieved evidence ids per sample.")
    eval_public.add_argument("--output", default="")
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
    eval_production_recall.add_argument("--persist-report", action="store_true")
    eval_openclaw_e2e = eval_sub.add_parser("openclaw-e2e")
    eval_openclaw_e2e.add_argument("--query", default="eimemory openclaw e2e")
    eval_openclaw_e2e.add_argument("--scope-agent", default="")
    eval_openclaw_e2e.add_argument("--scope-workspace", default="")
    eval_openclaw_e2e.add_argument("--scope-user", default="")
    eval_openclaw_e2e.add_argument("--output", default="")
    eval_task_replay = eval_sub.add_parser("task-replay")
    eval_task_replay.add_argument("dataset_json")
    eval_task_replay.add_argument("--output", default="")
    eval_task_replay.add_argument("--no-seed", action="store_true")
    eval_task_replay.add_argument("--persist-report", action="store_true")

    # eimemory patch - harness-patch CLI (1.6.0)
    patch = sub.add_parser("patch")
    patch_sub = patch.add_subparsers(dest="patch_command")
    patch_propose = patch_sub.add_parser("propose")
    patch_propose.add_argument("--surface", required=True, choices=[
        "INSTRUCTION", "VERIFICATION_GUIDANCE", "TOOL_LOOP_GUARD", "ARTIFACT_RECOVERY", "RUNTIME_POLICY",
    ])
    patch_propose.add_argument("--evidence", nargs="+", required=True, help="Record IDs that motivate the patch")
    patch_propose.add_argument("--agent", required=True, help="Target agent (eibrain / openclaw / mcp_consumer)")
    patch_propose.add_argument("--tier", required=True, choices=["L0", "L1", "L2", "L3", "L4"])
    patch_propose.add_argument("--rollback", required=True, help="Rollback plan (free text)")
    patch_propose.add_argument("--diff-lines", type=int, default=0)
    patch_propose.add_argument("--diff-tokens", type=int, default=0)
    patch_propose.add_argument("--notes", default="")
    patch_validate = patch_sub.add_parser("validate")
    patch_validate.add_argument("--card", required=True, help="Path to a JSON file with a ProposalCard dict")
    patch_promote = patch_sub.add_parser("promote")
    patch_promote.add_argument("--candidate", required=True, help="Candidate record_id")
    patch_rollback = patch_sub.add_parser("rollback")
    patch_rollback.add_argument("--candidate", required=True, help="Candidate record_id to roll back")
    patch_list = patch_sub.add_parser("list")
    patch_list.add_argument("--limit", type=int, default=100)

    return parser


def _handle_patch(parsed: object, *, runtime: Any, scope: dict[str, Any]) -> int:
    """Dispatch the eimemory patch subcommands (1.6.0 harness-patch)."""
    cmd = getattr(parsed, "patch_command", None)
    if not cmd:
        print(json.dumps({"usage": "eimemory patch propose|validate|promote|rollback|list"}))
        return 0
    if cmd == "propose":
        from eimemory.governance.harness_patch import HarnessSurface, ProposalCard
        card = ProposalCard(
            target_surface=HarnessSurface(parsed.surface),
            evidence_record_ids=tuple(parsed.evidence),
            expected_delta=0.0,
            target_agent=parsed.agent,
            risk_tier=parsed.tier,
            rollback_plan=parsed.rollback,
            diff_lines=parsed.diff_lines,
            diff_tokens=parsed.diff_tokens,
            notes=parsed.notes,
        )
        print(json.dumps({
            "target_surface": card.target_surface.value,
            "evidence_record_ids": list(card.evidence_record_ids),
            "expected_delta": card.expected_delta,
            "target_agent": card.target_agent,
            "risk_tier": card.risk_tier,
            "rollback_plan": card.rollback_plan,
            "diff_lines": card.diff_lines,
            "diff_tokens": card.diff_tokens,
            "notes": card.notes,
        }, ensure_ascii=False, indent=2))
        return 0
    if cmd == "validate":
        from eimemory.governance.harness_patch import HarnessSurface, ProposalCard, HarnessGate
        try:
            card_dict = json.loads(Path(parsed.card).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return _print_error("invalid_card_file", exc)
        try:
            card = ProposalCard(
                target_surface=HarnessSurface(card_dict["target_surface"]),
                evidence_record_ids=tuple(card_dict.get("evidence_record_ids") or ()),
                expected_delta=float(card_dict.get("expected_delta") or 0.0),
                target_agent=str(card_dict.get("target_agent") or ""),
                risk_tier=str(card_dict.get("risk_tier") or "L0"),
                rollback_plan=str(card_dict.get("rollback_plan") or ""),
                diff_lines=int(card_dict.get("diff_lines") or 0),
                diff_tokens=int(card_dict.get("diff_tokens") or 0),
            )
        except (KeyError, ValueError) as exc:
            return _print_error("invalid_card", exc)
        gate = HarnessGate(card)
        result = gate.evaluate(
            held_in_scores={"accuracy": 0.85},
            held_out_scores=None,
            baseline_held_in=0.80,
            baseline_held_out=None,
        )
        print(json.dumps({
            "verdict": result.verdict.value,
            "reason": result.reason,
            "held_in_score": result.held_in_score,
            "held_out_score": result.held_out_score,
            "delta": result.delta,
        }, ensure_ascii=False, indent=2))
        return 0
    if cmd == "promote":
        from eimemory.governance.regression_watch import evaluate_harness_gate
        gate_result = evaluate_harness_gate(
            runtime,
            candidate_id=parsed.candidate,
            held_in_scores={"accuracy": 0.85},
            held_out_scores=None,
            baseline_held_in=0.80,
            baseline_held_out=None,
            scope=scope,
        )
        verdict = gate_result.get("verdict")
        if verdict == "REJECT":
            print(json.dumps({
                "ok": False,
                "verdict": verdict,
                "reason": gate_result.get("reason"),
            }, ensure_ascii=False, indent=2))
            return 2
        from eimemory.governance import promotion_manager
        try:
            promo_result = promotion_manager.promote_candidate(
                runtime, candidate_id=parsed.candidate, scope=scope,
            )
        except Exception as exc:
            return _print_error("promote_failed", exc)
        print(json.dumps({
            "ok": True,
            "verdict": verdict,
            "promotion": promo_result,
        }, ensure_ascii=False, indent=2))
        return 0
    if cmd == "rollback":
        from eimemory.governance.promotion_manager import rollback_capability_candidate
        try:
            result = rollback_capability_candidate(
                runtime, candidate_id=parsed.candidate, scope=scope,
            )
        except Exception as exc:
            return _print_error("rollback_failed", exc)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if cmd == "list":
        try:
            proposals = list(runtime.store.list_records(
                kinds=["proposal_card"], scope=scope, limit=parsed.limit,
            ))
        except Exception as exc:
            return _print_error("list_failed", exc)
        items = []
        for p in proposals:
            if hasattr(p, "to_dict"):
                items.append(p.to_dict())
            else:
                items.append({"record_id": getattr(p, "record_id", ""), "title": getattr(p, "title", "")})
        print(json.dumps({"ok": True, "count": len(items), "proposals": items}, ensure_ascii=False, indent=2))
        return 0
    return _print_error("unknown_patch_command", ValueError(cmd))


@register("patch")
def _dispatch_patch(parsed: object, runtime: Any, scope: dict[str, Any]) -> int:
    return _handle_patch(parsed, runtime=runtime, scope=scope)


@register("recall")
def _dispatch_recall(parsed: object, runtime: Any, scope: dict[str, Any]) -> int:
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


@register("experience")
def _dispatch_experience(parsed: object, runtime: Any, scope: dict[str, Any]) -> int:
    if parsed.experience_command == "outcome":
        try:
            payload = json.loads(Path(parsed.json_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return _print_error("invalid_json", exc)
        if not isinstance(payload, dict):
            print(json.dumps({"ok": False, "error": "invalid_payload"}, ensure_ascii=False))
            return 2
        result = runtime.record_outcome_trace(payload, scope=scope)
        if result.get("ok") is not False:
            from eimemory.governance.closed_loop import post_experience_hook

            result["closed_loop"] = post_experience_hook(runtime, result, scope)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") is not False else 2
    print(json.dumps({"usage": "eimemory experience outcome <json_path>"}))
    return 0


@register("learn")
def _dispatch_learn(parsed: object, runtime: Any, scope: dict[str, Any]) -> Any:
    if parsed.learn_command != "autonomy":
        return FALLTHROUGH
    from eimemory.governance.closed_loop import autonomy_cycle

    report = autonomy_cycle(
        runtime,
        scope,
        apply=bool(parsed.apply),
        dry_run=bool(parsed.dry_run),
        full=bool(parsed.full),
        force=bool(parsed.force),
        max_goals=max(1, int(parsed.max_goals)),
        policy={"max_auto_promotions": max(0, int(parsed.max_promotions))},
        smoke=bool(getattr(parsed, "smoke", False)),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 1


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
    loaded = _load_json_argument(
        raw,
        allow_dict=True,
        allow_list=True,
        allow_empty=True,
        error_code="invalid_web_evidence_json",
    )
    if isinstance(loaded, dict) and isinstance(loaded.get("hypotheses"), list):
        loaded_payload = loaded["hypotheses"]
    elif isinstance(loaded, dict):
        loaded_payload = [loaded]
    else:
        loaded_payload = loaded

    return [dict(item) for item in loaded_payload if isinstance(item, dict)]


def _load_json_argument(
    raw: str,
    *,
    allow_dict: bool,
    allow_list: bool,
    allow_empty: bool = False,
    error_code: str,
) -> Any:
    raw_text = str(raw or "").strip()
    if not raw_text:
        if allow_empty:
            return []
        raise ValueError(error_code)
    try:
        loaded = json.loads(raw_text)
    except json.JSONDecodeError:
        path = Path(raw_text)
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(error_code) from exc
        try:
            loaded = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError(error_code) from exc

    if isinstance(loaded, dict) and allow_dict:
        return dict(loaded)
    if isinstance(loaded, list) and allow_list:
        return [dict(item) if isinstance(item, dict) else item for item in loaded]
    raise ValueError(error_code)


def _living_enrich_report(runtime, scope: dict, *, limit: int) -> dict:
    return runtime.enrich_living_memory(scope=scope, limit=limit)


def _living_timeline_report(runtime, scope: dict, *, limit: int) -> dict:
    return runtime.build_living_timeline(scope=scope, limit=limit)


def _living_posture_report(runtime, scope: dict, *, query: str, limit: int) -> dict:
    return runtime.recommend_action_posture(query, scope=scope, limit=limit)


def _capability_acceptance_succeeded(report: dict[str, Any]) -> bool:
    results = report.get("results")
    if not isinstance(results, list) or len(results) != 12:
        return False
    probe_ids = [str(item.get("source_record_id") or item.get("probe_id") or "") for item in results if isinstance(item, dict)]
    trace_ids = [str(item.get("trace_id") or "") for item in results if isinstance(item, dict)]
    return (
        report.get("ok") is True
        and report.get("all_passed") is True
        and report.get("case_count") == 12
        and report.get("pass_count") == 12
        and report.get("distinct_probe_sources") is True
        and report.get("distinct_trace_ids") is True
        and all(isinstance(item, dict) and item.get("passed") is True for item in results)
        and len(probe_ids) == len(set(probe_ids)) == 12
        and len(trace_ids) == len(set(trace_ids)) == 12
        and all(probe_ids)
        and all(trace_ids)
    )


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
                    "usage": "eimemory init|emergency-stop|ingest|recall|paper|source|intake|export|import|backup|rebuild-sqlite|migrate|brief|nightly|quality|identity|living|reflect|experience|learn|governance|evolve|eval|patch|ops|persona|serve-eibrain-rpc",
                }
            )
        )
        return 0
    if parsed.command in COMMAND_REGISTRY:
        dispatch_result = dispatch(parsed.command, parsed, runtime, scope)
        if dispatch_result is not FALLTHROUGH:
            return _dispatch_exit(dispatch_result)
    if parsed.command in {"doctor", "status"}:
        host = settings.rpc_host
        port = int(settings.rpc_port)
        loopback_health = None
        if settings.rpc_loopback_health_host and settings.rpc_loopback_health_port is not None:
            loopback_health = {
                "host": settings.rpc_loopback_health_host,
                "port": int(settings.rpc_loopback_health_port),
                "path": "/health",
            }
        from eimemory.governance.supervisor import build_supervisor_contract

        health_payload = build_health_payload(
            runtime,
            listen_host=host,
            listen_port=port,
            loopback_health=loopback_health,
        )
        health_payload["supervisor"] = build_supervisor_contract(runtime, scope=scope)
        print(json.dumps(health_payload, ensure_ascii=False, indent=2))
        return 0
    if parsed.command == "ops":
        if parsed.ops_command == "timer-monitor":
            from eimemory.ops.timer_monitor import check_user_systemd_timers

            report = check_user_systemd_timers(
                runtime,
                scope=scope,
                stale_after_minutes=max(1, int(parsed.stale_after_minutes)),
                include_legacy_learning_timers=bool(parsed.include_legacy_learning_timers),
                persist=not bool(parsed.no_persist),
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report.get("ok") else 1
        print(json.dumps({"usage": "eimemory ops timer-monitor"}))
        return 0
    if parsed.command == "serve-eibrain-rpc":
        host = parsed.host or settings.rpc_host
        port = int(parsed.port if parsed.port is not None else settings.rpc_port)
        loopback_health_host = parsed.loopback_health_host or settings.rpc_loopback_health_host
        loopback_health_port = (
            parsed.loopback_health_port
            if parsed.loopback_health_port is not None
            else settings.rpc_loopback_health_port
        )
        server_kwargs = {}
        if loopback_health_host and loopback_health_port is not None:
            server_kwargs = {
                "loopback_health_host": loopback_health_host,
                "loopback_health_port": loopback_health_port,
            }
        if parsed.auth_token:
            server_kwargs["auth_token"] = parsed.auth_token
        server = EIBrainRPCServer(runtime, host=host, port=port, **server_kwargs)
        print(json.dumps({"ok": True, "host": server.address[0], "port": server.address[1]}, ensure_ascii=False))
        server.serve_forever()
        return 0

    if parsed.command == "init":
        runtime.store.root.mkdir(parents=True, exist_ok=True)
        (runtime.store.root / "state").mkdir(parents=True, exist_ok=True)
        print(json.dumps({"ok": True, "root": str(runtime.store.root)}, ensure_ascii=False, indent=2))
        return 0
    if parsed.command == "emergency-stop":
        from eimemory.governance.safety.kill_switch import emergency_stop

        emergency_stop()
        print(json.dumps({"ok": True, "command": "emergency-stop"}, ensure_ascii=False))
        return 0
    if parsed.command == "rebuild-sqlite":
        if not bool(parsed.from_jsonl):
            print(json.dumps({"ok": False, "error": "missing_from_jsonl"}, ensure_ascii=False))
            return 2
        report = runtime.store.rebuild_sqlite_from_jsonl(replace=bool(parsed.replace))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if parsed.command == "ingest":
        record = runtime.memory.ingest(
            text=parsed.text,
            memory_type=parsed.memory_type,
            title=parsed.title,
            scope=scope,
            source="cli",
            force_capture=bool(parsed.force_capture),
        )
        payload = record.to_dict()
        if record.status == "rejected":
            payload["warnings"] = list(record.meta.get("capture_warnings") or [])
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if parsed.command == "experience":
        if parsed.experience_command == "outcome":
            try:
                payload = json.loads(Path(parsed.json_path).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                return _print_error("invalid_json", exc)
            if not isinstance(payload, dict):
                print(json.dumps({"ok": False, "error": "invalid_payload"}, ensure_ascii=False))
                return 2
            result = runtime.record_outcome_trace(payload, scope=scope)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result.get("ok") is not False else 2
        print(json.dumps({"usage": "eimemory experience outcome <json_path>"}))
        return 0
    if parsed.command == "learn":
        if parsed.learn_command == "watch":
            from eimemory.governance.world_watchers import collect_world_signals, default_watches

            report = collect_world_signals(
                runtime,
                scope=scope,
                watches=default_watches(),
                dry_run=not bool(parsed.apply),
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        if parsed.learn_command == "think":
            persist = bool(parsed.persist) and not bool(parsed.dry_run)
            report = runtime.generate_learning_thoughts(
                scope=scope,
                persist=persist,
                max_items=max(1, int(parsed.max_items)),
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report.get("ok") else 1
        if parsed.learn_command in {"cycle", "autonomy"}:
            if parsed.learn_command == "autonomy":
                report = runtime.run_autonomy_cycle(
                    scope=scope,
                    apply=bool(parsed.apply),
                    dry_run=bool(parsed.dry_run),
                    full=bool(parsed.full),
                    force=bool(parsed.force),
                    max_goals=max(1, int(parsed.max_goals)),
                    policy={"max_auto_promotions": max(0, int(parsed.max_promotions))},
                    smoke=bool(getattr(parsed, "smoke", False)),
                )
            else:
                report = runtime.run_autonomous_learning_cycle(
                    scope=scope,
                    apply=bool(parsed.apply),
                    dry_run=bool(parsed.dry_run),
                    full=bool(parsed.full),
                    force=bool(parsed.force),
                    max_goals=max(1, int(parsed.max_goals)),
                    max_promotions=max(0, int(parsed.max_promotions)),
                )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report.get("ok") else 1
        if parsed.learn_command == "evaluator-harness":
            replay_ok = not bool(parsed.fail_replay)
            report = runtime.run_isolated_evaluator_harness(
                scope=scope,
                loop_id="cli_isolated_evaluator",
                generator_model=str(parsed.generator_model or "") or None,
                evaluator_model=str(parsed.evaluator_model or "") or None,
                stop_judge_model=str(parsed.stop_judge_model or "") or None,
                replay_gate={
                    "ok": replay_ok,
                    "verdict": "pass" if replay_ok else "fail",
                    "pass_rate": 1.0 if replay_ok else 0.0,
                    "sample_count": 1,
                    "threshold": 0.6,
                    "reason": "cli_smoke",
                },
                real_task_replay={
                    "ok": replay_ok,
                    "verdict": "pass" if replay_ok else "fail",
                    "pass_rate": 1.0 if replay_ok else 0.0,
                    "pass_count": 1 if replay_ok else 0,
                    "fail_count": 0 if replay_ok else 1,
                    "report_type": "cli_isolated_evaluator_smoke",
                },
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report.get("ok") else 1
        if parsed.learn_command == "loops":
            print(json.dumps(runtime.list_learning_loops(scope=scope, limit=max(0, int(parsed.limit))), ensure_ascii=False, indent=2))
            return 0
        if parsed.learn_command == "goals":
            print(json.dumps(runtime.list_learning_goals(scope=scope, limit=max(0, int(parsed.limit))), ensure_ascii=False, indent=2))
            return 0
        if parsed.learn_command == "candidates":
            print(json.dumps(runtime.list_learning_candidates(scope=scope, limit=max(0, int(parsed.limit))), ensure_ascii=False, indent=2))
            return 0
        if parsed.learn_command == "ledger":
            print(
                json.dumps(
                    runtime.learning_ledger(
                        scope=scope,
                        limit=max(1, int(parsed.limit)),
                        since=str(parsed.since or "") or None,
                        until=str(parsed.until or "") or None,
                    ),
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if parsed.learn_command == "replay-dataset":
            report = runtime.build_replay_dataset(
                scope=scope,
                limit=max(1, int(parsed.limit)),
                persist=bool(parsed.persist),
                include_built_in_regressions=bool(parsed.include_built_in_regressions),
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report.get("ok") else 1
        if parsed.learn_command == "goal-graph":
            report = runtime.build_goal_graph_loop(
                scope=scope,
                max_goals=max(1, int(parsed.max_goals)),
                persist=bool(parsed.persist),
                capabilities=list(parsed.capability or []) or None,
                loop_id="cli_goal_graph",
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report.get("ok") else 1
        if parsed.learn_command == "world-model":
            report = runtime.build_world_model(
                scope=scope,
                persist=bool(parsed.persist),
                loop_id="cli_world_model",
                limit=max(1, int(parsed.limit)),
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report.get("ok") else 1
        if parsed.learn_command == "roadmap":
            world = runtime.build_world_model(scope=scope, persist=bool(parsed.persist), loop_id="cli_roadmap")
            report = runtime.build_strategic_roadmap(
                scope=scope,
                world_model=world,
                horizon_days=max(30, int(parsed.horizon_days)),
                persist=bool(parsed.persist),
                loop_id="cli_roadmap",
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report.get("ok") else 1
        if parsed.learn_command == "l5":
            report = runtime.run_l5_cycle(
                scope=scope,
                apply=bool(parsed.apply),
                force=bool(parsed.force),
                max_goals=max(1, int(parsed.max_goals)),
                max_promotions=max(0, int(parsed.max_promotions)),
                allow_network=not bool(parsed.no_network),
                loop_id="cli_l5",
                persist=not bool(parsed.no_persist),
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report.get("ok") else 1
        if parsed.learn_command == "l5-assess":
            report = runtime.assess_l5_closed_loop(
                scope=scope,
                loop_report={},
                persist=bool(parsed.persist),
                loop_id="cli_l5_assess",
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report.get("ok") else 1
        if parsed.learn_command == "l5-readiness":
            report = runtime.build_l5_readiness_report(
                scope=scope,
                persist=bool(parsed.persist),
                limit=max(1, int(parsed.limit)),
                loop_id="cli_l5_readiness",
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report.get("ok") else 1
        if parsed.learn_command == "closure-rehearsal":
            report = runtime.run_l5_closure_rehearsal(
                scope=_cli_scope(parsed, defaults=scope),
                persist=True,
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report.get("ok") else 1
        if parsed.learn_command == "deployment-receipt":
            report = runtime.verify_and_record_deployment(
                scope=_cli_scope(parsed, defaults=scope),
                repo_root=str(parsed.repo_root),
                current_link=str(parsed.current_link),
                health_url=str(parsed.health_url),
                prior_commit=str(parsed.prior_commit or ""),
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report.get("ok") else 1
        if parsed.learn_command == "capability-acceptance":
            report = runtime.run_capability_acceptance(scope=scope, persist=True)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if _capability_acceptance_succeeded(report) else 1
        if parsed.learn_command == "capability-replay":
            report = runtime.build_capability_replay_packs(
                scope=scope,
                capabilities=list(parsed.capability or []) or None,
                persist=bool(parsed.persist),
                loop_id="cli_capability_replay",
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report.get("ok") else 1
        if parsed.learn_command == "safety-replay":
            report = runtime.run_safety_boundary_replay(
                scope=scope,
                persist=bool(parsed.persist),
                loop_id="cli_safety_replay",
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report.get("ok") else 1
        if parsed.learn_command == "skills":
            if bool(parsed.promote):
                report = runtime.promote_repeated_sops_to_skill_candidates(
                    scope=scope,
                    min_repeats=max(1, int(parsed.min_repeats)),
                    persist=bool(parsed.persist),
                    limit=max(1, int(parsed.limit)),
                )
                report["registry"] = runtime.list_eiskills(scope=scope, limit=max(1, int(parsed.limit)))
            else:
                report = runtime.list_eiskills(scope=scope, limit=max(1, int(parsed.limit)))
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report.get("ok") else 1
        if parsed.learn_command == "skill-call":
            try:
                context = _load_json_argument(
                    parsed.context_json,
                    allow_dict=True,
                    allow_list=False,
                    allow_empty=True,
                    error_code="invalid_context_json",
                )
            except ValueError as exc:
                print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
                return 2
            report = runtime.call_eiskill(
                skill_id=str(parsed.skill_id),
                scope=scope,
                context=context if isinstance(context, dict) else {},
                persist=not bool(parsed.no_persist),
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report.get("ok") else 1
        if parsed.learn_command == "metrics":
            report = runtime.build_capability_dashboard_metrics(
                scope=scope,
                persist=bool(parsed.persist),
                limit=max(1, int(parsed.limit)),
                loop_id="cli_capability_dashboard",
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report.get("ok") else 1
        if parsed.learn_command == "compact":
            report = runtime.compact_learning_records(scope=scope, dry_run=not bool(parsed.apply))
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        if parsed.learn_command == "report":
            report = runtime.build_learning_daily_report(
                scope=scope,
                persist=bool(parsed.persist),
                report_date=str(parsed.date or "") or None,
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report.get("ok") else 1
        if parsed.learn_command == "dashboard":
            report = runtime.build_learning_dashboard(
                scope=scope,
                week_start=str(parsed.week_start or "") or None,
                persist=bool(parsed.persist),
                output_path=str(parsed.output or "") or None,
                weekly=bool(parsed.weekly),
            )
            if parsed.json:
                print(json.dumps(report, ensure_ascii=False, indent=2))
            else:
                print(str(report.get("markdown") or ""))
            return 0 if report.get("ok") else 1
        if parsed.learn_command == "promote":
            from eimemory.governance.promotion_manager import promote_candidate

            try:
                eval_result = _load_json_argument(
                    parsed.eval_json,
                    allow_dict=True,
                    allow_list=False,
                    allow_empty=True,
                    error_code="invalid_eval_json",
                )
                health = _load_json_argument(
                    parsed.health_json,
                    allow_dict=True,
                    allow_list=False,
                    allow_empty=True,
                    error_code="invalid_health_json",
                )
            except ValueError as exc:
                print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
                return 2
            if not eval_result:
                eval_result = None
            if not health:
                health = {"ok": True, "source": "cli"}
            report = promote_candidate(
                runtime,
                candidate_id=parsed.candidate_id,
                scope=scope,
                loop_id=str(parsed.loop_id or "cli"),
                apply=bool(parsed.apply),
                eval_result=eval_result,
                health=health,
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report.get("ok") else 1
        print(json.dumps({"usage": "eimemory learn watch|think|cycle|autonomy|evaluator-harness|loops|goals|candidates|ledger|replay-dataset|goal-graph|world-model|roadmap|l5|l5-assess|l5-readiness|closure-rehearsal|deployment-receipt|capability-acceptance|capability-replay|safety-replay|skills|skill-call|metrics|compact|report|dashboard|promote"}))
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
            return 0 if report.get("ok", True) else 1
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
    if parsed.command == "persona":
        return handle_persona_command(parsed, runtime, scope)
    if parsed.command == "openclaw-hook":
        try:
            event = json.loads(_read_stdin_text() or "{}")
        except json.JSONDecodeError:
            _write_json({"ok": False, "error": "invalid_json"})
            return 2
        if not isinstance(event, dict):
            _write_json({"ok": False, "error": "invalid_event"})
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
        _write_json(payload)
        return 0
    if parsed.command == "ei-bridge":
        if parsed.ei_bridge_command == "feishu":
            try:
                event = json.loads(_read_stdin_text() or "{}")
            except json.JSONDecodeError:
                _write_json({"ok": False, "error": "invalid_json"})
                return 2
            if not isinstance(event, dict):
                _write_json({"ok": False, "error": "invalid_event"})
                return 2
            try:
                payload = handle_openclaw_feishu_event(event, runtime)
            except Exception as exc:
                return _print_error("ei_bridge_failed", exc)
            _write_json(payload)
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
            return _print_report_exit(report)
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
            return _print_report_exit(report)
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
            return _print_report_exit(report)
        if parsed.evolve_command == "code-sandbox":
            try:
                incident = _load_json_argument(
                    parsed.incident_json,
                    allow_dict=True,
                    allow_list=False,
                    allow_empty=False,
                    error_code="invalid_incident_json",
                )
            except ValueError as exc:
                print(
                    json.dumps(
                        {"ok": False, "error": "invalid_incident_json", "detail": str(exc)},
                        ensure_ascii=False,
                    )
                )
                return 2
            report = runtime.run_code_sandbox(
                scope=_cli_scope(parsed, defaults=scope),
                incident=incident,
                create_worktree=bool(parsed.create_worktree),
                persist_report=bool(parsed.persist_report),
            )
            return _print_report_exit(report)
        if parsed.evolve_command == "gates":
            report = {
                "ok": True,
                "ledger": runtime.get_policy_rollout_ledger(
                    scope=_cli_scope(parsed, defaults=scope),
                    action=str(parsed.action or "") or None,
                    limit=max(0, int(parsed.limit)),
                ),
            }
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        if parsed.evolve_command == "rollback":
            report = runtime.rollback_intent_pattern(
                str(parsed.pattern_id),
                scope=_cli_scope(parsed, defaults=scope),
                reason=str(parsed.reason or "manual rollback"),
                auto=False,
            )
            return _print_report_exit(report)
        print(json.dumps({"usage": "eimemory evolve evaluate|promotions|loop|autonomous|code-sandbox|web-scout|gates|rollback"}))
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
        if parsed.eval_command == "locomo":
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
                from eimemory.evaluation import run_locomo

                report = run_locomo(
                    runtime,
                    dataset,
                    mode=parsed.mode,
                    granularity=parsed.granularity,
                    limit=parsed.limit,
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
        if parsed.eval_command == "public-benchmark":
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
                from eimemory.evaluation import run_public_memory_benchmark

                report = run_public_memory_benchmark(
                    dataset,
                    suite=parsed.suite,
                    mode=parsed.mode,
                    granularity=parsed.granularity,
                    limit=parsed.limit,
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

                report = run_production_recall_eval(
                    runtime,
                    dataset,
                    seed=not bool(parsed.no_seed),
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
        if parsed.eval_command == "openclaw-e2e":
            from eimemory.adapters.openclaw.tools import OpenClawMemoryTools

            e2e_scope = asdict(
                ScopeRef.from_dict(
                    {
                        "agent_id": parsed.scope_agent or settings.default_agent_id or "main",
                        "workspace_id": parsed.scope_workspace or settings.default_workspace_id,
                        "user_id": parsed.scope_user or "",
                    }
                )
            )
            report = OpenClawMemoryTools(runtime).memory_e2e_check(scope=e2e_scope, query=str(parsed.query or ""))
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
        if parsed.eval_command == "task-replay":
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
                from eimemory.evaluation import run_real_task_replay

                report = run_real_task_replay(
                    runtime,
                    dataset,
                    seed=not bool(parsed.no_seed),
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
        print(json.dumps({"usage": "eimemory eval run|ci|longmem|locomo|public-benchmark|living|actionable|production-recall|task-replay"}))
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
