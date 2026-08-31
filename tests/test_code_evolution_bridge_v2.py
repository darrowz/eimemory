from __future__ import annotations

from hashlib import sha256
import inspect
from pathlib import Path

from eimemory.adapters.hermes import code_implementation as provider_module
from eimemory.governance.code_evolution_bridge import propose_code_patch_v2
from eimemory.governance.code_evolution_test_plans import protected_test_plan_digest
from eimemory.adapters.hermes.code_implementation import build_attestation, canonical_json


SCOPE = {"tenant_id": "tenant", "agent_id": "agent", "workspace_id": "workspace", "user_id": "user"}


class _TestProvider:
    timeout_seconds = 15.0

    def propose_patch_v2(self, request):
        source = request["allowed_files"][0]
        response = {
            "schema": "code_implementation_response.v2",
            "request_id": request["request_id"],
            "request_digest": request["request_digest"],
            "file_updates": [{"path": source["path"], "prior_sha256": source["sha256"], "content": source["content"]}],
            "rationale": "bounded proposal",
            "assumptions": [],
        }
        return {
            "ok": True,
            "operation": "propose_patch_v2",
            "attestation": build_attestation(
                request,
                response,
                completed_at="2026-08-23T00:00:00Z",
                nonce=request["nonce"],
            ),
            "response": response,
        }


def test_v2_bridge_is_proposal_only_and_requires_attested_resolver_provider(monkeypatch) -> None:
    monkeypatch.delenv("EIMEMORY_L5_V3_PROFILE", raising=False)
    path = Path("eimemory/governance/l5_reader.py")
    content = path.read_bytes().replace(b"\r\n", b"\n")
    tree_digest = sha256(canonical_json([{"path": path.as_posix(), "sha256": sha256(content).hexdigest()}]).encode()).hexdigest()
    provider = _TestProvider()
    monkeypatch.setattr(provider_module, "CodeImplementationSocketClient", _TestProvider)
    monkeypatch.setattr(
        provider_module,
        "resolve_code_implementation_provider",
        lambda *_args, **_kwargs: {
            "ok": True,
            "provider_ready": True,
            "provider": provider,
            "implementation_digest": provider_module.IMPLEMENTATION_DIGEST,
            "provider_instance_id": provider_module.PROVIDER_INSTANCE_ID,
            "advertisement_id": "advertisement-test",
            "advertisement_digest": "c" * 64,
            "catalog_case_id": "catalog-test",
            "catalog_snapshot_digest": "d" * 64,
        },
    )
    report = propose_code_patch_v2(
        object(),
        transaction_id="tx-bridge",
        request_id="request-bridge",
        nonce="nonce-bridge",
        incident={
            "incident_id": "incident-bridge",
            "incident_digest": "a" * 64,
            "incident_class": "l5.product_completion_semantic_misreport",
            "title": "bounded test incident",
            "summary": "test-only provider contract",
            "diagnostic_codes": ["test"],
            "acceptance_requirements": ["proposal_only"],
        },
        scope=SCOPE,
        repo_root=Path.cwd(),
        base_commit="b" * 40,
        base_tree_digest=tree_digest,
        allowed_files=["eimemory/governance/l5_reader.py"],
        test_plan_id="l5.product-completion-reporting.v1",
        test_plan_digest=protected_test_plan_digest("l5.product-completion-reporting.v1"),
        bounds={"maximum_files": 1, "maximum_bytes_per_file": 48 * 1024, "maximum_total_bytes": 96 * 1024, "maximum_changed_lines": 400},
    )
    assert report["ok"] is True, report.get("reason")
    assert report["proposal_only"] is True
    assert report["qualifying"] is True
    assert report["incident"]["incident_id"] == "incident-bridge"
    assert report["repository"]["repository_root"] == str(Path.cwd())
    assert report["repository"]["repository_remote"] == "origin"
    assert len(report["repository"]["remote_url_digest"]) == 64
    assert report["profile_key"] == "l5.default"
    assert report["provider"]["capability_id"] == "code.implementation"
    assert report["provider"]["revision_id"] == "code.implementation:v8"
    assert provider.timeout_seconds == provider_module.FIXED_COMPLETION_TIMEOUT_SECONDS + 60.0
    assert "commands" not in report
    assert "verification_commands" not in report
    assert "provider_override" not in inspect.signature(propose_code_patch_v2).parameters


