"""Existing-source classification; synthetic original USERs, no model authority."""
from copy import deepcopy
from datetime import datetime, timezone
from time import perf_counter
from uuid import uuid4

import pytest

from test_ordinary_chat_temporal_semantics import chat
from test_chat_temporal_reader_boundary import owner, request, seed, strip_marker
from eimemory.knowledge import chat_temporal as temporal
from eimemory.metadata import business_metadata


def legacy(chat, source, summary=None):
    parent_id = chat.say(source)
    rows = chat.runtime.store.list_records_by_sources_exact_scope(
        scope=owner(), sources=["hermes.l1"], kinds=["memory"], limit=100)
    original = next(r for r in rows if business_metadata(r.meta).get(
        "source_event_binding", {}).get("record_id") == parent_id)
    item = deepcopy(original)
    item.record_id = "mem_" + uuid4().hex
    strip_marker(item)
    if summary is not None:
        item.title = item.summary = item.content["text"] = summary
    item.meta["semantic_key"] = "legacy-test:" + item.record_id
    chat.runtime.store.append(item)
    return item, original


def selected(chat, items):
    return temporal.resolve_candidates(chat.runtime.memory, request("ordinary source lookup"),
                                       items, expand=False)


def reasons(chat, items, **kwargs):
    return temporal.current_target_temporal_reasons(
        chat.runtime.memory, items, scope=owner(),
        valid_at=kwargs.pop("valid_at", "2026-11-17T12:00:00Z"),
        deadline_at=kwargs.pop("deadline_at", perf_counter() + 5), **kwargs)


@pytest.mark.parametrize("source,summary", [
    ("Juniper's sector is North.", None),
    ("岚河项目的负责人是陈木。", None),
    ("Harbor::region is West.", "Harbor::region is West"),
    ("松台的状态是正常。", "松台的状态为正常。"),
])
def test_unmarked_bound_undated_atom_preserves_existing_route(chat, source, summary):
    item, original = legacy(chat, source, summary)
    assert business_metadata(original.meta)[temporal.KEY]["state"] == "unresolved"
    assert selected(chat, [original, item]) == [item]


@pytest.mark.parametrize("source,summary", [
    ("Juniper's sector changed to North.", None),
    ("Since 2021-03-04, Juniper's sector is North.", "Juniper's sector is North."),
    ("Maybe Juniper's sector is North.", "Juniper's sector is North."),
    ("Juniper's sector is North. Juniper's sector is North.", "Juniper's sector is North."),
    ("Juniper's sector is North and its owner is Ada.", "Juniper's sector is North."),
    ("Please confirm Juniper's sector is North.", "Juniper's sector is North."),
])
def test_retro_read_does_not_promote_actions_partial_or_uncertain_atoms(chat, source, summary):
    item, _ = legacy(chat, source, summary)
    assert selected(chat, [item]) == []


def test_question_cannot_ground_positive_summary():
    with pytest.raises(ValueError):
        temporal._legacy_undated_fact("Juniper's sector is North?", "Juniper's sector is North.")


def test_persisted_unresolved_and_invalid_binding_stay_inapplicable(chat):
    item, original = legacy(chat, "Briar's mode is quiet.")
    bad = deepcopy(item)
    bad.meta["source_event_binding"]["input_digest"] = "0" * 64
    assert selected(chat, [original, bad]) == []
    assert reasons(chat, [original, bad]) == ["source_temporal_inapplicable"] * 2


def test_current_target_eligibility_is_not_a_query_or_title_gate(chat):
    _, _, old, new = seed(chat)
    ordinary, _ = legacy(chat, "Briar's mode is quiet.")
    assert reasons(chat, [old, new, ordinary]) == ["source_temporal_inapplicable", "", ""]
    assert reasons(chat, [old, new], valid_at="2020-06-01T00:00:00Z") == ["", "source_temporal_inapplicable"]


def test_existing_family_reserves_undated_slot(chat):
    seed(chat)
    item, _ = legacy(chat, "石湾项目预算是18万元。")
    assert selected(chat, [item]) == []
    assert reasons(chat, [item]) == ["source_temporal_inapplicable"]


def test_eligibility_deadline_is_unavailable_not_inapplicable(chat):
    item, _ = legacy(chat, "Briar's mode is quiet.")
    assert reasons(chat, [item], deadline_at=perf_counter() - 1) == ["source_current_authority_unavailable"]


def test_eligibility_requires_exact_current_digest(chat):
    item, _ = legacy(chat, "Briar's mode is quiet.")
    changed = deepcopy(item)
    changed.summary = "Briar's mode is loud."
    assert reasons(chat, [changed]) == ["source_temporal_inapplicable"]


