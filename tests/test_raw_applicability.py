"""Original-evidence applicability; synthetic examples, no network or production."""
from datetime import datetime, timezone

import pytest

from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.raw.applicability import MAX_TEXT_CHARS, assess_applicability


NOW = datetime(2026, 11, 17, tzinfo=timezone.utc)


def record(text, **kwargs):
    return RecordEnvelope.create(kind="raw_chunk", title="fixture", source="synthetic",
                                 content={"raw_text": text}, scope=ScopeRef(tenant_id="synthetic"), **kwargs)


def check(text, query="Where is the Zephyr deployment manifest stored?", **kwargs):
    return assess_applicability(record(text), query=query, now=NOW, **kwargs)


def test_unconstrained_material_is_not_a_version_claim():
    decision = check("Zephyr deployment manifests are stored in the infrastructure repository.")
    assert (decision.status, decision.required) == ("applicable", False)


@pytest.mark.parametrize("query", [
    "Where is the Zephyr v2.4 deployment manifest stored?",
    "Where is the Zephyr version 2.4 deployment manifest stored?",
    "Zephyr 版本 2.4 的部署清单存在哪里？",
])
def test_same_question_current_version_old_version_and_unknown(query):
    body = "Zephyr deployment manifests are stored in the infrastructure repository."
    assert check("Applies to versions: 2.4\n" + body, query).status == "applicable"
    old = check("Applies to versions: 2.3\n" + body, query)
    assert (old.status, old.reason) == ("not_applicable", "version_mismatch")
    unknown = check(body, query)
    assert (unknown.status, unknown.reason) == ("unknown", "version_applicability_unknown")


def test_independent_chinese_subject_and_multi_release_declaration():
    query = "青岚版本7.2的证书轮换周期是多少？"
    assert check("适用版本：7.1、v7.2\n青岚证书每30天轮换。", query).status == "applicable"
    assert check("适用版本：7.0\n青岚证书每90天轮换。", query).reason == "version_mismatch"


@pytest.mark.parametrize("declaration", ["2.x", ">=2.4", "2.4-2.9", "2.4 or later", "", "all"])
def test_unknown_version_grammar_never_means_compatible(declaration):
    result = check("Applies to versions: " + declaration + "\nZephyr stores manifests in Git.",
                   "Where does Zephyr version 2.4 store manifests?")
    assert result.status == "unknown"


def test_unlabelled_mentions_and_inventory_metadata_do_not_establish_version_support():
    item = record("The migration from v2.3 to v2.4 is documented. Zephyr manifests are stored in Git.")
    item.content["version"] = "2.4"
    item.meta["release_version"] = "2.4"
    assert assess_applicability(item, query="Where are Zephyr v2.4 manifests stored?", now=NOW).status == "unknown"


def test_canonical_body_wins_over_derived_title_and_summary():
    item = record("Zephyr stores manifests in Git.")
    item.summary = "Applies to versions: 2.4"
    item.title = "Applies to versions: 2.4"
    assert assess_applicability(item, query="Where are Zephyr v2.4 manifests stored?", now=NOW).status == "unknown"


@pytest.mark.parametrize("instant, expected, reason", [
    ("2026-01-01", "applicable", "constraints_satisfied"),
    ("2026-11-16T23:59:59Z", "applicable", "constraints_satisfied"),
    ("2026-11-17", "not_applicable", "validity_expired"),
    ("2025-12-31", "not_applicable", "not_yet_valid"),
])
def test_explicit_validity_interval_uses_requested_time_and_exclusive_end(instant, expected, reason):
    result = check("Valid from: 2026-01-01\nValid until: 2026-11-17\nZephyr manifests are stored in Git.",
                   f"As of {instant}, where are Zephyr manifests stored?")
    assert (result.status, result.reason) == (expected, reason)


def test_expired_evidence_rejected_at_pinned_current_time_even_without_temporal_question():
    assert check("Valid from: 2026-01-01\nValid until: 2026-11-17\nZephyr stores manifests in Git.").reason == "validity_expired"


