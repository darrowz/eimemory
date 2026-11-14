"""Synthetic reader-boundary contracts; parent executes under OS containment.

The source-echo fixture exercises ordinary capture and extraction. Default-off
readers withhold managed bodies; they do not acquire temporal answer authority.
The final test deliberately records the remaining true-old-reader limitation.
"""
from copy import deepcopy
from dataclasses import asdict
import json

import pytest

from test_ordinary_chat_temporal_semantics import SCOPE, assert_evidence, chat
from eimemory.adapters.runtime.channel import resolve_channel_scope
from eimemory.api.evolution import EvolutionAPI
from eimemory.api.memory import MemoryAPI
from eimemory.knowledge.chat_temporal import KEY, exclude_managed_candidates
from eimemory.metadata import business_metadata
from eimemory.models.records import RecallBundle, ScopeRef
from eimemory.retrieval.contracts import CandidateRequest
from eimemory.retrieval.sqlite_source import SQLiteCandidateSource


OLD = "自2020-01-01起，石湾项目预算是14万元。"
NEW = "自2021-01-01起，石湾项目预算改为18万元。"
QUERY = "石湾项目预算多少钱？"
ENABLED_QUERY = "石湾项目现在的预算是多少？"
LEGACY_QUERY = "松川项目预算多少钱？"


def owner():
    return ScopeRef.from_dict(resolve_channel_scope("hermes", SCOPE))


def request(query=QUERY):
    return CandidateRequest.create(query=query, scope=owner(), source_ids=["hermes"])


def default_reader(chat):
    store = chat.runtime.store
    api = MemoryAPI(store, candidate_source=SQLiteCandidateSource(store))
    assert api._ordinary_chat_temporal is False
    return api


def seed(chat):
    old_parent = chat.say(OLD)
    new_parent = chat.say(NEW)
    records = chat.runtime.store.list_records_by_sources_exact_scope(
        scope=owner(), sources=["hermes.l1"], kinds=["memory"], limit=100,
    )
    managed = [record for record in records if KEY in business_metadata(record.meta)]
    old = next(record for record in managed if record.summary == OLD)
    new = next(record for record in managed if record.summary == NEW)
    return old_parent, new_parent, old, new


def manual_counterpart(chat):
    return default_reader(chat).ingest(
        text="松川项目预算是23万元。", title="松川项目预算是23万元。",
        memory_type="fact", scope={**asdict(owner()), "preserve_scope": True},
        source="hermes.manual", source_id="hermes", force_capture=True,
    )


def recall(api, query=QUERY):
    return api.recall(query=query, scope=asdict(owner()),
                      task_context={"source_ids": ["hermes"]}, limit=8)


def strip_marker(record):
    record.meta.pop(KEY, None)
    record.meta.get("business_meta", {}).pop(KEY, None)


def test_default_sqlite_reader_withholds_managed_bodies_without_enabling(chat):
    old_parent, new_parent, old, new = seed(chat)
    assert_evidence(chat.recall(ENABLED_QUERY), value="18万元", source_id=new_parent,
                    excluded=("14万元",))
    assert_evidence(chat.recall("在2020-06-01，石湾项目预算是多少？"),
                    value="14万元", source_id=old_parent, excluded=("18万元",))
    before = {record.record_id: record.to_dict() for record in (old, new)}
    api = default_reader(chat)
    for query in (QUERY, "在2020-06-01，石湾项目预算多少钱？"):
        bundle = recall(api, query)
        assert not {old.record_id, new.record_id} & {
            record.record_id for record in [*bundle.items, *bundle.rules, *bundle.reflections]
        }
        assert "14万元" not in json.dumps(bundle.to_dict(), ensure_ascii=False)
        assert "18万元" not in json.dumps(bundle.to_dict(), ensure_ascii=False)
    assert api._ordinary_chat_temporal is False
    for record_id, expected in before.items():
        assert chat.runtime.store.get_by_exact_ref(
            record_id, scope=owner(), source_id="hermes").to_dict() == expected


def test_default_reader_after_reopen_still_withholds_managed_history(chat):
    _, _, old, new = seed(chat)
    chat.close()
    chat.open()
    api = default_reader(chat)
    assert not {old.record_id, new.record_id} & {r.record_id for r in recall(api).items}
    assert any("18万元" in row["summary"] for row in chat.recall(ENABLED_QUERY))


def test_default_reader_preserves_unrelated_ordinary_fact(chat):
    seed(chat)
    legacy = manual_counterpart(chat)
    api = default_reader(chat)
    assert any(record.record_id == legacy.record_id for record in recall(api, LEGACY_QUERY).items)
    assert exclude_managed_candidates(api, request(LEGACY_QUERY), [legacy]) == [legacy]


@pytest.mark.parametrize("marker", [None, [], {}, "resolved"],
                         ids=["null", "list", "empty-object", "string"])
