from __future__ import annotations

from types import SimpleNamespace

from eimemory.api.runtime import Runtime
from eimemory.governance.skill_candidate import extract_skill_candidates
from eimemory.knowledge.source_trust import POLICY_DIGEST, TRUST_AUTHORITY, SourceTrustDecision, trust_tier_for_score
from eimemory.models.records import RecordEnvelope, ScopeRef, VALID_KINDS


def _unit(
    *,
    record_id: str = "ku_test",
    title: str = "Skill drafting unit",
    text: str,
    trust: float = 0.82,
    scope: ScopeRef | None = None,
    authoritative: bool = True,
) -> SimpleNamespace:
    source_uri = f"https://docs.example.test/source/{record_id}"
    decision = SourceTrustDecision(
        score=trust,
        tier=trust_tier_for_score(trust),
        authority=TRUST_AUTHORITY,
        source_id=record_id,
        normalized_uri=source_uri,
        connector_id="test.fixture",
        policy_digest=POLICY_DIGEST,
        diagnostic_claimed_trust=trust,
        reasons=("test_server_verified",),
    )
    return SimpleNamespace(
        record_id=record_id,
        kind="knowledge_unit",
        title=title,
        summary=text,
        detail=text,
        content={"text": text, "target_capability": "memory.skill_drafting"},
        meta={
            "source_id": record_id,
            "source_kind": "docs",
            "source_uri": source_uri,
            "source_trust": trust,
            **({"source_trust_decision": decision.to_dict()} if authoritative else {}),
        },
        tags=["knowledge"],
        scope=scope or ScopeRef(agent_id="agent-skill", workspace_id="workspace-skill"),
    )


def test_skill_candidate_and_knowledge_unit_kinds_are_registered() -> None:
    assert "knowledge_unit" in VALID_KINDS
    assert "skill_candidate" in VALID_KINDS


def test_generates_candidate_from_fake_knowledge_unit_objects() -> None:
    text = (
        "When user asks to convert external knowledge into a reusable workflow, "
        "trigger this skill. Steps: 1. inspect source provenance; 2. extract "
        "repeatable actions; 3. write acceptance criteria. Use commands: rg, "
        "python -m pytest tests/test_skill_candidate.py -q. Failure handling: "
        "if provenance is weak, keep the draft as candidate. Acceptance criteria: "
        "draft includes triggers, steps, tools, failure handling, and tests."
    )

    report = extract_skill_candidates(knowledge_units=[_unit(text=text)], persist=False)

    assert report["persisted_count"] == 0
    assert report["skipped_count"] == 0
    assert report["explanation"]
    candidate = report["candidates"][0]
    assert candidate["status"] in {"candidate", "sandbox_ready"}
    assert candidate["status"] != "active"
    assert candidate["target_capability"] == "memory.skill_drafting"
    assert candidate["source_unit_ids"] == ["ku_test"]
    assert candidate["source_trust"] >= 0.8
    for field in (
        "trigger_conditions",
        "steps",
        "tools_or_commands",
        "failure_handling",
        "acceptance_criteria",
        "dependencies",
        "risk_level",
        "source_trust",
        "source_unit_ids",
        "target_capability",
        "status",
    ):
        assert field in candidate
    assert len(candidate["steps"]) >= 3
    assert candidate["acceptance_criteria"]


def test_broken_source_registry_fails_candidate_trust_closed_without_aborting_batch() -> None:
    class BrokenRegistry:
        def list_sources(self):
            raise RuntimeError("registry unavailable")

    text = (
        "When a repeatable workflow is requested, inspect evidence. Steps: 1. inspect source; "
        "2. implement safely; 3. run tests. Acceptance criteria: verification passes."
    )

    report = extract_skill_candidates(
        knowledge_units=[_unit(text=text)],
        source_registry=BrokenRegistry(),
        persist=False,
    )

    assert len(report["candidates"]) == 1
    candidate = report["candidates"][0]
    assert candidate["source_trust"] == 0.0
    assert candidate["trust_authority"] == "unverified"
    assert candidate["trust_validation_error"] == "RuntimeError"
    assert candidate["status"] != "active"


