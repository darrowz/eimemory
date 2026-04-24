from eimemory.models.records import RecordEnvelope, ScopeRef, TimeRef
from eimemory.storage.runtime_store import RuntimeStore


def test_runtime_store_persists_and_searches_records(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="demo")

    record = RecordEnvelope.create(
        kind="memory",
        title="OpenClaw recall",
        summary="Recall project memory before prompt build",
        scope=scope,
        tags=["openclaw"],
    )
    store.append(record)

    results = store.search(query="prompt build", kinds=["memory"], scope=scope, limit=5)

    assert len(results) == 1
    assert results[0].record_id == record.record_id


def test_runtime_store_returns_active_policy_rules(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    scope = ScopeRef(agent_id="eibrain", workspace_id="robot")

    rule = RecordEnvelope.create(
        kind="rule",
        title="Prefer task context",
        summary="Use task context first for brain respond",
        scope=scope,
        status="active",
        meta={
            "task_type": "brain.respond",
            "retrieval_policy": {
                "route_hint": "task_context_first",
                "open_unknown_on_low_confidence": True,
            },
        },
    )
    store.append(rule)

    policy = store.get_active_policy(task_type="brain.respond", scope=scope)

    assert policy["retrieval_policy"]["route_hint"] == "task_context_first"
    assert policy["retrieval_policy"]["open_unknown_on_low_confidence"] is True


def test_runtime_store_hybrid_search_matches_semantic_overlap(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="demo")

    store.append(
        RecordEnvelope.create(
            kind="memory",
            title="Concise assistant replies",
            summary="Respond briefly with compact answers for the operator",
            scope=scope,
        )
    )

    results = store.search(query="short concise responses", kinds=["memory"], scope=scope, limit=5)

    assert results
    assert results[0].title == "Concise assistant replies"


def test_runtime_store_quality_reranks_similar_memories(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="demo")
    high_quality = RecordEnvelope.create(
        kind="memory",
        title="Core deployment preference",
        summary="OpenClaw memory deployments must keep durable gateway state.",
        scope=scope,
        meta={
            "quality": {
                "importance": 0.95,
                "confidence": 0.95,
                "freshness": 1.0,
                "reuse_potential": 0.95,
                "salience_score": 0.95,
                "quality_tier": "core",
                "capture_decision": "accept",
            }
        },
    )
    low_quality = RecordEnvelope.create(
        kind="memory",
        title="Candidate deployment note",
        summary="OpenClaw gateway memory deploy note.",
        scope=scope,
        meta={
            "quality": {
                "importance": 0.12,
                "confidence": 0.2,
                "freshness": 1.0,
                "reuse_potential": 0.1,
                "salience_score": 0.18,
                "quality_tier": "candidate",
                "capture_decision": "accept",
            }
        },
    )
    store.append(high_quality)
    store.append(low_quality)

    results, report = store.search_with_diagnostics(
        query="openclaw gateway memory deploy",
        kinds=["memory"],
        scope=scope,
        limit=2,
    )

    assert [record.title for record in results] == [
        "Core deployment preference",
        "Candidate deployment note",
    ]
    assert report["retrieval_mode"] == "hybrid_vector"
    assert report["scored_items"][0]["quality"]["salience_score"] == 0.95
    assert report["scored_items"][0]["final_score"] > report["scored_items"][1]["final_score"]


def test_runtime_store_quality_does_not_match_unrelated_memories(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="demo")
    store.append(
        RecordEnvelope.create(
            kind="memory",
            title="Core deployment preference",
            summary="OpenClaw gateway memory deployments must keep durable state.",
            scope=scope,
            meta={
                "quality": {
                    "importance": 1.0,
                    "confidence": 1.0,
                    "freshness": 1.0,
                    "reuse_potential": 1.0,
                    "salience_score": 1.0,
                    "quality_tier": "core",
                    "capture_decision": "accept",
                }
            },
        )
    )

    results = store.search(query="banana smoothie recipe", kinds=["memory"], scope=scope, limit=5)

    assert results == []


def test_runtime_store_quality_boost_does_not_return_weakly_related_vector_noise(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="demo")
    store.append(
        RecordEnvelope.create(
            kind="memory",
            title="OpenClaw agent outcome",
            summary="OpenClaw memory quality should improve useful agent recall.",
            scope=scope,
            meta={
                "quality": {
                    "importance": 0.95,
                    "confidence": 0.95,
                    "freshness": 1.0,
                    "reuse_potential": 0.95,
                    "salience_score": 0.95,
                    "quality_tier": "core",
                    "capture_decision": "accept",
                }
            },
        )
    )

    results = store.search(
        query="EIMEMORY_SOURCE_CANDIDATE_UNIQUE_1781951",
        kinds=["memory", "claim_card", "knowledge_page"],
        scope=scope,
        limit=5,
    )

    assert results == []


def test_runtime_store_excludes_rejected_records_from_search(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="demo")
    store.append(
        RecordEnvelope.create(
            kind="memory",
            title="Rejected memory",
            summary="OpenClaw gateway memory deploy note.",
            scope=scope,
            status="rejected",
            meta={"quality": {"capture_decision": "reject", "salience_score": 0.0}},
        )
    )

    results = store.search(query="openclaw gateway", kinds=["memory"], scope=scope, limit=5)

    assert results == []


def test_runtime_store_scope_isolated_by_tenant_and_user(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    shared_agent_workspace = {"agent_id": "main", "workspace_id": "demo"}
    store.append(
        RecordEnvelope.create(
            kind="memory",
            title="Tenant A memory",
            summary="Only tenant A should see this",
            scope=ScopeRef(tenant_id="tenant-a", user_id="alice", **shared_agent_workspace),
        )
    )
    store.append(
        RecordEnvelope.create(
            kind="memory",
            title="Tenant B memory",
            summary="Only tenant B should see this",
            scope=ScopeRef(tenant_id="tenant-b", user_id="bob", **shared_agent_workspace),
        )
    )

    tenant_a_results = store.search(
        query="memory",
        kinds=["memory"],
        scope=ScopeRef(tenant_id="tenant-a", user_id="alice", **shared_agent_workspace),
        limit=5,
    )
    tenant_b_results = store.search(
        query="memory",
        kinds=["memory"],
        scope=ScopeRef(tenant_id="tenant-b", user_id="bob", **shared_agent_workspace),
        limit=5,
    )

    assert [record.title for record in tenant_a_results] == ["Tenant A memory"]
    assert [record.title for record in tenant_b_results] == ["Tenant B memory"]


def test_runtime_store_user_scope_can_see_global_memories_but_not_other_users(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    shared = {"tenant_id": "tenant-a", "agent_id": "main", "workspace_id": "demo"}
    store.append(
        RecordEnvelope.create(
            kind="memory",
            title="Global memory",
            summary="Shared global project memory",
            scope=ScopeRef(user_id="", **shared),
        )
    )
    store.append(
        RecordEnvelope.create(
            kind="memory",
            title="Alice memory",
            summary="Alice project memory",
            scope=ScopeRef(user_id="alice", **shared),
        )
    )
    store.append(
        RecordEnvelope.create(
            kind="memory",
            title="Bob memory",
            summary="Bob project memory",
            scope=ScopeRef(user_id="bob", **shared),
        )
    )

    results = store.search(
        query="project memory",
        kinds=["memory"],
        scope=ScopeRef(user_id="alice", **shared),
        limit=10,
    )

    titles = {record.title for record in results}
    assert "Global memory" in titles
    assert "Alice memory" in titles
    assert "Bob memory" not in titles


def test_runtime_store_empty_user_scope_does_not_wildcard_private_users(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    shared = {"tenant_id": "tenant-a", "agent_id": "main", "workspace_id": "demo"}
    store.append(
        RecordEnvelope.create(
            kind="memory",
            title="Global memory",
            summary="Shared global project memory",
            scope=ScopeRef(user_id="", **shared),
        )
    )
    store.append(
        RecordEnvelope.create(
            kind="memory",
            title="Alice memory",
            summary="Alice private project memory",
            scope=ScopeRef(user_id="alice", **shared),
        )
    )

    results = store.search(
        query="project memory",
        kinds=["memory"],
        scope=ScopeRef(user_id="", **shared),
        limit=10,
    )

    assert [record.title for record in results] == ["Global memory"]


def test_runtime_store_scopes_duplicate_record_ids_independently(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    shared_id = "stable_paper_id"
    tenant_a = ScopeRef(tenant_id="tenant-a", agent_id="main", workspace_id="repo")
    tenant_b = ScopeRef(tenant_id="tenant-b", agent_id="main", workspace_id="repo")
    for scope, title in [(tenant_a, "Tenant A paper"), (tenant_b, "Tenant B paper")]:
        store.append(
            RecordEnvelope(
                record_id=shared_id,
                kind="paper_source",
                status="active",
                title=title,
                summary=f"{title} summary",
                detail="",
                content={"text": f"{title} content"},
                tags=[],
                links=[],
                evidence=[],
                source="test",
                scope=scope,
                time=TimeRef(
                    created_at="2026-04-23T00:00:00+00:00",
                    updated_at="2026-04-23T00:00:00+00:00",
                    occurred_at="2026-04-23T00:00:00+00:00",
                ),
                provenance={},
                meta={},
            )
        )

    assert store.get_by_id(shared_id, scope=tenant_a).title == "Tenant A paper"
    assert store.get_by_id(shared_id, scope=tenant_b).title == "Tenant B paper"
    assert store.search(query="paper", kinds=["paper_source"], scope=tenant_a, limit=5)[0].title == "Tenant A paper"
    assert store.search(query="paper", kinds=["paper_source"], scope=tenant_b, limit=5)[0].title == "Tenant B paper"


def test_runtime_store_get_by_id_requires_matching_scope_when_provided(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    record = RecordEnvelope.create(
        kind="memory",
        title="Alice memory",
        summary="Alice private project memory",
        scope=ScopeRef(tenant_id="tenant-a", agent_id="main", workspace_id="demo", user_id="alice"),
    )
    store.append(record)

    assert store.get_by_id(
        record.record_id,
        scope=ScopeRef(tenant_id="tenant-a", agent_id="main", workspace_id="demo", user_id="bob"),
    ) is None


def test_runtime_store_list_records_uses_stable_tiebreaker_for_same_timestamp(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="demo")
    same_time = TimeRef(
        created_at="2026-04-21T00:00:00+00:00",
        updated_at="2026-04-21T00:00:00+00:00",
        occurred_at="2026-04-21T00:00:00+00:00",
    )
    records = [
        RecordEnvelope(
            record_id=record_id,
            kind="memory",
            status="active",
            title=f"Stable record {record_id}",
            summary="Stable pagination memory record",
            detail="",
            content={"text": "Stable pagination memory record"},
            tags=[],
            links=[],
            evidence=[],
            source="test",
            scope=scope,
            time=same_time,
            provenance={},
            meta={},
        )
        for record_id in ["mem_a", "mem_b", "mem_c"]
    ]
    for record in records:
        store.append(record)

    first_page = store.list_records(scope=scope, limit=2, offset=0)
    second_page = store.list_records(scope=scope, limit=2, offset=2)

    paged_ids = [record.record_id for record in [*first_page, *second_page]]
    assert paged_ids == ["mem_c", "mem_b", "mem_a"]
    assert len(set(paged_ids)) == 3



def test_runtime_store_prefers_user_scoped_policy_over_newer_global_rule(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    scoped = ScopeRef(agent_id="eibrain", workspace_id="robot", user_id="alice")
    global_scope = ScopeRef(agent_id="eibrain", workspace_id="robot")

    specific_rule = RecordEnvelope.create(
        kind="rule",
        title="Alice policy",
        summary="Alice-specific retrieval policy",
        scope=scoped,
        status="active",
        meta={
            "task_type": "brain.respond",
            "retrieval_policy": {"route_hint": "user_specific"},
        },
    )
    global_rule = RecordEnvelope.create(
        kind="rule",
        title="Global policy",
        summary="Global retrieval policy",
        scope=global_scope,
        status="active",
        meta={
            "task_type": "brain.respond",
            "retrieval_policy": {"route_hint": "global_default"},
        },
    )
    store.append(specific_rule)
    global_rule.time.updated_at = "9999-12-31T23:59:59+00:00"
    store.append(global_rule)

    policy = store.get_active_policy(task_type="brain.respond", scope=scoped)

    assert policy["retrieval_policy"]["route_hint"] == "user_specific"