def test_disabled_reader_never_certifies_managed_current(chat):
    from test_chat_temporal_reader_boundary import default_reader
    _, _, old, new = seed(chat)
    api = default_reader(chat)
    assert temporal.current_target_temporal_reasons(
        api, [old, new], scope=owner(), valid_at=datetime.now(timezone.utc),
        deadline_at=perf_counter() + 5) == ["source_temporal_inapplicable"] * 2


@pytest.mark.parametrize("text", ["Briar's mode is quiet.", "杉湾项目预算是29万元。"])
def test_disabled_reader_preserves_unmanaged_bound_source_eligibility(chat, text):
    from test_chat_temporal_reader_boundary import default_reader
    item, _ = legacy(chat, text)
    assert temporal.current_target_temporal_reasons(default_reader(chat), [item], scope=owner(),
        valid_at="2026-11-17T12:00:00Z", deadline_at=0) == [""]


def test_legacy_parent_links_do_not_require_new_binding_marker(chat):
    item, _ = legacy(chat, "Briar's mode is quiet.")
    item.meta.pop("source_event_binding", None)
    item.meta.get("business_meta", {}).pop("source_event_binding", None)
    item.record_id = "mem_" + uuid4().hex
    chat.runtime.store.append(item)
    assert selected(chat, [item]) == [item]
    assert reasons(chat, [item]) == [""]


@pytest.mark.parametrize("marker", [None, [], {}, "resolved"])
def test_enabled_reader_does_not_reinterpret_present_malformed_marker(chat, marker):
    item, _ = legacy(chat, "Briar's mode is quiet.")
    item.meta[temporal.KEY] = marker
    assert selected(chat, [item]) == []


def override_reader(monkeypatch, api, method, replacement):
    original = api.store.read_consistent
    class Reader:
        def __init__(self, sqlite):
            self.sqlite = sqlite
        def __getattr__(self, name):
            return replacement if name == method else getattr(self.sqlite, name)
    monkeypatch.setattr(api.store, "read_consistent", lambda fn: original(lambda sqlite: fn(Reader(sqlite))))


def test_incomplete_exact_family_never_proves_legacy_absence(chat, monkeypatch):
    item, _ = legacy(chat, "Briar's mode is quiet.")
    override_reader(monkeypatch, chat.runtime.memory, "temporal_fact_family", lambda **kwargs: ([], False))
    assert selected(chat, [item]) == []
    assert reasons(chat, [item]) == ["source_current_authority_unavailable"]


def test_incomplete_managed_family_is_unavailable(chat, monkeypatch):
    _, _, _, item = seed(chat)
    override_reader(monkeypatch, chat.runtime.memory, "temporal_fact_family", lambda **kwargs: ([], False))
    assert reasons(chat, [item]) == ["source_current_authority_unavailable"]


def test_exact_read_failure_is_unavailable(chat, monkeypatch):
    item, _ = legacy(chat, "Briar's mode is quiet.")
    def unavailable(*args, **kwargs):
        raise OSError("synthetic unavailable source")
    override_reader(monkeypatch, chat.runtime.memory, "get_by_exact_ref", unavailable)
    assert selected(chat, [item]) == []
    assert reasons(chat, [item]) == ["source_current_authority_unavailable"]


def test_missing_exact_parent_is_inapplicable(chat, monkeypatch):
    item, _ = legacy(chat, "Briar's mode is quiet.")
    parent_id = business_metadata(item.meta)["source_event_binding"]["record_id"]
    original = chat.runtime.store.read_consistent
    class Reader:
        def __init__(self, sqlite):
            self.sqlite = sqlite
        def __getattr__(self, name):
            return getattr(self.sqlite, name)
        def get_by_exact_ref(self, record_id, **kwargs):
            return None if record_id == parent_id else self.sqlite.get_by_exact_ref(record_id, **kwargs)
    monkeypatch.setattr(chat.runtime.store, "read_consistent", lambda fn: original(lambda sqlite: fn(Reader(sqlite))))
    assert selected(chat, [item]) == []
    assert reasons(chat, [item]) == ["source_temporal_inapplicable"]


def test_current_target_rejects_later_unrepresented_source(chat):
    _, _, _, new = seed(chat)
    from eimemory.models.records import RecordEnvelope
    parent = RecordEnvelope.create(kind="memory", title="original", scope=owner(),
        content={"text": "User: 自2022-01-01起，石湾项目预算改为22万元。\nAssistant: Acknowledged."},
        source="hermes.memory", source_id="hermes", meta={"memory_layer": "l0",
            "capture_origin": "turn_sync", "runtime_channel": "hermes", "memory_type": "conversation"})
    chat.runtime.store.append(parent)
    assert reasons(chat, [new]) == ["source_temporal_inapplicable"]


