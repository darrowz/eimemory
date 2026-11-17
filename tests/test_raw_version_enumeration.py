"""Per-release evidence is a contribution, not proof of a continuous range."""
import pytest

from eimemory.raw.applicability import assess_applicability


def check(text, query):
    return assess_applicability({"status": "active", "content": {"raw_text": text}}, query=query)


@pytest.mark.parametrize("query", [
    "For each Alderbeam release from 3.4 through 3.7, give the queue limit.",
    "For each Alderbeam version from 3.4 to 3.7, give the queue limit.",
    "List the queue limit separately for every Alderbeam version from 3.4 through 3.7.",
    "列出青栎客户端3.4到3.7每个版本的队列上限。",
    "青栎客户端从3.4至3.7的每个版本的队列上限是多少？",
    "请分别列出青栎客户端从3.4到3.7每个版本的队列上限。",
])
def test_per_release_request_keeps_scoped_contributions(query):
    name = "青栎" if "青栎" in query else "Alderbeam "
    text = f"{name}3.4的队列上限是12。{name}3.5的队列上限是18。"
    result = check(text, query)
    assert result.status == "applicable"
    assert result.reason == "enumerated_version_clause_matched"
    assert [text[a:b] for a, b in result.spans] == text.rstrip("。").split("。")


@pytest.mark.parametrize("version", ["3.3", "3.8", "4.0"])
def test_enumeration_rejects_outside_versions(version):
    result = check(f"Alderbeam {version} allows 50 views.",
                   "For each Alderbeam release from 3.4 through 3.7, give the queue limit.")
    assert result.status == "not_applicable"


def test_enumeration_preserves_product_and_source_offsets():
    text = "Otherbeam 3.5 allows 50 views. Alderbeam 3.5 allows 18 views. Alderbeam 3.8 allows 90 views."
    result = check(text, "For each Alderbeam release from 3.4 through 3.7, give the queue limit.")
    assert result.status == "applicable"
    assert [text[a:b] for a, b in result.spans] == ["Alderbeam 3.5 allows 18 views"]


@pytest.mark.parametrize("query", [
    "Which limit applies to all Alderbeam releases from v3.4 inclusive to v3.7 exclusive?",
    "Which limit applies to Alderbeam versions below v3.7?",
    "Which limit applies to Alderbeam versions from 3.4 to 3.7?",
])
def test_enumeration_does_not_turn_one_point_into_continuous_coverage(query):
    assert check("Alderbeam 3.5 allows 18 views.", query).status != "applicable"


def test_ordinary_quantity_each_does_not_create_version_enumeration():
    result = check("Alderbeam v3.4 permits 8 retries.",
                   "For each of the probes from 3 to 8, which mode does Alderbeam v3.4 require?")
    assert result.status == "applicable"
    assert result.reason == "version_clause_matched"


@pytest.mark.parametrize("query", [
    "For each Alderbeam release from 3.7 through 3.4, give the limit.",
    "For each Alderbeam release from 3.4 inclusive to 3.7 exclusive, give the limit.",
])
def test_malformed_or_explicit_continuous_range_is_not_discrete(query):
    assert check("Alderbeam 3.5 allows 18 views.", query).status != "applicable"


@pytest.mark.parametrize("query,text", [
    ("For each Alderbeam release from 3.4 to 3.7 (exclusive), give its mode.", "Alderbeam 3.7 uses endpoint-only."),
    ("For each Alderbeam release from 3.4 through 3.7, excluding 3.5, give its mode.", "Alderbeam 3.5 uses hidden-only."),
    ("列出青栎客户端3.4到3.7每个版本的队列上限，不含3.7。", "青栎3.7的队列上限是90。"),
    ("青栎客户端3.4到3.7每个版本，排除3.5，队列上限是多少？", "青栎3.5的队列上限是90。"),
])
def test_unparsed_explicit_exclusions_do_not_admit_excluded_endpoint(query, text):
    assert check(text, query).status != "applicable"


@pytest.mark.parametrize("query,text", [
    ("For each Alderbeam release from 3.5 through 3.5, give its client retry limit excluding 2 administrative retries.",
     "Alderbeam 3.5 allows 18 client retries excluding 2 administrative retries."),
    ("青栎客户端3.5到3.5每个版本的客户端重试次数是多少，不含2次管理重试？", "青栎3.5的客户端重试上限是18次，不含2次管理重试。"),
])
def test_counted_quantity_exclusion_does_not_exclude_release(query, text):
    assert check(text, query).status == "applicable"


@pytest.mark.parametrize("exclusion", ["excluding version 3.5", "except release 3.5", "excluding v3.5"])
def test_explicit_release_exclusions_stay_unknown(exclusion):
    assert check("Alderbeam 3.5 allows 18 views.",
                 f"For each Alderbeam release from 3.4 through 3.7, {exclusion}, give its limit.").status != "applicable"
