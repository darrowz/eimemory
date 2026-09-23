from dataclasses import replace
from eimemory.api.runtime import Runtime
from eimemory.adapters.hermes.code_implementation import REVISION_ID
from eimemory.capabilities.code_implementation_bootstrap import code_implementation_revision, register_code_implementation_v2


def test_changed_implementation_registers_v11_without_rewriting_v10(tmp_path):
    scope = dict(tenant_id='default', agent_id='operator', workspace_id='workspace', user_id='owner')
    runtime = Runtime.create(root=tmp_path)
    try:
        runtime.apply_capability_seed_manifest(scope=scope)
        current = code_implementation_revision()
        old_contract = current.to_dict()['contract']
        old_contract['evidence_requirements']['implementation_digest'] = 'a' * 64
        old = replace(current, revision_id='code.implementation:v10', contract=old_contract)
        runtime.capabilities.register_revision(old, runtime_scope=scope, request_key='old-v10')
        result = register_code_implementation_v2(runtime, runtime_scope=scope)
        assert result['ok'], result
        assert result['revision_id'] == REVISION_ID
        assert any(row['entity_id'] == 'code.implementation:v10' and row['status'] == 'deprecated' for row in result['superseded_revision_transitions'])
        # Re-registering exactly the old immutable fact must still be idempotent.
        receipt = runtime.capabilities.register_revision(old, runtime_scope=scope, request_key='old-v10')
        assert receipt.idempotent
        again = register_code_implementation_v2(runtime, runtime_scope=scope)
        assert again['ok'], again
        assert again['revision_receipt']['idempotent']
    finally:
        runtime.close()
