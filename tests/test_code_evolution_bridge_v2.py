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
    path = Path("eimemory/governance/l5_reader.py")
    content = path.read_bytes().replace(b"\r\n", b"\n")
    tree_digest = sha256(canonical_json([{"path": str(path), "sha256": sha256(content).hexdigest()}]).encode()).hexdigest()
    monkeypatch.setattr(
        provider_module,
        "resolve_code_implementation_provider",
        lambda *_args, **_kwargs: {
            "ok": True,
            "provider_ready": True,
            "provider": _TestProvider(),
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
    assert report["ok"] is True
    assert report["proposal_only"] is True
    assert report["qualifying"] is True
    assert report["incident"]["incident_id"] == "incident-bridge"
    assert report["repository"]["repository_root"] == str(Path.cwd())
    assert report["provider"]["capability_id"] == "code.implementation"
    assert report["provider"]["revision_id"] == "code.implementation:v5"
    assert "commands" not in report
    assert "verification_commands" not in report
    assert "provider_override" not in inspect.signature(propose_code_patch_v2).parameters
