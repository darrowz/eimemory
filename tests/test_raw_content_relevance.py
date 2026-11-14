"""Synthetic raw ranking cases; no external corpus or answer model."""
from copy import deepcopy

import pytest

from eimemory.raw.retrieval import rerank_raw_results


def candidate(name, text, score, day=None, **extra):
    record = {"record_id": name, "kind": "raw_chunk", "text": text, **extra}
    if day:
        record["occurred_at"] = day + "T00:00:00Z"
    return {"record": record, "base_score": score}


def ids(ranked):
    return [item["record"]["record_id"] for item in ranked]


def test_content_answer_beats_unrelated_newer_correction():
    ranked = rerank_raw_results(
        query="Which screen states must a design handoff include?",
        results=[
            candidate("answer", "Design handoff: screen states include loading, empty and error.", .4993, "2026-01-01"),
            candidate("distractor", "Interview process: use a panel instead of individual calls.", .009618, "2026-02-01"),
        ],
    )
    assert ids(ranked) == ["answer", "distractor"]


def test_later_same_subject_correction_survives_lower_base():
    ranked = rerank_raw_results(query="Which archive backend is used?", results=[
        candidate("old", "Archive backend is SQLite.", .9, "2026-01-01"),
        candidate("new", "Correction: archive backend changed to PostgreSQL.", .1, "2026-02-01"),
        candidate("other", "Correction: interview process now uses a panel instead of calls.", .8, "2026-03-01"),
    ])
    assert ids(ranked) == ["new", "old", "other"]


@pytest.mark.parametrize("query,answer,distractor", [
    ("How do we use the archive backend?", "Archive backend accepts immutable snapshots.",
     "We use the garden pump and make it run; I prefer it currently instead of buckets."),
    ("Which backups must be retained?", "A backup must be retained for thirty days.",
     "I prefer a new chair instead of the old chair."),
    ("灯塔项目交接需要哪些页面状态？", "灯塔项目交接需要加载、空白和错误页面状态。",
     "更正：晚餐改为米饭，不再用面条。"),
    ("Cache_Key的TTL规则是什么？", "Cache_Key的TTL规则是三十秒。",
     "Other_Key的版本更正为TTL十秒。"),
    ("What does the 'amber switch' control?", "The amber switch controls ventilation.",
     "The amber badge and a switch control the new display instead of the old display."),
    ("「蓝鲸开关」控制什么？", "「蓝鲸开关」控制通风。", "蓝鲸图案与开关已更正。"),
    ("Which archive retention policy applies?", "The archive policy keeps snapshots for thirty days.",
     "Correction: lunch preference is soup instead of noodles."),
])
def test_content_words_identifiers_quotes_and_partial_paraphrase(query, answer, distractor):
    ranked = rerank_raw_results(query=query, results=[
        candidate("distractor", distractor, .9, "2026-03-01", current=True),
        candidate("answer", answer, .2, "2026-01-01"),
    ])
    assert ids(ranked)[0] == "answer"


@pytest.mark.parametrize("query,old,new", [
    ("灯塔项目归属", "灯塔项目归属甲组。", "更正：灯塔项目归属乙组，不属于甲组。"),
    ("Cache_Key TTL", "Cache_Key TTL is sixty seconds.", "Correction: Cache_Key TTL changed to thirty seconds."),
    ('"amber switch" setting', "The amber switch setting is low.",
     "Correction: the amber switch setting changed to high."),
    ("archival backend", "Archival backend is SQLite.", "Archival backend migrated to PostgreSQL."),
])
def test_dated_topic_corrections_across_languages(query, old, new):
    ranked = rerank_raw_results(query=query, results=[
        candidate("old", old, .95, "2026-01-01"),
        candidate("new", new, .01, "2026-02-01"),
    ])
    assert ids(ranked) == ["new", "old"]
    assert ranked[0]["boosts"]["correction_marker"] > 0
    assert ranked[0]["final_score"] > ranked[1]["final_score"]


@pytest.mark.parametrize("old_day,new_day", [(None, None), (None, "2026-02-01"),
    ("2026-01-01", None), ("2026-01-01", "2026-01-01"), ("2026-02-01", "2026-01-01")])