def test_malformed_marker_cannot_reenter_default_candidate_path(chat, marker):
    legacy = manual_counterpart(chat)
    malformed = deepcopy(legacy)
    malformed.meta[KEY] = marker
    api = default_reader(chat)
    resolution = api._ordinary_temporal_candidates(request(LEGACY_QUERY), [malformed, legacy])
    assert list(resolution.items) == [legacy]
    assert not resolution.matches


def test_stripped_marker_does_not_hide_exact_journal_membership(chat):
    _, _, old, _ = seed(chat)
    stripped = deepcopy(old)
    strip_marker(stripped)
    # No persisted evidence/digest is edited. The deterministic journal lookup
    # must recognize this stale/scrubbed candidate independently of its marker.
    assert KEY not in business_metadata(stripped.meta)
    assert not exclude_managed_candidates(default_reader(chat), request(), [stripped])


def test_unjournaled_source_bound_dated_body_is_withheld_without_marker(chat):
    _, _, old, _ = seed(chat)
    unjournaled = deepcopy(old)
    unjournaled.record_id = "mem_51fa42b975df43e99a7cf4ac5d129a01"
    strip_marker(unjournaled)
    unjournaled.meta["semantic_key"] = "chat-observation:" + unjournaled.record_id
    chat.runtime.store.append(unjournaled)
    assert not exclude_managed_candidates(default_reader(chat), request(), [unjournaled])


def test_default_guard_keeps_undated_unmanaged_chat_l1(chat):
    chat.say("松川项目预算是23万元。")
    records = chat.runtime.store.list_records_by_sources_exact_scope(
        scope=owner(), sources=["hermes.l1"], kinds=["memory"], limit=100,
    )
    legacy = next(record for record in records if "23万元" in record.summary)
    assert KEY not in business_metadata(legacy.meta)
    api = default_reader(chat)
    assert exclude_managed_candidates(api, request(LEGACY_QUERY), [legacy]) == [legacy]
    assert any(record.record_id == legacy.record_id for record in recall(api, LEGACY_QUERY).items)


def test_marker_free_explicit_undated_legacy_l1_has_no_invented_temporal_family(chat):
    text = "host-008's sector is North."
    chat.say(text)
    records = chat.runtime.store.list_records_by_sources_exact_scope(
        scope=owner(), sources=["hermes.l1"], kinds=["memory"], limit=100,
    )
    unresolved = next(record for record in records if record.summary == text)
    assert business_metadata(unresolved.meta)[KEY]["state"] == "unresolved"
    legacy = deepcopy(unresolved)
    strip_marker(legacy)
    api = default_reader(chat)
    query = request("What is host-008's sector?")
    # A current unresolved managed observation remains withheld. A plausible
    # preintegration copy has neither a journal nor a dated original clause.
    assert not exclude_managed_candidates(api, query, [unresolved])
    assert exclude_managed_candidates(api, query, [legacy]) == [legacy]


def test_marker_free_undated_clause_survives_another_slots_dated_source(chat):
    undated = "host-008's sector is North."
    text = "Since 2020-01-01, host-009::sector is South.\n" + undated
    chat.say(text)
    records = chat.runtime.store.list_records_by_sources_exact_scope(
        scope=owner(), sources=["hermes.l1"], kinds=["memory"], limit=100,
    )
    # L1 presentation normalizes whitespace; source binding still refers to
    # the original multiline USER text. Do not confuse display normalization
    # with absence of the captured observation.
    original = next(record for record in records
                    if " ".join(record.summary.split()) == " ".join(text.split()))
    legacy = deepcopy(original)
    strip_marker(legacy)
    legacy.title = legacy.summary = undated
    legacy.content["text"] = undated
    # Keep the exact original parent, source digest and derived_from link.
    # Only this detached legacy-shaped candidate is supplied to the boundary.
    assert exclude_managed_candidates(
        default_reader(chat), request("What is host-008's sector?"), [legacy]) == [legacy]


class BundleEngine:
    def __init__(self, store, bundle):
        self.store = store
        self.bundle = bundle

    def recall(self, request):
        return self.bundle


def test_final_default_guard_scrubs_all_lanes_and_derived_text(chat):
    _, _, old, _ = seed(chat)
    legacy = manual_counterpart(chat)
    supplied = RecallBundle(
        items=[old, legacy], rules=[old], reflections=[old], confidence=0.9,
        next_action_hint=old.summary,
        explanation={"cascade_evidence": [old.summary], "nested": {"body": old.summary}},
    )
    api = MemoryAPI(chat.runtime.store, recall_engine=BundleEngine(chat.runtime.store, supplied))
    result = recall(api)
    assert [r.record_id for r in result.items] == [legacy.record_id]
    assert not result.rules and not result.reflections
    assert not result.next_action_hint
    assert "14万元" not in json.dumps(result.to_dict(), ensure_ascii=False)
    assert api._ordinary_chat_temporal is False


