"""Five exposed parent regressions and paired identity/boundary negatives.

These are seen synthetic fixtures, not sealed evaluation or production data.
"""
from copy import deepcopy

import pytest

from eimemory.raw.retrieval import rerank_raw_results


CASES = [
    pytest.param(
        "现在白鹭泵站采用什么值班表？",
        "白鹭泵站值班表采用双班制，每班十二小时。",
        "白鹭泵站值班表纠正：不再采用双班制，改为三班制，每班八小时。",
        .7, .6, "2023-01-01", "2025-01-01", True, id="seen-pv06"),
    pytest.param(
        "What is the current expiry rule for Vireo access passes?",
        "Vireo access passes expire after 14 days. The expiry rule is applied at the entrance.",
        "Vireo access passes changed: they now expire after 21 days instead of 14 days. "
        "This corrects the Vireo expiry rule.",
        .72, .62, "2023-02-01", "2025-02-01", True, id="seen-pv07"),
    pytest.param(
        "现在云杉仓库的开门时间是什么？", "云杉仓库开门时间是每天上午八点。",
        "云杉仓库开门时间更正：不再是上午八点，改为上午九点。",
        .7, .6, "2023-01-01", "2025-01-01", True, id="seen-rm01"),
    pytest.param(
        "现在青石站的应急联系人是谁？", "青石站的应急联系人是林工。",
        "更正：前面登记错了，青石站的应急联系人是许工，不是林工。",
        .7, .6, "2023-04-01", "2025-04-01", True, id="seen-rm03"),
    pytest.param(
        'What is the current owner of "Granite Bay"?', 'The current owner of "Granite Bay" is Rhea.',
        'Correction: the owner of "East Granite Bay" is now Mira, not Theo.',
        .62, .08, "2024-08-01", "2025-08-01", False, id="seen-rm07"),
]


def row(name, text, base, day, **extra):
    return {"record": {"record_id": name, "kind": "raw_chunk", "text": text,
                       "source_id": "synthetic", "scope": {"user_id": "synthetic"},
                       "occurred_at": day + "T00:00:00Z", **extra}, "base_score": base}


@pytest.mark.parametrize("query,old,new,old_base,new_base,old_day,new_day,changed", CASES)
def test_seen_directed_identity_failures(query, old, new, old_base, new_base, old_day, new_day, changed):
    inputs = [row("old", old, old_base, old_day), row("new", new, new_base, new_day)]
    before = deepcopy(inputs)
    ranked = rerank_raw_results(query=query, results=inputs)
    assert inputs == before
    assert [item["record"]["record_id"] for item in ranked] == (["new", "old"] if changed else ["old", "new"])
    assert ranked[0]["final_score"] > ranked[1]["final_score"]
    if changed:
        assert ranked[0]["boosts"]["correction_marker"] == .95
        assert ranked[0]["record"]["text"] == new
    else:
        assert not ranked[1]["boosts"].get("quoted_phrase")
        assert not ranked[1]["boosts"].get("correction_marker")
        assert not ranked[1]["boosts"].get("current_fact")
        assert not ranked[1]["boosts"].get("temporal_currentness")


