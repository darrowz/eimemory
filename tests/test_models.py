from eimemory.models.records import LinkRef, RecallBundle, RecordEnvelope, ScopeRef, evaluate_memory_quality


def test_record_envelope_builds_with_defaults() -> None:
    scope = ScopeRef(agent_id="main", workspace_id="repo-x")
    record = RecordEnvelope.create(
        kind="memory",
        title="OpenClaw memory note",
        summary="memory summary",
        scope=scope,
    )

    assert record.kind == "memory"
    assert record.status == "active"
    assert record.scope.agent_id == "main"
    assert record.record_id.startswith("mem_")
    assert record.time.created_at
    assert record.time.updated_at == record.time.created_at
    assert record.meta["scoring"]["memory_score_v1"]["schema_version"] == "memory_score.v1"


def test_record_envelope_keeps_typed_links() -> None:
    scope = ScopeRef(agent_id="main")
    record = RecordEnvelope.create(
        kind="reflection",
        title="retrieval miss",
        scope=scope,
        links=[
            LinkRef(
                relation="derived_from",
                target_kind="unknown",
                target_id="unk_123",
            )
        ],
    )

    assert len(record.links) == 1
    assert record.links[0].relation == "derived_from"
    assert record.links[0].target_kind == "unknown"


def test_recall_bundle_reports_selected_items_and_hint() -> None:
    scope = ScopeRef(agent_id="main")
    memory = RecordEnvelope.create(
        kind="memory",
        title="Use short replies",
        summary="Prefer short replies for brain output",
        scope=scope,
    )

    bundle = RecallBundle(
        items=[memory],
        rules=[],
        reflections=[],
        confidence=0.81,
        next_action_hint="prefer short reply",
        explanation={"query": "reply style"},
    )

    assert bundle.items[0].title == "Use short replies"
    assert bundle.confidence == 0.81
    assert bundle.next_action_hint == "prefer short reply"


def test_memory_quality_accepts_high_value_project_facts() -> None:
    quality = evaluate_memory_quality(
        text="Decision: eimemory should keep OpenClaw project memories scoped by tenant and user.",
        title="OpenClaw scope decision",
        memory_type="decision",
    )

    assert quality["capture_decision"] == "accept"
    assert quality["quality_tier"] in {"confirmed", "core"}
    assert quality["importance"] >= 0.6
    assert quality["salience_score"] >= 0.55


def test_memory_quality_rejects_thin_chatter_unless_forced() -> None:
    rejected = evaluate_memory_quality(text="ok", title="chat", memory_type="conversation")
    forced = evaluate_memory_quality(text="ok", title="chat", memory_type="conversation", force_capture=True)

    assert rejected["capture_decision"] == "reject"
    assert rejected["quality_tier"] == "rejected"
    assert forced["capture_decision"] == "accept"
