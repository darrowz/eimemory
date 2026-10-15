"""Natural outcome coverage and reversible retirement of duplicate synthetic rules.

``outcome_stats`` is read-only (SQLite ``mode=ro``). It reports, per gateway,
how many natural proactive decisions carry host-observed outcome signals
(tool chain, task success, correction, quick re-ask, explicit rating) and how
many carry a verified terminal outcome. Signals are labels only; they are not
verified task outcomes and are never counted as such.

``retire-synthetic-rules`` previews by default. ``--apply`` routes the exact
duplicates (newest identical rule stays active) through the existing reviewed
manifest path of ``storage quarantine-test-rules``: status ``deprecated`` with
the original envelope kept for restore, plus archived audit receipts. The
manifest is saved so ``--revert BATCH`` restores exactly that batch. Nothing
is deleted.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Any

from eimemory.knowledge.evidence_contracts import versioned_record_ref
from eimemory.models.records import ScopeRef

SYNTHETIC_RULE_SOURCE = "eimemory.correction_replay"
SYNTHETIC_RULE_TITLE_PREFIX = "Ground truth behavior: "
MANIFEST_DIR = Path("state") / "synthetic-rule-retirement"


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


def _origin(row: sqlite3.Row) -> str:
    session = str(row["session_id"] or "")
    if int(row["acceptance_generated"] or 0) == 1:
        return "acceptance"
    if session.startswith("hermes-release-"):
        return "deploy_replay"
    if session.startswith("cron_"):
        return "scheduled"
    return "natural"


def outcome_stats(db_path: Path, *, since: str = "", channel: str = "") -> dict[str, Any]:
    conn = _connect_ro(db_path)
    try:
        tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(proactive_decisions)")}
        accept_sql = "COALESCE(acceptance_generated,0)" if "acceptance_generated" in columns else "0"
        where, params = ["1=1"], []
        if since:
            where.append("created_at >= ?")
            params.append(since)
        if channel:
            where.append("channel = ?")
            params.append(channel)
        decisions = conn.execute(
            f"SELECT decision_id, channel, agent_id, session_id, {accept_sql} AS acceptance_generated, "
            "outcome_verified, outcome_success FROM proactive_decisions WHERE " + " AND ".join(where),
            params,
        ).fetchall()
        signals_by_decision: dict[str, list[tuple[str, dict]]] = {}
        if "proactive_effect_signals" in tables:
            for row in conn.execute("SELECT decision_id, phase, payload_json FROM proactive_effect_signals"):
                try:
                    labels = dict(json.loads(row["payload_json"]).get("labels") or {})
                except (TypeError, ValueError):
                    labels = {}
                signals_by_decision.setdefault(str(row["decision_id"]), []).append((str(row["phase"]), labels))
        origins: Counter[str] = Counter()
        gateways: dict[str, dict[str, Any]] = {}
        totals: Counter[str] = Counter()
        for row in decisions:
            origin = _origin(row)
            origins[origin] += 1
            if origin != "natural":
                continue
            bucket = gateways.setdefault(f"{row['channel']}:{row['agent_id']}", {
                "decisions": 0, "with_any_signal": 0, "with_turn_signal": 0, "with_followup_signal": 0,
                "with_rating": 0, "with_verified_outcome": 0, "labels": Counter()})
            bucket["decisions"] += 1
            phases = signals_by_decision.get(str(row["decision_id"]), [])
            seen = {phase for phase, _ in phases}
            bucket["with_any_signal"] += int(bool(phases))
            bucket["with_turn_signal"] += int("turn_completed" in seen)
            bucket["with_followup_signal"] += int("next_user" in seen)
            bucket["with_rating"] += int("explicit_rating" in seen)
            bucket["with_verified_outcome"] += int(bool(row["outcome_verified"]))
            for _phase, labels in phases:
                for key, value in labels.items():
                    if key != "latency_ms" and isinstance(value, str):
                        bucket["labels"][f"{key}={value}"] += 1
        for bucket in gateways.values():
            total = bucket["decisions"]
            bucket["signal_coverage"] = round(bucket["with_any_signal"] / total, 4) if total else 0.0
            bucket["verified_outcome_coverage"] = round(bucket["with_verified_outcome"] / total, 4) if total else 0.0
            for key in ("decisions", "with_any_signal", "with_turn_signal", "with_followup_signal",
                        "with_rating", "with_verified_outcome"):
                totals[key] += bucket[key]
            totals_labels = bucket["labels"]
            bucket["labels"] = dict(sorted(totals_labels.items()))
        natural = totals["decisions"]
        raw_query_rows = 0
        if "query_text" in columns:
            raw_query_rows = int(conn.execute(
                "SELECT COUNT(*) FROM proactive_decisions WHERE COALESCE(query_text,'')<>''").fetchone()[0])
        return {
            "ok": True,
            "report_type": "natural_outcome_stats",
            "since": since,
            "channel": channel,
            "decisions_by_origin": dict(origins),
            "natural": {
                **{key: int(totals[key]) for key in ("decisions", "with_any_signal", "with_turn_signal",
                                                     "with_followup_signal", "with_rating", "with_verified_outcome")},
                "signal_coverage": round(totals["with_any_signal"] / natural, 4) if natural else 0.0,
                "verified_outcome_coverage": round(totals["with_verified_outcome"] / natural, 4) if natural else 0.0,
            },
            "gateways": dict(sorted(gateways.items())),
            "semantics": {
                "signals": "host-observed labels (recall.effect_signal.v1); not verified task outcomes",
                "verified_outcome": "proactive_decisions.outcome_verified (host-attested terminal outcome only)",
            },
            "raw_text_check": {"signals_are_labels_only": True,
                               "proactive_decisions_query_text_rows": raw_query_rows,
                               "ok": raw_query_rows == 0},
        }
    finally:
        conn.close()


def _duplicate_groups(store: Any) -> list[dict[str, Any]]:
    rows = store.sqlite.execute(
        "SELECT record_id, tenant_id, agent_id, workspace_id, user_id, source_id, title, summary, detail, created_at "
        "FROM records WHERE kind='rule' AND source=? AND status='active' AND title LIKE ? "
        "ORDER BY created_at DESC, record_id DESC",
        (SYNTHETIC_RULE_SOURCE, SYNTHETIC_RULE_TITLE_PREFIX + "%"),
    ).fetchall()
    groups: dict[tuple[str, ...], list[Any]] = {}
    for row in rows:
        key = tuple(str(row[i] or "") for i in (1, 2, 3, 4, 5, 6, 7, 8))
        groups.setdefault(key, []).append(row)
    result = []
    for key, members in groups.items():
        if len(members) < 2:
            continue
        keep, *retire = members  # newest stays active: the rehearsal reuses it
        scope = ScopeRef(tenant_id=key[0], agent_id=key[1], workspace_id=key[2], user_id=key[3])
        refs = []
        for row in retire:
            record = store.get_by_id(str(row[0]), scope=scope)
            if record is not None:
                refs.append({"record_id": record.record_id, "version_digest": versioned_record_ref(record)["version_digest"]})
        result.append({"scope": scope, "source_id": key[4], "title": key[5],
                       "identical_active": len(members), "keep_record_id": str(keep[0]), "retire": refs})
    return result


def retirement_plan(store: Any) -> dict[str, Any]:
    with store.locked():
        groups = _duplicate_groups(store)
    retire_ids = sorted(item["record_id"] for group in groups for item in group["retire"])
    return {
        "ok": True,
        "report_type": "synthetic_rule_retirement_plan",
        "groups": [{"scope": asdict(g["scope"]), "source_id": g["source_id"], "title": g["title"],
                    "identical_active": g["identical_active"], "keep_record_id": g["keep_record_id"],
                    "retire_count": len(g["retire"])} for g in groups],
        "retire_count": len(retire_ids),
        "batch_id": ("srr-" + sha256("\n".join(retire_ids).encode()).hexdigest()[:12]) if retire_ids else "",
        "_groups": groups,
    }


def _manifest(group: dict[str, Any]) -> dict[str, Any]:
    from eimemory.storage.test_rule_quarantine import SCHEMA
    return {"schema": SCHEMA, "scope": asdict(group["scope"]), "source_id": group["source_id"], "rules": group["retire"]}


def apply_retirement(store: Any, root: Path, *, expect_count: int | None = None) -> dict[str, Any]:
    from eimemory.storage.test_rule_quarantine import quarantine_test_rules

    plan = retirement_plan(store)
    groups = plan.pop("_groups")
    if expect_count is not None and plan["retire_count"] != expect_count:
        return {**plan, "ok": False, "applied": 0, "error": "retire_count_mismatch", "expected": expect_count}
    if not plan["retire_count"]:
        return {**plan, "applied": 0}
    manifests = [_manifest(group) for group in groups if group["retire"]]
    directory = Path(root) / MANIFEST_DIR
    directory.mkdir(parents=True, exist_ok=True)
    manifest_path = directory / f"{plan['batch_id']}.json"
    manifest_path.write_text(json.dumps({"batch_id": plan["batch_id"], "manifests": manifests}, ensure_ascii=False, indent=2))
    applied = 0
    for manifest in manifests:
        report = quarantine_test_rules(store, scope=manifest["scope"], source_id=manifest["source_id"],
                                       apply=True, manifest=manifest)
        applied += int(report.get("changed") or 0)
    return {**plan, "applied": applied, "manifest_path": str(manifest_path),
            "revert": f"eimemory outcomes retire-synthetic-rules --revert {plan['batch_id']}"}


def revert_retirement(store: Any, root: Path, batch_id: str) -> dict[str, Any]:
    from eimemory.storage.test_rule_quarantine import quarantine_test_rules

    if not batch_id.startswith("srr-") or "/" in batch_id:
        return {"ok": False, "error": "batch_id_invalid"}
    manifest_path = Path(root) / MANIFEST_DIR / f"{batch_id}.json"
    if not manifest_path.is_file():
        return {"ok": False, "error": "batch_manifest_missing", "batch_id": batch_id}
    saved = json.loads(manifest_path.read_text())
    reverted = 0
    for manifest in saved["manifests"]:
        current = {"schema": manifest["schema"], "scope": manifest["scope"], "source_id": manifest["source_id"],
                   "rules": []}
        scope = ScopeRef.from_dict(manifest["scope"])
        for item in manifest["rules"]:
            record = store.get_by_id(item["record_id"], scope=scope)
            if record is not None:
                current["rules"].append({"record_id": record.record_id,
                                         "version_digest": versioned_record_ref(record)["version_digest"]})
        report = quarantine_test_rules(store, scope=current["scope"], source_id=current["source_id"],
                                       apply=True, revert=True, manifest=current)
        reverted += int(report.get("changed") or 0)
    return {"ok": True, "report_type": "synthetic_rule_retirement_revert", "batch_id": batch_id,
            "reverted": reverted}