def test_ambiguous_dates_or_earlier_correction_do_not_assert_new_order(old_day, new_day):
    ranked = rerank_raw_results(query="archive backend", results=[
        candidate("old", "Archive backend is SQLite.", .9, old_day),
        candidate("new", "Correction: archive backend changed to PostgreSQL.", .1, new_day),
    ])
    assert ids(ranked) == ["old", "new"]
    assert not any(item["boosts"].get("correction_marker") for item in ranked)


@pytest.mark.parametrize("new_text", [
    "Archive backend is PostgreSQL.\nCorrection: interview process changed to panels.",
    "Archive backend is PostgreSQL.\nCorrection: garden backend changed to files.",
    "Archive backend is PostgreSQL.\nInstead of salt, use pepper.",
])
def test_correction_line_must_name_shared_subject(new_text):
    ranked = rerank_raw_results(query="archive backend", results=[
        candidate("old", "Archive backend is SQLite.", .9, "2026-01-01"),
        candidate("new", new_text, .1, "2026-02-01"),
    ])
    assert ids(ranked) == ["old", "new"]


@pytest.mark.parametrize("query", [
    "As of 2026-01-15, which archive backend was used?",
    "Before 2026-02-01, which archive backend was used?",
    "截至2026年1月15日 archive backend 是什么？",
])
def test_as_of_retains_date_relevant_old_evidence(query):
    ranked = rerank_raw_results(query=query, results=[
        candidate("new", "Correction: archive backend changed to PostgreSQL.", .9, "2026-02-01"),
        candidate("old", "Archive backend is SQLite.", .1, "2026-01-01"),
    ])
    assert ids(ranked) == ["old", "new"]
    assert ranked[0]["boosts"].get("historical_window", 0) > 0


def test_as_of_allows_a_real_correction_that_already_happened():
    ranked = rerank_raw_results(query="archive backend as of 2026-03-01", results=[
        candidate("old", "Archive backend is SQLite.", .9, "2026-01-01"),
        candidate("new", "Correction: archive backend changed to PostgreSQL.", .1, "2026-02-01"),
    ])
    assert ids(ranked) == ["new", "old"]


@pytest.mark.parametrize("query", ["Historical archive backend", "Previously, which archive backend?",
    "archive backend in 2025", "过去的 archive backend 是什么？"])
def test_historical_without_exact_cutoff_does_not_blindly_select_newest(query):
    ranked = rerank_raw_results(query=query, results=[
        candidate("old", "Archive backend is SQLite.", .9, "2026-01-01"),
        candidate("new", "Correction: archive backend changed to PostgreSQL.", .1, "2026-02-01", current=True),
    ])
    assert ids(ranked) == ["old", "new"]


@pytest.mark.parametrize("query", ["", "what is the and how do we use it", "unknownquasar"])
def test_no_topic_evidence_preserves_base_ranking_and_candidates(query):
    ranked = rerank_raw_results(query=query, results=[
        candidate("base", "Original unmarked evidence.", .7),
        candidate("markers", "I prefer soup currently instead of noodles; correction.", .1, "2026-02-01", current=True),
    ])
    assert ids(ranked) == ["base", "markers"]
    assert not any(item["boosts"].get("correction_marker") for item in ranked)
    assert rerank_raw_results(query=query, results=[]) == []


@pytest.mark.parametrize("partition", ["source_id", "scope"])
def test_local_correction_cannot_cross_source_or_scope_partitions(partition):
    left = "alpha" if partition == "source_id" else {"user_id": "alpha"}
    right = "beta" if partition == "source_id" else {"user_id": "beta"}
    ranked = rerank_raw_results(query="archive backend", results=[
        candidate("old", "Archive backend is SQLite.", .9, "2026-01-01", **{partition: left}),
        candidate("new", "Correction: archive backend changed to PostgreSQL.", .1, "2026-02-01", **{partition: right}),
    ])
    assert ids(ranked) == ["old", "new"]


def test_storage_write_dates_do_not_supply_missing_fact_dates():
    ranked = rerank_raw_results(query="archive backend", results=[
        candidate("old", "Archive backend is SQLite.", .9, updated_at="2026-01-01"),
        candidate("new", "Correction: archive backend changed to PostgreSQL.", .1, updated_at="2026-02-01"),
    ])
    assert ids(ranked) == ["old", "new"]


