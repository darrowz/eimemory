from __future__ import annotations

from hashlib import sha256
from functools import partial
from contextlib import closing
import inspect
from pathlib import Path
import subprocess

import pytest

from eimemory.adapters.hermes import code_implementation as provider_module
from eimemory.governance.code_evolution_bridge import propose_code_patch_v2
from eimemory.governance.code_evolution_test_plans import protected_test_plan_digest
from eimemory.adapters.hermes.code_implementation import build_attestation, canonical_json
from code_provider_authority_cases import *


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


@pytest.mark.parametrize("wiring", ["unchanged", "unwired", "wired"])
@pytest.mark.parametrize("oversized", [False, True])
@pytest.mark.parametrize('unknown_source_facts', [False, True])
def test_v2_bridge_is_proposal_only_and_requires_attested_resolver_provider(monkeypatch, tmp_path, oversized, unknown_source_facts, wiring) -> None:
    monkeypatch.delenv("EIMEMORY_L5_V3_PROFILE", raising=False)
    path = Path("eimemory/governance/l5/l5_reader.py")
    # A real checkout's reader can exceed the fixed provider file limit.
    # Exercise this contract with a bounded source in a genuine clean repo.
    source = tmp_path / path
    source.parent.mkdir(parents=True)
    source.write_bytes(("VALUE = 1\n" + ("# bounded-source\n" * 4000 if oversized else "")).encode("utf-8"))
    content = source.read_bytes()
    for args in (("init", "--quiet"), ("config", "core.autocrlf", "false"),
                 ("remote", "add", "origin", "https://example.com/eimemory.git"),
                 ("add", "."), ("-c", "user.name=Test", "-c", "user.email=test@example.com",
                                 "commit", "--quiet", "-m", "Bounded fixture")):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path,
                          check=True, capture_output=True, text=True).stdout.strip()
    tree_digest = sha256(canonical_json([{"path": path.as_posix(), "sha256": sha256(content).hexdigest()}]).encode()).hexdigest()
    requests = []
    class WiringProvider(_TestProvider):
        def propose_patch_v2(self, request):
            requests.append(request)
            envelope = super().propose_patch_v2(request)
            if wiring != "unchanged":
                response = envelope["response"]
                response["file_updates"][0]["content"] += "\ndef proposal_revalidation():\n    return True\n"
                if wiring == "wired":
                    response["file_updates"][0]["content"] += "\nproposal_revalidation()\n"
                envelope["attestation"] = build_attestation(
                    request, response, completed_at="2026-08-23T00:00:00Z", nonce=request["nonce"])
            return envelope

    provider = WiringProvider()
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
    invoke = partial(propose_code_patch_v2,
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
        repo_root=tmp_path,
        base_commit=base,
        base_tree_digest=tree_digest,
        allowed_files=["eimemory/governance/l5/l5_reader.py"],
        test_plan_id="l5.product-completion-reporting.v1",
        test_plan_digest=protected_test_plan_digest("l5.product-completion-reporting.v1"),
        bounds={"maximum_files": 1, "maximum_bytes_per_file": 48 * 1024, "maximum_total_bytes": 96 * 1024, "maximum_changed_lines": 400},
        **({'origin': 'system_detector', 'detector': 'eimemory.release_closure_failure.v1',
            'known_before_detection': None, 'prior_user_reported': None, 'manual_bootstrap': None}
           if unknown_source_facts else {}),
    )
    report = invoke()
    if oversized:
        assert report["ok"] is False and report["reason"] == "allowed_file_content_invalid"
        assert source.read_bytes() == content
        assert subprocess.run(["git", "status", "--porcelain"], cwd=tmp_path,
                              check=True, capture_output=True, text=True).stdout == ""
        return
    if wiring == "unwired":
        assert report["ok"] is False
        assert report["reason"] == "provider_response_invalid:proposal_unwired_public_functions"
        assert source.read_bytes() == content
        assert subprocess.run(["git", "status", "--porcelain"], cwd=tmp_path,
                              check=True, capture_output=True, text=True).stdout == ""
        # Same request identity must reach the provider again after rejection.
        wiring = "wired"
        report = invoke()
        assert len(requests) == 2
        assert requests[0] == requests[1]
    assert report["ok"] is True, report.get("reason")
    assert report["proposal_only"] is True
    assert report["qualifying"] is True
    assert report["incident"]["incident_id"] == "incident-bridge"
    assert report["repository"]["repository_root"] == str(tmp_path)
    assert report["repository"]["base_commit"] == base
    assert source.read_bytes() == content
    assert subprocess.run(["git", "status", "--porcelain"], cwd=tmp_path,
                          check=True, capture_output=True, text=True).stdout == ""
    assert report["repository"]["repository_remote"] == "origin"
    assert len(report["repository"]["remote_url_digest"]) == 64
    assert report["profile_key"] == "l5.default"
    assert report["provider"]["capability_id"] == "code.implementation"
    assert report["provider"]["revision_id"] == provider_module.REVISION_ID
    assert provider.timeout_seconds == provider_module.FIXED_COMPLETION_TIMEOUT_SECONDS + 60.0
    assert "commands" not in report
    assert "verification_commands" not in report
    assert "provider_override" not in inspect.signature(propose_code_patch_v2).parameters
    if unknown_source_facts:
        from eimemory.storage.runtime_store import RuntimeStore
        from eimemory.governance.evolution.code_evolution_transaction import CodeEvolutionTransactionManager
        from eimemory.contracts.code_incident_provenance import FIELDS, SOURCE_KEY
        assert all(report[key] is None for key in FIELDS)
        with closing(RuntimeStore(tmp_path/'isolated-ledger')) as store:
            manager = CodeEvolutionTransactionManager(store)
            result = manager.submit_proposal(report, scope=SCOPE, effects_enabled=False)
            assert all(result['transaction'][key] is None for key in FIELDS)
            receipt = manager.store.get_terminal_receipt(report['transaction_id'])
            assert all(receipt['payload'][SOURCE_KEY]['facts'][key] is None for key in FIELDS)