def test_final_default_guard_leaves_unrelated_bundle_unchanged(chat):
    legacy = manual_counterpart(chat)
    supplied = RecallBundle(items=[legacy], rules=[], reflections=[], confidence=0.8,
                            next_action_hint="Use the admitted ordinary fact.",
                            explanation={"ordinary": "kept"})
    api = MemoryAPI(chat.runtime.store, recall_engine=BundleEngine(chat.runtime.store, supplied))
    assert recall(api, LEGACY_QUERY).to_dict() == supplied.to_dict()


def test_evolution_dataset_uses_guarded_default_reader(chat, monkeypatch):
    _, _, old, new = seed(chat)
    legacy = manual_counterpart(chat)
    observed = []
    original = MemoryAPI.recall

    def track(self, **kwargs):
        observed.append(self._ordinary_chat_temporal)
        return original(self, **kwargs)

    monkeypatch.setattr(MemoryAPI, "recall", track)
    result = EvolutionAPI(chat.runtime.store).evaluate_recall_dataset(
        scope=asdict(owner()), dataset=[
            {"query": QUERY, "expect_any_record_id": [old.record_id],
             "task_context": {"source_ids": ["hermes"]}},
            {"query": LEGACY_QUERY, "expect_any_record_id": [legacy.record_id],
             "task_context": {"source_ids": ["hermes"]}},
        ],
    )
    assert observed == [False, False]
    assert result["samples"][0]["hit"] is False
    assert not {old.record_id, new.record_id} & set(result["samples"][0]["returned_record_ids"])
    assert result["samples"][1]["hit"] is True


def test_evolution_replay_uses_guarded_default_reader(chat, monkeypatch):
    _, _, old, new = seed(chat)
    legacy = manual_counterpart(chat)
    evolution = EvolutionAPI(chat.runtime.store)
    rule = evolution.store_rule(title="Synthetic reader baseline", summary="Read available facts.",
                                task_type="", retrieval_policy={}, scope=asdict(owner()))
    observed = []
    original = MemoryAPI.recall

    def track(self, **kwargs):
        result = original(self, **kwargs)
        observed.append((self._ordinary_chat_temporal, {r.record_id for r in result.items}))
        return result

    monkeypatch.setattr(MemoryAPI, "recall", track)
    report = evolution.replay_rule(record_id=rule.record_id, dataset=[
        {"query": QUERY, "scope": asdict(owner()), "expect_any_title": [old.title],
         "task_context": {"source_ids": ["hermes"]}},
        {"query": LEGACY_QUERY, "scope": asdict(owner()), "expect_any_title": [legacy.title],
         "task_context": {"source_ids": ["hermes"]}},
    ])
    assert [enabled for enabled, _ in observed] == [False, False]
    assert not {old.record_id, new.record_id} & observed[0][1]
    assert legacy.record_id in observed[1][1]
    assert business_metadata(report.meta)["pass_rate"] == 0.5


def test_enabled_future_and_late_history_remain_meaningful(chat):
    old_parent, new_parent, _, _ = seed(chat)
    future = chat.say("自2099-01-01起，石湾项目预算改为22万元。")
    late = chat.say("从2010-01-01到2011-01-01，石湾项目预算是6万元。")
    for query, value, parent, excluded in (
        (ENABLED_QUERY, "18万元", new_parent, ("14万元", "22万元", "6万元")),
        ("在2020-06-01，石湾项目预算是多少？", "14万元", old_parent, ("18万元", "22万元")),
        ("在2010-06-01，石湾项目预算是多少？", "6万元", late, ("14万元", "18万元", "22万元")),
        ("在2099-06-01，石湾项目预算是多少？", "22万元", future, ("14万元", "18万元")),
    ):
        assert_evidence(chat.recall(query), value=value, source_id=parent, excluded=excluded)
        assert not recall(default_reader(chat), query).items


def test_unpatched_legacy_status_and_export_contract_still_exposes_active_bodies(chat):
    """Document the remaining blocker, never claim true old-binary safety.

    These lifecycle/export helpers are unchanged from the reviewed git base.
    They protect inactive journals but cannot classify active managed bodies.
    """
    from eimemory.recall.indexing import is_inactive_or_superseded_record
    from eimemory.storage.record_export import should_export_record

    _, _, old, new = seed(chat)
    active = chat.runtime.store.list_records(kinds=["memory"], scope=owner(),
                                            source_ids=["hermes"], status="active", limit=100)
    assert {old.record_id, new.record_id} <= {record.record_id for record in active}
    assert not is_inactive_or_superseded_record(old)
    assert should_export_record(old)
    journals = chat.runtime.store.list_records_by_sources_exact_scope(
        scope=owner(), sources=["hermes.chat_temporal"], kinds=["unknown"], limit=100)
    assert journals
    assert all(is_inactive_or_superseded_record(record) for record in journals)
    assert not any(should_export_record(record) for record in journals)
    assert not recall(default_reader(chat)).items