def test_independent_future_and_unbounded_evidence():
    assert check("有效起始：2027-01-01\n有效截止：unbounded\n青岚证书每30天轮换。",
                 "青岚证书的轮换周期是多少？").reason == "not_yet_valid"
    assert check("Valid from: 2026-01-01\nValid until: unbounded\nCygnus rotates keys weekly.",
                 "How often does Cygnus rotate keys?").status == "applicable"


@pytest.mark.parametrize("text, reason", [
    ("Valid from: 2026-01-01", "incomplete_validity_interval"),
    ("Valid until: 2027-01-01", "incomplete_validity_interval"),
    ("Valid from: tomorrow\nValid until: unbounded", "invalid_validity_declaration"),
    ("Valid from: 2027-01-01\nValid until: 2026-01-01", "invalid_validity_interval"),
    ("Valid from: 2026-02-30\nValid until: unbounded", "invalid_validity_declaration"),
])
def test_incomplete_or_invalid_intervals_are_unknown(text, reason):
    result = check(text)
    assert (result.status, result.reason) == ("unknown", reason)


def test_storage_timestamp_and_valid_from_metadata_cannot_prove_validity():
    item = record("Zephyr stores manifests in Git.")
    item.content["valid_from"] = "2026-01-01"
    item.meta["valid_until"] = "2027-01-01"
    assert assess_applicability(item, query="As of 2026-11-17, where are Zephyr manifests stored?").reason == "time_applicability_unknown"


@pytest.mark.parametrize("kwargs", [
    {"status": "deprecated"},
    {"meta": {"mutation_state": "expired"}},
    {"meta": {"superseded_by": "replacement"}},
    {"meta": {"business_meta": {"mutation_state": "quarantined"}}},
])
def test_existing_withdrawal_contract_precedes_apparent_original_support(kwargs):
    item = record("Applies to versions: 2.4\nZephyr manifests are stored in Git.", **kwargs)
    result = assess_applicability(item, query="Where are Zephyr v2.4 manifests stored?", now=NOW)
    assert (result.status, result.reason) == ("not_applicable", "withdrawn_evidence")


@pytest.mark.parametrize("status", ["revoked", "withdrawn", "superseded", "已撤销", "已废止"])
def test_original_explicit_withdrawal_is_not_citable(status):
    assert check(f"Status: {status}\nZephyr manifests are stored in Git.").reason == "withdrawn_declaration"


def test_conflicting_declarations_and_comparison_requests_stay_unknown():
    assert check("Applies to versions: 2.4\nApplies to versions: 2.5").reason == "conflicting_applicability_declarations"
    assert check("Applies to versions: 2.4, 2.5", "Compare Zephyr v2.4 with v2.5.").reason == "ambiguous_applicability_request"


def test_explicit_request_can_supply_constraint_but_cannot_override_query():
    text = "Applies to versions: 2.4\nZephyr manifests are stored in Git."
    assert check(text, task_context={"applicability": {"version": "v2.4"}}).status == "applicable"
    assert check(text, "Where are Zephyr v2.5 manifests stored?",
                 task_context={"applicability": {"version": "2.4"}}).reason == "ambiguous_applicability_request"
    assert check(text, task_context={"applicability": {"unsupported": "yes"}}).reason == "invalid_applicability_request"


def test_bounds_are_unknown_without_scanning_unbounded_payloads():
    assert check("x" * (MAX_TEXT_CHARS + 1)).reason == "applicability_text_bound"
    assert check("small", "q" * 4097).reason == "applicability_query_bound"


@pytest.mark.parametrize("version", ["2.x", ">=2.4", "2.4-2.9", "2.4+build"])
def test_unsupported_query_constraint_cannot_become_an_unconstrained_question(version):
    result = check("Applies to versions: 2.4\nZephyr stores manifests in Git.",
                   "Where does Zephyr version " + version + " store manifests?")
    assert result.status == "unknown"