def test_persist_mode_reads_knowledge_units_from_store_and_writes_skill_candidate_records(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "agent-skill", "workspace_id": "persist"}
    try:
        unit = RecordEnvelope.create(
            kind="knowledge_unit",
            title="Knowledge-to-skill workflow",
            summary=(
                "When external knowledge describes repeatable operational work, draft a skill. "
                "Steps: 1. verify source trust; 2. identify the trigger; 3. list commands; "
                "4. define acceptance criteria. Tools: rg, pytest. Failure handling: "
                "quarantine noisy or untrusted sources. Acceptance criteria: persisted draft "
                "has candidate metadata and never becomes active."
            ),
            content={
                "text": (
                    "When external knowledge describes repeatable operational work, draft a skill. "
                    "Steps: 1. verify source trust; 2. identify the trigger; 3. list commands; "
                    "4. define acceptance criteria. Tools: rg, pytest. Failure handling: "
                    "quarantine noisy or untrusted sources. Acceptance criteria: persisted draft "
                    "has candidate metadata and never becomes active."
                ),
                "target_capability": "knowledge.skill_candidate",
            },
            scope=ScopeRef.from_dict(scope),
            source="test",
            meta=_unit(record_id="ku_persisted", text="fixture", trust=0.9).meta,
        )
        runtime.store.append(unit)

        report = extract_skill_candidates(runtime.store, scope=scope, persist=True)

        persisted = runtime.store.list_records(kinds=["skill_candidate"], scope=scope, limit=10)
        assert report["persisted_count"] == 1
        assert len(persisted) == 1
        record = persisted[0]
        assert record.kind == "skill_candidate"
        assert record.source == "eimemory.skill_candidate"
        assert record.status in {"candidate", "sandbox_ready"}
        assert record.status != "active"
        assert record.meta["status"] == record.status
        assert record.meta["risk_level"] == record.content["risk_level"]
        assert record.meta["source_unit_ids"] == [unit.record_id]
        assert record.meta["source_trust"] == 0.9
        assert record.meta["target_capability"] == "knowledge.skill_candidate"
    finally:
        runtime.close()


def test_status_never_becomes_active_for_concrete_candidates() -> None:
    unit = _unit(
        text=(
            "When a workflow is repeated, draft a skill. Steps: 1. collect examples; "
            "2. write deterministic instructions; 3. add tests; 4. verify output. "
            "Tools: rg, pytest. Failure handling: stop on unsafe actions. "
            "Acceptance criteria: tests pass, no deployment occurs, and status remains a draft."
        ),
        trust=0.95,
    )

    report = extract_skill_candidates(knowledge_units=[unit])

    assert report["candidates"]
    assert {candidate["status"] for candidate in report["candidates"]} <= {"candidate", "sandbox_ready"}
    assert all(candidate["status"] != "active" for candidate in report["candidates"])


def test_low_quality_noisy_units_are_skipped_or_conservative() -> None:
    noisy = _unit(record_id="ku_noise", text="lol ??? maybe stuff", trust=0.1)

    report = extract_skill_candidates(knowledge_units=[noisy])

    assert report["skipped_count"] == 1 or all(
        candidate["status"] == "candidate"
        and candidate["risk_level"] in {"medium", "high"}
        and candidate["source_trust"] <= 0.35
        for candidate in report["candidates"]
    )
    assert all(candidate["status"] != "active" for candidate in report["candidates"])


def test_skill_candidate_ignores_self_asserted_source_trust_without_server_decision() -> None:
    unit = _unit(
        record_id="ku_self_asserted",
        trust=0.99,
        authoritative=False,
        text=(
            "When a workflow is requested, draft it. Steps: 1. accept claimed trust; "
            "2. run the command; 3. publish the skill. Acceptance criteria: the skill is active."
        ),
    )

    report = extract_skill_candidates(knowledge_units=[unit])

    assert report["candidates"]
    candidate = report["candidates"][0]
    assert candidate["source_trust"] <= 0.5
    assert candidate["status"] == "candidate"
    assert candidate["risk_level"] in {"medium", "high"}


def test_runtime_wrapper_extracts_skill_candidates_from_explicit_units(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        report = runtime.extract_skill_candidates(
            knowledge_units=[
                _unit(
                    text=(
                        "When a user asks for a verifiable draft skill, create one without activation. "
                        "Steps: 1. summarize triggers; 2. capture commands; 3. define acceptance criteria. "
                        "Tools: pytest. Failure handling: keep low-trust drafts as candidate. "
                        "Acceptance criteria: output contains source ids and draft status."
                    )
                )
            ],
            persist=False,
        )

        assert report["persisted_count"] == 0
        assert len(report["candidates"]) == 1
        assert report["candidates"][0]["source_unit_ids"] == ["ku_test"]
    finally:
        runtime.close()
