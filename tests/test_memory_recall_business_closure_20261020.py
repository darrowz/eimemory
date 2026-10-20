"""Real local storage/recall with a fixed model boundary, never live acceptance."""
from contextlib import closing
from dataclasses import asdict
import json
from types import SimpleNamespace

import pytest

from eimemory.api.runtime import Runtime
from eimemory.governance.l5.l5_product_completion import build_product_completion
from eimemory.governance.l5.real_replay_gate import build_verified_real_replay_summary
from eimemory.knowledge.l1_pipeline import extract_l1_from_l0_record
from eimemory.metadata import business_metadata
from eimemory.models.records import RecordEnvelope, ScopeRef


SCOPE = ScopeRef(tenant_id="synthetic", agent_id="honda", workspace_id="closure-audit", user_id="owner")


class FixedExtractionAndJudge:
    """Only substitute the nondeterministic model; use actual conflict retrieval."""

    def __init__(self, text):
        self.text = text

    def complete(self, **kwargs):
        prompt = kwargs["user_prompt"]
        if prompt.startswith("{"):
            inputs = json.loads(prompt)
            decisions = []
            for item in inputs["new_memories"]:
                targets = item["related_ids"]
                assert len(targets) == 1
                decisions.append({"record_id": item["record_id"], "action": "update",
                    "target_ids": targets[0], "merged_content": self.text, "merged_type": "fact"})
            return SimpleNamespace(text=json.dumps(decisions, ensure_ascii=False))
        return SimpleNamespace(text=json.dumps([
            {"content": self.text, "type": "fact", "priority": 90},
        ], ensure_ascii=False))


def test_l0_correction_reaches_normal_l1_recall_without_manufacturing_l5(tmp_path, monkeypatch):
    monkeypatch.setattr("eimemory.llm.command_client.CommandLLMClient.complete",
        lambda *args, **kwargs: pytest.fail("This acceptance fixture cannot call an external model"))
    old_text = "鸿欣账号属于小马哥。"
    corrected_text = "更正：鸿欣账号属于钊哥，不属于小马哥。"
    with closing(Runtime.create(root=tmp_path)) as runtime:
        children = []
        for index, text in enumerate((old_text, corrected_text), start=1):
            raw = "User: " + text + "\nAssistant: 收到。"
            parent = RecordEnvelope.create(kind="memory", title="Synthetic source conversation",
                summary=raw, content={"text": raw, "memory_type": "conversation"},
                scope=SCOPE, source="hermes.turn", source_id="hermes",
                meta={"memory_layer": "l0", "memory_type": "conversation", "capture_origin": "turn_sync"})
            parent.time.occurred_at = f"2026-10-0{index}T00:00:00Z"
            runtime.store.append(parent)
            written = extract_l1_from_l0_record(runtime.memory, parent, use_llm=True,
                llm=FixedExtractionAndJudge(text), fallback_heuristic=False)
            assert len(written) == 1
            child = runtime.store.get_by_exact_ref(written[0]["record_id"], scope=SCOPE, source_id="hermes")
            assert child.status == "active" and child.content["memory_type"] == "fact"
            assert any(link.relation == "derived_from" and link.target_id == parent.record_id for link in child.links)
            completed = runtime.store.get_by_exact_ref(parent.record_id, scope=SCOPE, source_id="hermes")
            assert business_metadata(completed.meta)["l1_extraction_status"] == "stored"
            children.append(child.record_id)

        old = runtime.store.get_by_exact_ref(children[0], scope=SCOPE, source_id="hermes")
        assert old.status == "superseded"
        assert business_metadata(old.meta)["superseded_by"] == children[1]
        for query in ("鸿欣账号归属", "鸿欣账号属于谁"):
            bundle = runtime.memory.recall(query=query, scope=asdict(SCOPE), limit=5,
                task_context={"source_ids": ["hermes"], "exact_scope_only": True})
            assert [item.record_id for item in bundle.items] == [children[1]]
            assert bundle.items[0].summary == corrected_text
            assert bundle.explanation["relevance_selector"]["status"] == "evidence_found"

        other_scope = {**asdict(SCOPE), "user_id": "another-user"}
        assert runtime.memory.recall(query="鸿欣账号归属", scope=other_scope, limit=5,
            task_context={"source_ids": ["hermes"], "exact_scope_only": True}).items == []

        # Synthetic L0/L1 success is not a real business terminal receipt.
        replay = build_verified_real_replay_summary(runtime, scope=SCOPE)
        assert replay["ok"] is False and replay["sample_count"] == 0
        product = build_product_completion({"ok": True, "status": "ready"},
            provider={"ready": True, "catalog_ready": True, "advertisement_fresh": True},
            current_lineage={"ok": True, "compatible": True})
        assert product["product_l5_complete"] is False
        assert "terminal_receipt_unbound" in product["gaps"]
