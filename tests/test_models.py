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
    assert record.links[0].target_id == "unk_123"


def test_record_envelope_ordinary_defaults_are_independent() -> None:
    first = RecordEnvelope.create(kind="reflection", title="first", scope=ScopeRef(agent_id="main"))
    second = RecordEnvelope.create(kind="reflection", title="second", scope=ScopeRef(agent_id="main"))

    assert first.title == "first"
    assert first.summary == ""
    assert first.detail == ""
    assert first.source == "eimemory"
    assert first.content == {}
    assert first.tags == []
    assert first.links == []
    assert first.time.occurred_at == first.time.created_at
    first.content["text"] = "changed"
    first.tags.append("changed")
    first.links.append(LinkRef(relation="related_to", target_kind="reflection", target_id="ref_other"))
    assert second.content == {}
    assert second.tags == []
    assert second.links == []


def test_record_envelope_copies_ordinary_input_containers() -> None:
    content = {"text": "original"}
    tags = ["original"]
    link = LinkRef(relation="related_to", target_kind="reflection", target_id="ref_other")
    links = [link]
    record = RecordEnvelope.create(
        kind="reflection", title="copy", scope=ScopeRef(agent_id="main"),
        content=content, tags=tags, links=links,
    )

    content["text"] = "changed"
    tags.append("changed")
    links.clear()
    assert record.content == {"text": "original"}
    assert record.tags == ["original"]
    assert record.links == [link]


def test_ordinary_serialized_fields_and_containers() -> None:
    record = RecordEnvelope.create(
        kind="reflection", title="serialized", summary="summary", detail="detail",
        scope=ScopeRef(agent_id="main"), content={"count": 3}, tags=["tag"],
        links=[LinkRef(relation="related_to", target_kind="reflection", target_id="ref_other")],
    )
    payload = record.to_dict()
    assert payload["title"] == "serialized"
    assert payload["summary"] == "summary"
    assert payload["detail"] == "detail"
    assert payload["content"] == {"count": 3}
    assert payload["tags"] == ["tag"]
    assert payload["links"] == [{"relation": "related_to", "target_kind": "reflection", "target_id": "ref_other"}]
    assert payload["time"] == {
        "created_at": record.time.created_at,
        "updated_at": record.time.updated_at,
        "occurred_at": record.time.occurred_at,
    }
    payload["content"]["count"] = 4
    payload["tags"].append("changed")
    payload["links"][0]["relation"] = "changed"
    assert record.content == {"count": 3}
    assert record.tags == ["tag"]
    assert record.links[0].relation == "related_to"

    rule_item = RecordEnvelope.create(kind="reflection", title="rule slot", scope=ScopeRef(agent_id="main"))
    reflection_item = RecordEnvelope.create(kind="reflection", title="reflection slot", scope=ScopeRef(agent_id="main"))
    bundle = RecallBundle(items=[record], rules=[rule_item], reflections=[reflection_item], confidence=0.81,
                          next_action_hint="hint", explanation={"count": 2})
    serialized = bundle.to_dict()
    assert len(serialized["items"]) == 1
    assert isinstance(serialized["items"][0], dict)
    assert serialized["items"][0]["title"] == "serialized"
    assert len(serialized["rules"]) == 1
    assert isinstance(serialized["rules"][0], dict)
    assert serialized["rules"][0]["title"] == "rule slot"
    assert len(serialized["reflections"]) == 1
    assert isinstance(serialized["reflections"][0], dict)
    assert serialized["reflections"][0]["title"] == "reflection slot"
    assert serialized["confidence"] == 0.81
    assert serialized["next_action_hint"] == "hint"
    assert serialized["explanation"] == {"count": 2}
    serialized["explanation"]["count"] = 3
    assert bundle.explanation == {"count": 2}


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