def test_original_payload_and_inputs_are_preserved():
    text = "  Archive backend is SQLite.\nSecond original line.  "
    inputs = [candidate("one", text, .3, "2026-01-01", title="Archive settings", source_id="fixture")]
    before = deepcopy(inputs)
    ranked = rerank_raw_results(query="archive backend", results=inputs)
    assert inputs == before
    assert ranked[0]["record"]["text"] == text
    assert ranked[0]["record"]["source_id"] == "fixture"
    assert ranked[0]["base_score"] == .3
    assert set(ranked[0]) == {"record", "base_score", "final_score", "boosts"}


def test_metadata_question_preserves_identifier_and_quoted_phrase():
    ranked = rerank_raw_results(query="setting", task_context={"question": 'Which Cache_Key has "amber switch" setting?'}, results=[
        candidate("near", "Other_Key: amber badge and switch setting.", .9),
        candidate("exact", "Cache_Key has amber switch setting.", .1),
    ])
    assert ids(ranked) == ["exact", "near"]


def test_huge_finite_mixed_provider_score_cannot_override_content_relevance():
    ranked = rerank_raw_results(query="archive backend", results=[
        candidate("unrelated", "Garden plans", 1e300),
        candidate("related", "Archive backend is SQLite.", .01),
    ])
    assert ids(ranked) == ["related", "unrelated"]


def test_fullwidth_identifier_and_function_word_case_are_normalized():
    ranked = rerank_raw_results(query="WHAT IS Ｃａｃｈｅ＿Ｋｅｙ TTL?", results=[
        candidate("other", "Other_Key TTL is what it is; currently corrected.", .9, "2026-03-01"),
        candidate("exact", "Cache_Key TTL is thirty seconds.", .1, "2026-01-01"),
    ])
    assert ids(ranked) == ["exact", "other"]


def test_two_subjects_cannot_share_a_generic_action_as_correction_evidence():
    ranked = rerank_raw_results(query="Which archive backend should we use?", results=[
        candidate("old", "Archive backend uses SQLite.", .9, "2026-01-01"),
        candidate("other", "We use a garden pump instead of buckets.", .1, "2026-02-01"),
    ])
    assert ids(ranked) == ["old", "other"]
    assert not ranked[1]["boosts"].get("correction_marker")


def test_same_day_with_different_clock_times_is_still_ambiguous():
    first = candidate("old", "Archive backend is SQLite.", .9)
    other = candidate("new", "Correction: archive backend changed to PostgreSQL.", .1)
    first["record"]["occurred_at"] = "2026-01-01T01:00:00Z"
    other["record"]["occurred_at"] = "2026-01-01T23:00:00Z"
    assert ids(rerank_raw_results(query="archive backend", results=[first, other])) == ["old", "new"]


def test_same_topic_newer_fact_without_change_or_current_assertion_keeps_base_order():
    ranked = rerank_raw_results(query="archive backend", results=[
        candidate("old", "Archive backend is SQLite.", .9, "2026-01-01"),
        candidate("new", "Archive backend is PostgreSQL.", .1, "2026-02-01"),
    ])
    assert ids(ranked) == ["old", "new"]


def test_preference_and_current_assertions_cannot_displace_other_subjects():
    ranked = rerank_raw_results(query="archive backend", results=[
        candidate("old", "Archive backend is SQLite.", .1, "2026-01-01"),
        candidate("other", "I prefer garden pumps currently.", .9, "2026-02-01", current=True),
    ])
    assert ids(ranked) == ["old", "other"]