def test_release_closure_bridge_rejects_storage_receipt_fallback(monkeypatch) -> None:
    path = Path("eimemory/governance/release/release_closure_gate_evidence.py")
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


def test_proposal_wiring_ignores_untracked_callers_and_rejects_removed_callers(tmp_path):
    from eimemory.governance.evolution.code_evolution_bridge import _proposal_wiring_error
    package = tmp_path / 'eimemory'
    package.mkdir()
    source = package / 'api.py'
    source.write_text('def active_api():\n    return 1\n')
    caller = package / 'caller.py'
    caller.write_text('from .api import active_api\nactive_api()\n')
    subprocess.run(['git', 'init', '--quiet'], cwd=tmp_path, check=True)
    subprocess.run(['git', 'add', '.'], cwd=tmp_path, check=True)
    assert _proposal_wiring_error(tmp_path, []) == ''
    assert _proposal_wiring_error(tmp_path, [{'path': 'eimemory/caller.py', 'content': ''}]) == (
        'proposal_unwired_public_functions')
    (tmp_path / 'scratch.py').write_text('candidate_orphan()\n')
    update = {'path': 'eimemory/api.py', 'content': source.read_text() +
              '\ndef candidate_orphan():\n    return True\n'}
    assert _proposal_wiring_error(tmp_path, [update]) == 'proposal_unwired_public_functions'
    assert _proposal_wiring_error(tmp_path, [update, {'path': 'eimemory/caller.py',
        'content': caller.read_text() + '\nfrom .api import candidate_orphan\ncandidate_orphan()\n'}]) == ''
    caller.unlink()
    assert _proposal_wiring_error(tmp_path, []) == 'proposal_wiring_source_unavailable'


@pytest.mark.parametrize('directory', ['.worktrees', '.venv', 'build'])
def test_proposal_wiring_skips_tracked_directory_callers_before_reading(tmp_path, directory):
    from eimemory.governance.evolution.code_evolution_bridge import _proposal_wiring_error
    (tmp_path / 'eimemory').mkdir()
    (tmp_path / 'eimemory/api.py').write_text('')
    caller = tmp_path / 'nested' / directory / 'caller.py'
    caller.parent.mkdir(parents=True)
    caller.write_text('candidate_orphan()\n')
    subprocess.run(['git', 'init', '--quiet'], cwd=tmp_path, check=True)
    subprocess.run(['git', 'add', '-f', '.'], cwd=tmp_path, check=True)
    update = {'path': 'eimemory/api.py', 'content': 'def candidate_orphan():\n    return 1\n'}
    assert _proposal_wiring_error(tmp_path, [update]) == 'proposal_unwired_public_functions'
    caller.unlink()
    assert _proposal_wiring_error(tmp_path, [update]) == 'proposal_unwired_public_functions'


def test_proposal_wiring_fails_closed_on_symlink_source(tmp_path):
    from eimemory.governance.evolution.code_evolution_bridge import _proposal_wiring_error
    (tmp_path / 'eimemory').mkdir()
    (tmp_path / 'outside.txt').write_text('candidate_orphan()\n')
    (tmp_path / 'eimemory' / 'caller.py').symlink_to(tmp_path / 'outside.txt')
    subprocess.run(['git', 'init', '--quiet'], cwd=tmp_path, check=True)
    subprocess.run(['git', 'add', '.'], cwd=tmp_path, check=True)
    assert _proposal_wiring_error(tmp_path, []) == 'proposal_wiring_source_unavailable'