def test_ordinary_store_search_retains_historical_material(tmp_path):
    from eimemory.raw.retrieval import search_raw_chunks
    from eimemory.storage.runtime_store import RuntimeStore

    item = record("Applies to versions: 2.3\nZephyr deployment manifests are stored in the infrastructure repository.")
    store = RuntimeStore(tmp_path / "store")
    try:
        item = store.append(item)
        query = "Where are Zephyr v2.4 deployment manifests stored?"
        result = search_raw_chunks(store, query=query, scope=item.scope,
                                   task_context={"evidence_intent": "exploration"}, limit=5)
        assert item.record_id in {entry["record"]["record_id"] for entry in result}
        assert assess_applicability(item, query=query, now=NOW).reason == "version_mismatch"
    finally:
        store.close()


@pytest.mark.parametrize("query, matching, wrong, status, quote", [
    ("港湾仍在SDK 2.7，应使用哪种流接口？",
     "港湾SDK 2.x只接受回调；从3.0开始支持async迭代。", "港湾SDK 3.1默认采用async迭代。",
     "applicable", "港湾SDK 2.x只接受回调"),
    ("雪松数据库是12版，能套用15版生成列迁移吗？",
     "雪松PostgreSQL 12禁用生成列的当前迁移脚本；PostgreSQL 15的脚本允许使用。", "雪松PostgreSQL 15生成列迁移已通过。",
     "applicable", "雪松PostgreSQL 12禁用生成列的当前迁移脚本"),
    ("燧石接收第一代协议包应计算何种摘要？",
     "燧石协议v1校验和为CRC32；v2改为SHA256，不能对v1包套用v2校验。", "燧石协议v2使用SHA256。",
     "applicable", "燧石协议v1校验和为CRC32"),
    ("纸鸢使用18运行时启动监听前要注意什么？",
     "纸鸢Node 18仍需实验性文件监听开关；Node 22已经移除该开关要求。", "纸鸢Node 22无需额外监听开关。",
     "applicable", "纸鸢Node 18仍需实验性文件监听开关"),
    ("芦苇4.2设备能按128条一批推送吗？",
     "芦苇ARM固件4.2每批最多32条，固件5.0才扩至128条。", "芦苇固件5.0允许128条批次。",
     "applicable", "芦苇ARM固件4.2每批最多32条"),
    ("欧泊还没升到1.0，用户名字段怎么拼？",
     "欧泊客户端0.9必须发送旧字段user_name；1.0改为userName。", "欧泊1.0仅接受userName。",
     "unknown", "欧泊客户端0.9必须发送旧字段user_name"),
])
def test_original_chinese36_version_cases_without_changing_source_text(query, matching, wrong, status, quote):
    decision = check(matching, query)
    assert decision.status == status
    assert [matching[start:end] for start, end in decision.spans] == [quote]
    assert check(wrong, query).status == "not_applicable"


@pytest.mark.parametrize("query, text, status, quote", [
    ("青岚仍在SDK 7.4，应该如何提交？", "青岚SDK 7.x使用submit函数；从8.0开始支持send函数。",
     "applicable", "青岚SDK 7.x使用submit函数"),
    ("青岚仍在SDK 8.2，应该如何提交？", "青岚SDK 7.x使用submit函数；从8.0开始支持send函数。",
     "applicable", "从8.0开始支持send函数"),
    ("雨燕接收第三代协议包应该做什么？", "雨燕协议v3需要签名；v4改为双重签名。",
     "applicable", "雨燕协议v3需要签名"),
    ("墨竹使用24运行时，应开启哪个选项？", "墨竹Quasar 20需要旧选项；Quasar 24支持自动启动。",
     "applicable", "Quasar 24支持自动启动"),
    ("青岚还没升到3.0，怎么传字段？", "青岚客户端2.9必须发送旧字段payload；3.0改为body。",
     "unknown", "青岚客户端2.9必须发送旧字段payload"),
])
def test_independent_subject_versions_and_reversed_target_preserve_only_matching_clause(query, text, status, quote):
    decision = check(text, query)
    assert decision.status == status
    assert [text[start:end] for start, end in decision.spans] == [quote]


def test_quantity_is_not_a_software_version():
    query = "Where are Zephyr version 2.4 manifests stored?"
    assert check("Zephyr rotates keys every 30 days. Manifests are stored in Git.", query).status == "unknown"
    assert check("Wait 30 seconds. Zephyr manifests are stored in Git.", query).status == "unknown"