def test_content_statistics_only_see_hydrated_active_allowed_candidates(tmp_path, monkeypatch):
    from contextlib import closing
    from eimemory.models.records import RecordEnvelope, ScopeRef
    from eimemory.raw import retrieval
    from eimemory.storage.runtime_store import RuntimeStore

    scope = ScopeRef(tenant_id="fixture", agent_id="fixture", workspace_id="fixture", user_id="owner")
    with closing(RuntimeStore(tmp_path)) as store:
        def put(name, *, source="alpha", row_scope=scope, status="active"):
            return store.append(RecordEnvelope.create(kind="raw_chunk", title=name, scope=row_scope,
                source_id=source, status=status, content={"raw_text": name + " archive backend evidence"}))
        target = put("allowed")
        foreign_source = put("wrong-source", source="beta")
        foreign_scope = put("wrong-scope", row_scope=ScopeRef(tenant_id="other"))
        inactive = put("inactive", status="superseded")
        forged = deepcopy(target)
        forged.content["raw_text"] = "Forged correction and current claim"
        monkeypatch.setattr(retrieval, "_raw_api_search", lambda *args, **kwargs: [
            {"record": row, "base_score": .9} for row in (forged, foreign_source, foreign_scope, inactive)
        ])
        seen = []
        original = retrieval.match_content
        def observe(plan, text, title=""):
            seen.append(text)
            return original(plan, text, title)
        monkeypatch.setattr(retrieval, "match_content", observe)
        ranked = retrieval.search_raw_chunks(store, query="archive backend", scope=scope,
            source_ids=["alpha"], task_context={"exact_scope_only": True}, limit=1)
        assert seen == [target.content["raw_text"]]
        assert ids(ranked) == [target.record_id]
        assert ranked[0]["record"]["text"] == target.content["raw_text"]


def test_authority_snapshot_is_rechecked_after_content_ranking(tmp_path, monkeypatch):
    from contextlib import closing
    from eimemory.models.records import RecordEnvelope, ScopeRef
    from eimemory.raw import retrieval
    from eimemory.raw.boundary import RawRecallUnavailable
    from eimemory.storage.runtime_store import RuntimeStore

    scope = ScopeRef(user_id="fixture")
    with closing(RuntimeStore(tmp_path)) as store:
        target = store.append(RecordEnvelope.create(kind="raw_chunk", title="Archive backend",
            scope=scope, content={"raw_text": "Archive backend is SQLite."}))
        monkeypatch.setattr(retrieval, "_raw_api_search", lambda *args, **kwargs: [{"record": target, "base_score": .9}])
        changed = deepcopy(target)
        changed.content["raw_text"] = "Archive backend changed during ranking."
        original = retrieval.match_content
        def mutate_after_snapshot(plan, text, title=""):
            monkeypatch.setattr(store, "get_by_exact_ref", lambda *args, **kwargs: changed)
            return original(plan, text, title)
        monkeypatch.setattr(retrieval, "match_content", mutate_after_snapshot)
        with pytest.raises(RawRecallUnavailable):
            retrieval.search_raw_chunks(store, query="archive backend", scope=scope, limit=1)


@pytest.mark.parametrize("query,old,new", [
    pytest.param("Mira snapshot retention policy", "Mira snapshot retention policy is thirty days.",
                 "Correction: Niko snapshot retention policy changed to ten days.", id="named-entity"),
    pytest.param("松林项目快照保留策略", "松林项目快照保留策略是三十天。",
                 "更正：竹林项目快照保留策略改为十天。", id="cjk-entity"),
    pytest.param("incident 8 resolution", "Incident 8 resolution is pending.",
                 "Correction: incident 9 resolution changed to completed.", id="single-digit-id"),
])
def test_correction_retains_every_anchor_content_term(query, old, new):
    ranked = rerank_raw_results(query=query, results=[
        candidate("old", old, .9, "2026-01-01"),
        candidate("other", new, .1, "2026-02-01"),
    ])
    assert ids(ranked) == ["old", "other"]
    assert not ranked[1]["boosts"].get("correction_marker")


@pytest.mark.parametrize("phrase,wrong", [
    pytest.param("Éva", "Séva", id="unicode-name"),
    pytest.param("青松 团队", "深青松 团队", id="cjk-multiword"),
    pytest.param("青松 团队", "青松 团队组", id="cjk-suffix"),
])
def test_quoted_entity_requires_complete_unicode_boundaries(phrase, wrong):
    ranked = rerank_raw_results(query=f'"{phrase}" snapshot policy', results=[
        candidate("old", f"{phrase}: snapshot policy is thirty days.", .9, "2026-01-01"),
        candidate("other", f"Correction: {wrong} snapshot policy changed to ten days.", .1, "2026-02-01"),
    ])
    assert ids(ranked) == ["old", "other"]
    assert ranked[0]["boosts"]["quoted_phrase"] > 0
    assert not ranked[1]["boosts"].get("quoted_phrase")