def test_release_closure_bridge_rejects_storage_receipt_fallback(monkeypatch) -> None:
    path = Path("eimemory/governance/release_closure_gate_evidence.py")
    source = path.read_bytes().replace(b"\r\n", b"\n")
    tree_digest = sha256(
        canonical_json([{"path": path.as_posix(), "sha256": sha256(source).hexdigest()}]).encode()
    ).hexdigest()

    class InvalidProvider(_TestProvider):
        def propose_patch_v2(self, request):
            allowed = request["allowed_files"][0]
            invalid = '''
def build_release_lineage_gate_evidence(*, receipt_record_id, live_record_ids, **kwargs):
    current_receipt = receipt_record_id if receipt_record_id in live_record_ids else live_record_ids[-1]
    evidence = {
        "storage.integrity": list(live_record_ids),
        "deployment.runtime": [current_receipt],
        "code.evolution": [current_receipt],
    }
    return evidence
'''
            response = {
                "schema": "code_implementation_response.v2",
                "request_id": request["request_id"],
                "request_digest": request["request_digest"],
                "file_updates": [{"path": allowed["path"], "prior_sha256": allowed["sha256"], "content": invalid}],
                "rationale": "incorrectly infer receipt from storage records",
                "assumptions": [],
            }
            return {
                "ok": True,
                "operation": "propose_patch_v2",
                "attestation": build_attestation(
                    request, response, completed_at="2026-08-23T00:00:00Z", nonce=request["nonce"]
                ),
                "response": response,
            }

    provider = InvalidProvider()
    monkeypatch.setattr(provider_module, "CodeImplementationSocketClient", InvalidProvider)
    monkeypatch.setattr(
        provider_module,
        "resolve_code_implementation_provider",
        lambda *_args, **_kwargs: {
            "ok": True,
            "provider_ready": True,
            "provider": provider,
            "implementation_digest": provider_module.IMPLEMENTATION_DIGEST,
            "provider_instance_id": provider_module.PROVIDER_INSTANCE_ID,
            "advertisement_id": "advertisement-test",
            "advertisement_digest": "c" * 64,
            "catalog_case_id": "catalog-test",
            "catalog_snapshot_digest": "d" * 64,
        },
    )
    report = propose_code_patch_v2(
        object(),
        transaction_id="tx-release-closure",
        request_id="request-release-closure",
        nonce="nonce-release-closure",
        incident={
            "incident_id": "incident-release-closure",
            "incident_digest": "a" * 64,
            "incident_class": "release.closure_internal_failure",
            "title": "exact receipt evidence required",
            "summary": "deployment receipt and storage acceptance records are independent",
            "diagnostic_codes": ["release_lineage:code_evolution_gate_evidence_missing"],
            "acceptance_requirements": ["deployment_receipt_fallback_is_forbidden"],
        },
        scope=SCOPE,
        repo_root=Path.cwd(),
        base_commit="b" * 40,
        base_tree_digest=tree_digest,
        allowed_files=[path.as_posix()],
        test_plan_id="release.closure-self-repair.v1",
        test_plan_digest=protected_test_plan_digest("release.closure-self-repair.v1"),
        bounds={"maximum_files": 1, "maximum_bytes_per_file": 48 * 1024, "maximum_total_bytes": 96 * 1024, "maximum_changed_lines": 400},
    )

    assert report["ok"] is False
    assert report["status"] == "blocked"
    assert report["reason"] == (
        "provider_response_invalid:release_closure_receipt_must_be_authoritative_input"
    )
