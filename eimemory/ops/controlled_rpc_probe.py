"""Exercise the authenticated live recall transport using authorized old records."""
from dataclasses import asdict
from types import SimpleNamespace
import re

from eimemory.adapters.runtime.http_client import AgentRuntimeRPCClient
from eimemory.core.rpc_probe_auth import rpc_probe_token
from eimemory.ei_bridge.protocol import EIMEMORY_RPC_CONTRACT_VERSION
from eimemory.evaluation import controlled_production_acceptance as acceptance
from eimemory.evaluation import controlled_memory_benchmark as benchmark
from eimemory.models.records import RecordEnvelope
from eimemory.ops.controlled_stability import collect_controlled_endpoint_identity

SCHEMA = "controlled_production_rpc_probe.v1"


def _endpoint_matches(value, commit, version):
    return (isinstance(value, dict) and value.get("schema") == "controlled_rpc_release_identity.v1"
        and value.get("ok") is True and value.get("expected_commit") == commit
        and value.get("version") == version and value.get("identity_only") is True
        and value.get("retrieval_health_certified") is False
        and isinstance(value.get("package_tree_digest"), str)
        and re.fullmatch("[0-9a-f]{64}", value["package_tree_digest"]) is not None)


def run_controlled_rpc_probe(runtime, *, scope, expected_commit):
    """One known-item request per protected scope; no labels or natural receipt.

    This deliberately precedes stability observation. A cold RPC process must
    serve a real request, rather than having its cached health flags waived.
    """
    report = {"schema": SCHEMA, "ok": False, "expected_commit": expected_commit,
        "maintenance_only": True, "natural_samples_created": 0,
        "certifies_natural_queries": False, "endpoint_release_verified": False,
        "rpc_contract_version": EIMEMORY_RPC_CONTRACT_VERSION, "cases": []}
    try:
        if not isinstance(expected_commit, str) or not re.fullmatch("[0-9a-f]{40}", expected_commit):
            raise ValueError("controlled_rpc_commit_invalid")
        owner = acceptance._scope(scope)
        policy = acceptance.load_controlled_policy(runtime, owner)
        if policy is None:
            raise ValueError("controlled_rpc_policy_missing")
        release, receipt = acceptance._release(runtime, owner)
        if release["release_commit"] != expected_commit:
            raise ValueError("controlled_rpc_release_mismatch")
        token = rpc_probe_token()
        if not token:
            raise ValueError("controlled_rpc_auth_unavailable")
        endpoint_before = collect_controlled_endpoint_identity(expected_commit=expected_commit)
        report["endpoint_identity_before"] = endpoint_before
        if not _endpoint_matches(endpoint_before, expected_commit, release["release_version"]):
            raise ValueError("controlled_rpc_endpoint_identity_invalid")
        client = AgentRuntimeRPCClient(base_url="http://127.0.0.1:8091/rpc", auth_token=token,
            timeout_seconds=12, max_response_bytes=4 * 1024 * 1024)
        for entry in policy["agents"]:
            exact = acceptance._scope(entry["scope"])
            sources = tuple(sorted(entry["source_ids"]))
            row = {"agent_id": exact.agent_id, "scope_digest": acceptance.digest(asdict(exact)),
                   "source_ids": list(sources), "passed": False}
            phase = "build_target"
            try:
                dataset = benchmark.build_controlled_memory_benchmark(runtime, scope=exact,
                    source_ids=sources, limit=1, stable_before=policy["stable_before"])
                if len(dataset.get("cases", [])) != 1:
                    raise ValueError("controlled_rpc_old_target_missing")
                case = dataset["cases"][0]
                limit = case["limit"]
                if type(limit) is not int or not 1 <= limit <= 50:
                    raise ValueError("controlled_rpc_limit_invalid")
                binding = case["source_binding"]
                before = acceptance._physical(runtime, binding, exact, sources, policy["stable_before"])
                context = {**case["task_context"], "acceptance_generated": True,
                           "task_type": "maintenance.controlled_production_transport"}
                phase = "rpc_call"
                response = client.call("memory.recall", {"query": case["query"],
                    "scope": {**asdict(exact), "preserve_scope": True},
                    "task_context": context, "limit": limit})
                result = response.get("result")
                if (response.get("ok") is not True or not isinstance(result, dict)
                        or response.get("contract_version") != EIMEMORY_RPC_CONTRACT_VERSION):
                    raise ValueError("controlled_rpc_result_invalid")
                returned = []
                main_items = []
                phase = "hydrate_returned"
                for section in ("items", "rules", "reflections"):
                    values = result.get(section)
                    if not isinstance(values, list) or len(values) > (limit if section == "items" else 50):
                        raise ValueError("controlled_rpc_result_invalid")
                    records = [RecordEnvelope.from_dict(value) for value in values]
                    returned += records
                    if section == "items":
                        main_items = records
                if any(record.scope != exact or record.source_id not in sources for record in returned):
                    raise ValueError("controlled_rpc_boundary_invalid")
                phase = "validate_returned"
                for record in returned:
                    acceptance._physical(runtime, benchmark._binding(record), exact, sources,
                                         policy["stable_before"], require_old=False)
                state, complete = acceptance._complete(SimpleNamespace(explanation=result.get("explanation", {})))
                after = acceptance._physical(runtime, binding, exact, sources, policy["stable_before"])
                target_present = any(benchmark._binding(record) == binding for record in main_items)
                row.update(passed=complete and before == after and target_present,
                    collection_complete=state["collection_complete"] is True,
                    no_fallback=state["fallback"] is False,
                    budgets_clear=type(state["source_budget_exhausted"]) is int
                        and state["source_budget_exhausted"] == 0
                        and type(state["recall_budget_exhausted"]) is int
                        and state["recall_budget_exhausted"] == 0,
                    target_present=target_present, exact_boundary_verified=True,
                    target_version_unchanged=before == after, target_in_main_items=target_present,
                    rpc_contract_verified=True, target_binding_digest=acceptance.digest(binding),
                    target_version_digest=acceptance.digest(before), context_digest=acceptance.digest(context),
                    query_digest=acceptance.digest(case["query"]))
            except (ValueError, RuntimeError, TypeError, KeyError, AttributeError, OSError) as exc:
                code = str(exc)
                row.update(error=code if re.fullmatch(r"controlled_(?:rpc|acceptance)_[a-z_]{1,80}", code)
                           else "controlled_rpc_probe_failed", error_type=type(exc).__name__, failed_phase=phase)
            report["cases"].append(row)
        endpoint_after = collect_controlled_endpoint_identity(expected_commit=expected_commit)
        report["endpoint_identity_after"] = endpoint_after
        if not _endpoint_matches(endpoint_after, expected_commit, release["release_version"]) or endpoint_before != endpoint_after:
            raise ValueError("controlled_rpc_endpoint_identity_changed")
        report["endpoint_release_verified"] = True
        if (acceptance.load_controlled_policy(runtime, owner) != policy
                or acceptance._release(runtime, owner) != (release, receipt)):
            raise ValueError("controlled_rpc_authority_changed")
        report["ok"] = bool(report["cases"] and all(row["passed"] for row in report["cases"]))
        report["policy_digest"] = acceptance.digest(policy)
        report["release_identity"] = release
    except (ValueError, RuntimeError, TypeError, KeyError, AttributeError, OSError) as exc:
        report.update(error="controlled_rpc_probe_unavailable", error_type=type(exc).__name__)
    report["sample_count"] = len(report["cases"])
    report["pass_count"] = sum(bool(row.get("passed")) for row in report["cases"])
    return report