@pytest.mark.parametrize("query", ["Where are Atlas v2 deployment manifests stored?",
                                  "where are atlas v2 deployment manifests stored?"])
def test_other_product_version_cannot_supply_the_requested_product_span(query):
    text = ("Atlas v3 deployment manifests are stored in the new repository. "
            "Boreal v2 deployment manifests are stored in the unrelated repository.")
    assert check(text, query).status == "unknown"
    assert check(text, query).spans == ()
    assert check("Rotate 3 keys after the incident. Atlas manifests are stored in Git.", query).status == "unknown"


def test_document_declaration_cannot_erase_body_version_distinctions():
    query = "Where are Atlas v2 deployment manifests stored?"
    text = ("Applies to versions: 2\n"
            "Atlas v2 deployment manifests are stored in the stable repository.\n"
            "Atlas v3 deployment manifests are stored in the experimental repository.")
    decision = check(text, query)
    assert decision.status == "applicable"
    assert [text[start:end] for start, end in decision.spans] == [
        "Atlas v2 deployment manifests are stored in the stable repository"]
    assert check("Applies to versions: 2\nAtlas v3 manifests are stored in Git.", query).status == "unknown"


def test_different_product_in_same_project_cannot_supply_version_span():
    query = "青岚仍在SDK 7.4，应该如何提交？"
    text = "青岚SDK 8.2支持send函数；青岚Quasar 7.4支持submit函数。"
    assert check(text, query).status == "unknown"


def test_current_decision_binds_expiry_but_explicit_history_does_not_expire_with_wall_clock():
    text = "Valid from: 2026-01-01\nValid until: 2026-12-01T08:00:00+08:00\nZephyr stores manifests in Git."
    assert check(text).valid_until == "2026-12-01T00:00:00+00:00"
    assert check(text, "As of 2026-11-01, where are Zephyr manifests stored?").valid_until is None
    assert check(text, task_context={"applicability": {"as_of": "2026-11-01"}}).valid_until is None
    unknown = check(text, "Where are Zephyr version 2.4 manifests stored?")
    assert unknown.status == "unknown"
    assert unknown.valid_until == "2026-12-01T00:00:00+00:00"


def test_conflicting_cutoffs_keep_earliest_expiry_for_unknown_evidence():
    result = check("Valid until: 2027-01-01\nValid until: 2026-12-01\nZephyr stores manifests in Git.")
    assert result.status == "unknown"
    assert result.valid_until == "2026-12-01T00:00:00+00:00"


@pytest.mark.parametrize("opener, closer", [("~~~text", "~~~"), ("```text", "```"), (">", ">")])
def test_example_validity_headers_do_not_expire_unconstrained_document(opener, closer):
    sample = "Valid from: 2020-01-01\nValid until: 2020-02-01"
    if opener == ">":
        sample = "> Valid from: 2020-01-01\n> Valid until: 2020-02-01"
    text = ("Example of an expired validity header:\n" + opener + "\n" + sample + "\n" + closer +
            "\nAtlas deployment manifests are stored in the infrastructure repository.")
    decision = check(text, "Where are Atlas deployment manifests stored?")
    assert decision.status == "applicable"
    assert decision.required is False
    assert decision.valid_until is None


@pytest.mark.parametrize("middle", [". ", ". Boreal runs in a separate container. ", "; An unrelated note; "])
def test_version_subject_inheritance_cannot_cross_sentence_or_unbound_clause(middle):
    text = ("Atlas v3 deployment manifests are stored in the new repository" + middle +
            "v2 deployment manifests are stored in the unrelated repository.")
    decision = check(text, "Where are Atlas v2 deployment manifests stored?")
    assert decision.status == "unknown"
    assert decision.spans == ()


def test_ignoring_example_keeps_matching_clause_offsets_in_original_text():
    text = ("```\nAtlas v2 manifests are stored in the wrong repository.\n```\n"
            "Atlas v2 manifests are stored in the correct repository.")
    decision = check(text, "Where are Atlas v2 manifests stored?")
    assert decision.status == "applicable"
    assert [text[start:end] for start, end in decision.spans] == [
        "Atlas v2 manifests are stored in the correct repository"]
