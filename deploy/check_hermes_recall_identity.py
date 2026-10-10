"""Controlled read-only provider probe with explicit expected routing.

Run under the corresponding gateway's actual Python and environment. This is
an independent process; it never certifies the identity of a running session.
Only counts and routing are returned, never query text or recalled content.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import uuid


def probe_hermes_recall_identity(provider, *, scope, source_ids, queries):
    if not queries or len(queries) > 8 or any(not isinstance(query, str) or not query.strip() or len(query) > 4000 for query in queries):
        raise ValueError("one to eight nonempty queries of at most 4000 characters are required")
    provider.initialize(
        "hermes-identity-probe-" + uuid.uuid4().hex,
        agent_context="subagent", agent_identity=scope["agent_id"],
        agent_workspace=scope["workspace_id"], user_id=scope["user_id"],
        expected_scope=scope, expected_source_ids=source_ids,
    )
    checks = []
    for index, query in enumerate(queries):
        response = json.loads(provider.handle_tool_call("eimemory_recall", {"query": query, "limit": 8}))
        result = response.get("result")
        if response.get("ok") is not True or not isinstance(result, dict) or result.get("ok") is not True:
            error = response.get("error")
            if error not in {"hermes_response_scope_mismatch", "hermes_expected_sources_mismatch", "adapter_unavailable"}:
                error = "recall_rpc_failed"
            checks.append({"query_index": index, "ok": False, "error": error})
            continue
        bundle = result.get("bundle")
        if not isinstance(bundle, dict) or not isinstance(bundle.get("items"), list):
            checks.append({"query_index": index, "ok": False, "error": "recall_bundle_invalid"})
            continue
        partitions = {key: bundle.get(key, []) for key in ("items", "persona", "rules", "reflections")}
        if any(not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows) for rows in partitions.values()):
            checks.append({"query_index": index, "ok": False, "error": "recall_bundle_invalid"})
            continue
        # Preference recall may be entirely in the persona partition. Counting
        # only items incorrectly calls a successfully rendered loadout empty.
        total = sum(len(rows) for rows in partitions.values())
        checks.append({"query_index": index, "ok": True, "item_count": len(partitions["items"]),
                       "persona_count": len(partitions["persona"]), "rule_count": len(partitions["rules"]),
                       "reflection_count": len(partitions["reflections"]), "total_record_count": total,
                       "recall_has_evidence": total > 0})
    return {
        "report_type": "hermes_recall_identity_probe", "ok": all(check["ok"] for check in checks),
        "identity": provider.identity_diagnostics(), "checks": checks,
        "recall_has_evidence": all(check.get("recall_has_evidence", False) for check in checks),
        "live_session_verified": False, "certifies_recall_quality": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", default="default")
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--source-id", action="append", required=True)
    parser.add_argument("--query", action="append", required=True)
    args = parser.parse_args(argv)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from eimemory.adapters.hermes.provider_core import HermesMemoryProviderCore
    from eimemory.version import __version__
    provider = HermesMemoryProviderCore()
    scope = {key: getattr(args, key) for key in ("tenant_id", "agent_id", "workspace_id", "user_id")}
    try:
        report = probe_hermes_recall_identity(provider, scope=scope, source_ids=args.source_id, queries=args.query)
        report["imported_version"] = __version__
    except ValueError as exc:
        report = {"ok": False, "error": str(exc), "live_session_verified": False,
                  "certifies_recall_quality": False, "imported_version": __version__}
    finally:
        provider.shutdown()
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["ok"] and report["recall_has_evidence"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
