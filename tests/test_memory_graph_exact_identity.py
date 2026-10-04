from dataclasses import replace

from eimemory.api.runtime import Runtime
from eimemory.models.records import LinkRef, RecordEnvelope, ScopeRef


def row(record_id, scope, source="synthetic", links=()):
    result = RecordEnvelope.create(kind="memory", title="Synthetic graph", scope=scope,
                                   source_id=source, links=list(links), content={"text": "Synthetic"},
                                   meta={"force_capture": True})
    result.record_id = record_id
    return result


def test_graph_same_id_scopes_keep_both_and_second_hops(tmp_path):
    runtime = Runtime.create(root=tmp_path)
    scope = ScopeRef(tenant_id="synthetic", agent_id="agent", workspace_id="workspace", user_id="user")
    shared = replace(scope, user_id="")
    seed = row("seed", scope, links=[LinkRef(relation="related", target_kind="memory", target_id="same")])
    private = row("same", scope, links=[LinkRef(relation="related", target_kind="memory", target_id="next-private")])
    public = row("same", shared, links=[LinkRef(relation="related", target_kind="memory", target_id="next-shared")])
    records = [seed, private, public, row("next-private", scope), row("next-shared", shared)]
    try:
        for item in records:
            runtime.store.append(item)
        assert runtime.store.get_by_exact_ref("same", scope=scope, source_id="synthetic") is not None
        assert runtime.store.get_by_exact_ref("same", scope=shared, source_id="synthetic") is not None
        result = runtime.memory._expand_graph_items(base_items=[seed], scopes=[scope], graph_depth=2, source_ids=("synthetic",))
        assert [(x.record_id, x.scope.user_id) for x in result] == [("seed", "user"), ("same", "user"), ("same", ""), ("next-private", "user"), ("next-shared", "")]
        assert runtime.memory._expand_graph_items(base_items=[private, private, public, public], scopes=[scope], graph_depth=0) == [private, public]
    finally:
        runtime.close()


def test_preference_request_and_types_use_shared_contract(tmp_path):
    runtime = Runtime.create(root=tmp_path)
    try:
        assert runtime.memory._is_preference_query("How should you reply to me?", {})
        scope = ScopeRef(user_id="synthetic")
        item = row("preference", scope)
        item.content["memory_type"] = "USER_PREFERENCE"
        item.content["text"] = "I prefer concise replies."
        assert runtime.memory._is_preference_recall_candidate(item, "my reply style")
    finally:
        runtime.close()