def test_complete_quote_can_follow_an_overlapping_substring_match():
    ranked = rerank_raw_results(query='"red red" signal', results=[
        candidate("other", "Infrared red ready signal is active.", .9),
        candidate("exact", "Infrared red red signal is active.", .1),
    ])
    assert ids(ranked) == ["exact", "other"]
    assert ranked[0]["boosts"]["quoted_phrase"] > 0
    assert not ranked[1]["boosts"].get("quoted_phrase")


def test_numeric_only_query_keeps_single_digit_identity_and_correction():
    ranked = rerank_raw_results(query="8", results=[
        candidate("other", "Incident 9 was completed.", .95, "2026-03-01"),
        candidate("old", "Incident 8 is pending.", .9, "2026-01-01"),
        candidate("new", "Correction: incident 8 changed to completed.", .1, "2026-02-01"),
    ])
    assert ids(ranked) == ["new", "old", "other"]


@pytest.mark.parametrize("query", [
    pytest.param("When is the snapshot export scheduled?", id="current-schedule"),
    pytest.param("Which SPEC2007 snapshot export schedule applies?", id="letter-year-id"),
    pytest.param("Which issue-2024 snapshot export schedule applies?", id="hyphen-year-id"),
])
def test_current_time_question_and_year_identifiers_allow_correction(query):
    ranked = rerank_raw_results(query=query, results=[
        candidate("old", "SPEC2007 issue-2024 snapshot export schedule is scheduled at noon.", .9, "2026-01-01"),
        candidate("new", "Correction: SPEC2007 issue-2024 snapshot export schedule is scheduled at dusk.", .1, "2026-02-01"),
    ])
    assert ids(ranked) == ["new", "old"]


@pytest.mark.parametrize("query", [
    pytest.param("When was the snapshot export scheduled?", id="past-tense"),
    pytest.param("snapshot export in 2024", id="standalone-year"),
    pytest.param("snapshot export in 2024-11", id="year-month"),
    pytest.param("snapshot export as of 2026-02-30", id="invalid-date"),
    pytest.param("snapshot export between 2026-01-15 and 2026-02-15", id="date-interval"),
])
def test_historical_or_ambiguous_dates_do_not_assert_current_correction(query):
    ranked = rerank_raw_results(query=query, results=[
        candidate("old", "Snapshot export is scheduled at noon.", .9, "2026-01-01"),
        candidate("new", "Correction: snapshot export is scheduled at dusk.", .1, "2026-02-01"),
    ])
    assert ids(ranked) == ["old", "new"]


def test_correction_line_retains_entity_even_when_document_matches():
    ranked = rerank_raw_results(query="Mira snapshot retention policy", results=[
        candidate("old", "Mira snapshot retention policy is thirty days.", .9, "2026-01-01"),
        candidate("other", "Mira snapshot retention policy is thirty days.\n"
                  "Correction: Niko snapshot retention policy changed to ten days.", .1, "2026-02-01"),
    ])
    assert ids(ranked) == ["old", "other"]
    assert not ranked[1]["boosts"].get("correction_marker")


def test_partial_query_and_plural_forms_still_allow_same_topic_correction():
    ranked = rerank_raw_results(query="snapshot retention policy", results=[
        candidate("old", "Snapshot policy is thirty days.", .9, "2026-01-01"),
        candidate("new", "Correction: snapshots policy changed to ten days.", .1, "2026-02-01"),
    ])
    assert ids(ranked) == ["new", "old"]


@pytest.mark.parametrize("context_key", ["query", "metadata_question"])
def test_unmatched_quotes_preserve_payload_and_later_complete_phrases(context_key):
    question = "“" * 100_000 + "\n'Mira team' snapshot policy"
    text = "  'Mira team' snapshot policy remains thirty days.\nOriginal second line.  "
    inputs = [candidate("exact", text, .1), candidate("other", "Niko team snapshot policy.", .9)]
    before = deepcopy(inputs)
    query = question if context_key == "query" else "snapshot policy"
    context = {"metadata_question": question} if context_key == "metadata_question" else {}
    ranked = rerank_raw_results(query=query, task_context=context, results=inputs)
    assert ids(ranked) == ["exact", "other"]
    assert ranked[0]["boosts"]["quoted_phrase"] > 0
    assert ranked[0]["record"]["text"] == text
    assert inputs == before