@pytest.mark.parametrize("query,old,new", [
    pytest.param("现在白鹭泵站采用什么值班表？",
        "白鹭泵站值班表采用双班制，每班十二小时。",
        "白鹭泵站值班表采用双班制。白鹭泵站检修表纠正：不再采用双班制，改为三班制。",
        id="header-other-attribute"),
    pytest.param("What is the current expiry rule for Vireo access passes?",
        "Vireo access passes expire after 14 days. The expiry rule is applied at the entrance.",
        "Vireo access passes expire after 14 days. Sora access passes changed: they now expire after 21 days. "
        "This corrects the Sora expiry rule.", id="restatement-other-project"),
    pytest.param("现在云杉仓库的开门时间是什么？", "云杉仓库开门时间是每天上午八点。",
        "云杉仓库开门时间是每天上午八点。白桦仓库开门时间更正：不再是上午八点，改为上午九点。",
        id="copula-other-project"),
    pytest.param("现在青石站的应急联系人是谁？", "青石站的应急联系人是林工。",
        "青石站的应急联系人是林工。更正：前面登记错了，青石站的采购联系人是许工，不是林工。",
        id="preamble-other-attribute"),
    pytest.param('What is the current owner of "Granite Bay"?', 'The current owner of "Granite Bay" is Rhea.',
        'The owner of "Granite Bay" is Rhea. Correction: the owner of "East Granite Bay" is currently Mira.',
        id="quoted-mention-cannot-lend-currentness"),
])
def test_paired_identity_and_attribute_negatives(query, old, new):
    ranked = rerank_raw_results(query=query, results=[
        row("old", old, .7, "2023-01-01"), row("new", new, .1, "2025-01-01")])
    assert ranked[0]["record"]["record_id"] == "old"
    assert not any(item["boosts"].get("correction_marker") for item in ranked)
    assert not any(item["boosts"].get("current_fact") for item in ranked)


@pytest.mark.parametrize("new", [
    "Vireo access passes expire after 14 days. This corrects the Vireo expiry rule.",
    "Vireo access passes changed: they now expire after 21 days. This corrects the Sora expiry rule.",
    "Vireo access passes changed: they now expire after 21 days.\nThis corrects the Vireo expiry rule.",
    "Vireo access passes changed: they now expire after 21 days. Vireo expiry rule changed to a new format.",
], ids=["unmarked-mention", "other-identity", "cross-line", "independent-attribute"])
def test_change_restatement_requires_adjacent_asserted_same_identity(new):
    ranked = rerank_raw_results(query="What is the current expiry rule for Vireo access passes?", results=[
        row("old", "Vireo access passes expire after 14 days. The expiry rule is applied at the entrance.",
            .72, "2023-02-01"), row("new", new, .62, "2025-02-01")])
    assert ranked[0]["record"]["record_id"] == "old"
    assert not any(item["boosts"].get("correction_marker") for item in ranked)


@pytest.mark.parametrize("query,old,new", [
    pytest.param('"What Is" backend', '"What Is" backend uses SQLite.',
        'Correction: "What Is" backend changed to MySQL.', id="english-question-name"),
    pytest.param('"是什么"后端', '"是什么"后端使用 SQLite。',
        '更正："是什么"后端改为 MySQL。', id="chinese-question-name"),
])
def test_interrogative_syntax_does_not_erase_quoted_names(query, old, new):
    ranked = rerank_raw_results(query=query, results=[
        row("old", old, .9, "2023-01-01"), row("new", new, .1, "2025-01-01")])
    assert ranked[0]["record"]["record_id"] == "new"
    assert ranked[0]["boosts"]["correction_marker"] == .95


@pytest.mark.parametrize("metadata", [False, True], ids=["text", "metadata"])
def test_quoted_mention_cannot_lend_identity_to_another_current_assertion(metadata):
    ranked = rerank_raw_results(query='What is the current owner of "Granite Bay"?', results=[
        row("old", 'The owner of "Granite Bay" is Rhea.', .7, "2023-01-01"),
        row("new", 'The owner of "Granite Bay" was Rhea. '
            'The owner of "East Granite Bay" is currently Mira.', .1, "2025-01-01", current=metadata)])
    assert ranked[0]["record"]["record_id"] == "old"
    assert not any(item["boosts"].get("current_fact") for item in ranked)


def test_change_restatement_identity_does_not_require_query_capitalization():
    ranked = rerank_raw_results(query="current expiry rule for vireo access passes", results=[
        row("old", "Vireo access passes expire after 14 days. The expiry rule is applied at the entrance.",
            .72, "2023-02-01"),
        row("new", "Vireo access passes changed: they now expire after 21 days instead of 14 days. "
            "This corrects the Vireo expiry rule.", .62, "2025-02-01")])
    assert ranked[0]["record"]["record_id"] == "new"
    assert ranked[0]["boosts"]["correction_marker"] == .95