def test_literal_title_lookup_keeps_only_exact_current_member(chat):
    _, _, old, new = seed(chat)
    api = chat.runtime.memory
    assert temporal.resolve_candidates(api, request(new.title), [new], expand=True) == [new]
    assert temporal.resolve_candidates(api, request(old.title), [old, new], expand=True) == []
    assert temporal.resolve_candidates(api, request(new.title[:-2]), [new], expand=True) == []


def test_literal_title_cannot_borrow_changed_digest_or_source(chat):
    _, _, _, new = seed(chat)
    changed = deepcopy(new)
    changed.detail = "untrusted material mutation"
    assert temporal.resolve_candidates(chat.runtime.memory, request(new.title), [changed], expand=True) == []


def test_retrospective_atom_does_not_authorize_different_content(chat):
    item, _ = legacy(chat, "Briar's mode is quiet.")
    item.content["text"] = "Briar's mode is loud."
    assert selected(chat, [item]) == []


def test_as_of_in_literal_title_does_not_grant_historical_access(chat):
    chat.say("As of 2020-01-01, Fern's region is North.")
    chat.say("Since 2021-01-01, Fern's region changed to South.")
    rows = chat.runtime.store.list_records_by_sources_exact_scope(
        scope=owner(), sources=["hermes.l1"], kinds=["memory"], limit=100)
    old = next(r for r in rows if "North" in r.summary)
    new = next(r for r in rows if "South" in r.summary)
    assert temporal.query_time(old.title)[0] is not None
    assert temporal.resolve_candidates(chat.runtime.memory, request(old.title), [old, new], expand=True) == []
    assert temporal.resolve_candidates(chat.runtime.memory,
        request("As of 2020-06-01, what is Fern's region?"), [old, new], expand=True) == [old]


def test_future_literal_title_is_not_current_evidence(chat):
    chat.say("Since 2099-01-01, Cedar's region is North.")
    rows = chat.runtime.store.list_records_by_sources_exact_scope(
        scope=owner(), sources=["hermes.l1"], kinds=["memory"], limit=100)
    item = next(r for r in rows if "North" in r.summary)
    assert temporal.resolve_candidates(chat.runtime.memory, request(item.title), [item], expand=True) == []


def test_literal_title_requires_complete_family(chat, monkeypatch):
    _, _, _, item = seed(chat)
    override_reader(monkeypatch, chat.runtime.memory, "temporal_fact_family", lambda **kwargs: ([], False))
    assert temporal.resolve_candidates(chat.runtime.memory, request(item.title), [item], expand=True) == []


def test_ordinary_shared_owner_survives_temporal_gate(chat):
    from dataclasses import replace
    from test_chat_temporal_reader_boundary import manual_counterpart
    item = manual_counterpart(chat)
    item.scope = replace(item.scope, user_id="shared-authorized-owner")
    # Authority selection already admitted this ordinary candidate. Temporal
    # applicability is not a second blanket owner restriction for all lanes.
    assert selected(chat, [item]) == [item]


@pytest.mark.parametrize("marker", ["resolved", None, [], {}])
def test_shared_owner_never_gains_managed_temporal_authority(chat, marker):
    from dataclasses import replace
    _, _, _, item = seed(chat)
    item = deepcopy(item)
    item.scope = replace(item.scope, user_id="shared-authorized-owner")
    if marker != "resolved":
        item.meta[temporal.KEY] = marker
    assert temporal.resolve_candidates(chat.runtime.memory, request(item.title), [item], expand=True) == []


def test_persisted_stripped_marker_cannot_become_ordinary_through_shared_scope(chat):
    from dataclasses import replace
    from eimemory.retrieval.contracts import CandidateRequest
    _, _, _, item = seed(chat)
    stripped = deepcopy(item)
    strip_marker(stripped)
    chat.runtime.store.rewrite(stripped)
    current = chat.runtime.store.get_by_exact_ref(item.record_id, scope=item.scope,
                                                  source_id=item.source_id, fail_on_unavailable=True)
    assert temporal.KEY not in business_metadata(current.meta)
    foreign_request = CandidateRequest.create(query=current.title,
        scope=replace(owner(), user_id="other-request-owner"), source_ids=["hermes"])
    for query in [request(current.title), foreign_request]:
        assert temporal.resolve_candidates(chat.runtime.memory, query, [current], expand=True) == []
