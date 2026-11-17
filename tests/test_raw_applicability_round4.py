"""Bounded date and range grammar, independent of benchmark entities/gold."""
from datetime import datetime, timezone

import pytest

from eimemory.raw.applicability import assess_applicability, _inline_constraints, _calendar_date_spans


def check(text, query, **kwargs):
    return assess_applicability(
        {"status": "active", "content": {"raw_text": text}}, query=query,
        now=datetime(2026, 11, 17, tzinfo=timezone.utc), **kwargs)


@pytest.mark.parametrize("date", [
    "the twenty-fourth of March 2032", "March the twenty-fourth, 2032",
    "TWENTY FOURTH OF MARCH 2032", "the first of May 2032",
    "the thirty-first of January 2032", "February the twenty-ninth 2032",
])
def test_valid_ordinal_calendar_does_not_invent_release(date):
    query = f"Which inbox should Emberleaf use on {date}?"
    result = check("Emberleaf's temporary instruction uses the cobalt inbox.", query)
    assert result.status == "applicable"
    assert result.required is False
    assert not _inline_constraints(query)


@pytest.mark.parametrize("date", ["the thirty-first of February 2032", "February the twenty-ninth 2031"])
def test_invalid_ordinal_calendar_does_not_hide_version_like_number(date):
    query = f"On {date}, what is the rule for Emberleaf v4?"
    assert not _calendar_date_spans(query)
    assert _inline_constraints(query)


@pytest.mark.parametrize("query", [
    "On the twenty-fourth of March 2032, does Emberleaf v4 support this field?",
    "Does Emberleaf version 4 support it on March the twenty-fourth 2032?",
    "Emberleaf 4\nMarch the twenty-fourth 2032: does it support this field?",
])
def test_ordinal_date_does_not_remove_real_version(query):
    assert check("Emberleaf v3 accepts the old field.", query).status == "not_applicable"


EN_RANGE = "For Emberleaf releases greater than or equal to v4.2 and less than v4.7, every release requires mode locked."
EN_QUERY = "Which mode applies across every Emberleaf release from v4.2 inclusive up to v4.7 exclusive?"
ZH_RANGE = "晴芷代理适用区间：版本大于等于4.2且小于4.7。在这个整个区间内，批次上限统一为9条。"
ZH_QUERY = "晴芷代理从4.2（含）到4.7（不含）的所有版本，批次上限是多少？"


@pytest.mark.parametrize("text,query,required", [
    (EN_RANGE, EN_QUERY, "mode locked"), (ZH_RANGE, ZH_QUERY, "上限统一为9条"),
    ("Emberleaf versions from v4.2 inclusive to v4.7 exclusive require signed input.", EN_QUERY, "signed input"),
])
def test_complete_explicit_range_preserves_binding_and_fact(text, query, required):
    result = check(text, query)
    assert result.status == "applicable"
    assert result.reason == "version_clause_matched"
    assert required in " ".join(text[start:end] for start, end in result.spans)
    assert all(0 <= start < end <= len(text) for start, end in result.spans)


@pytest.mark.parametrize("version,expected", [("4.2", "applicable"), ("4.6", "applicable"),
                                            ("4.7", "not_applicable"), ("4.1", "not_applicable")])
def test_half_open_range_exact_point_boundaries(version, expected):
    assert check(EN_RANGE, f"Which mode applies to Emberleaf v{version}?").status == expected


@pytest.mark.parametrize("lo,hi,at,expected", [
    ("exclusive", "inclusive", "4.2", "not_applicable"),
    ("exclusive", "inclusive", "4.7", "applicable"),
    ("inclusive", "inclusive", "4.2", "applicable"),
    ("inclusive", "exclusive", "4.7", "not_applicable"),
])
def test_both_endpoint_inclusion_flags(lo, hi, at, expected):
    text = f"Emberleaf releases from v4.2 {lo} to v4.7 {hi} require mode locked."
    assert check(text, f"Which mode does Emberleaf v{at} require?").status == expected


@pytest.mark.parametrize("text", [
    "Emberleaf v4.3 requires mode locked.",
    "Emberleaf releases from v4.3 inclusive to v4.7 exclusive require mode locked.",
    "Emberleaf releases from v4.2 exclusive to v4.7 exclusive require mode locked.",
])
def test_point_or_partial_range_does_not_cover_complete_requested_range(text):
    result = check(text, EN_QUERY)
    assert result.status == "unknown"
    assert result.reason == "version_range_not_covered"


def test_range_at_excluded_boundary_cannot_overlap():
    assert check(EN_RANGE, "Emberleaf releases from v4.7 inclusive to v5.0 exclusive use which mode?").status == "not_applicable"


@pytest.mark.parametrize("source", [
    EN_RANGE.replace("Emberleaf", "Otherleaf"),
    EN_RANGE.replace("Emberleaf", "Otherleaf") + " Emberleaf v5 requires mode adaptive.",
    "```\n" + EN_RANGE + "\n```\nEmberleaf has no declared compatibility here.",
    "> " + EN_RANGE + "\nEmberleaf has no declared compatibility here.",
])
def test_other_product_or_example_range_cannot_qualify(source):
    assert check(source, EN_QUERY).status != "applicable"


@pytest.mark.parametrize("text", [
    "Emberleaf releases from v4.7 inclusive to v4.2 exclusive require mode locked.",
    "Emberleaf releases from v4.2 inclusive to v4.2 exclusive require mode locked.",
    "Emberleaf migrates from v4.2 to v4.7; the next paragraph discusses mode locked.",
])
def test_empty_reversed_or_implicit_ranges_do_not_prove_coverage(text):
    assert check(text, EN_QUERY).status != "applicable"


def test_invalid_requested_interval_is_unknown():
    result = check(EN_RANGE, EN_QUERY.replace("from v4.2", "from v4.8"))
    assert (result.status, result.reason) == ("unknown", "unsupported_requested_version")


@pytest.mark.parametrize("query", [
    "Which mode applies to Emberleaf versions between 4.2 and 4.7?",
    "Which mode applies to Emberleaf versions from 4.2 to 4.7?",
    "Which mode applies to Emberleaf versions [4.2, 4.7)?",
    "Which mode applies to Emberleaf versions >=4.2, <4.7?",
    "晴芷代理从4.2到4.7的所有版本采用什么模式？",
])
def test_unsupported_range_not_silently_treated_as_unconstrained(query):
    result = check("An unrelated product has no applicability statement.", query)
    assert (result.status, result.reason) == ("unknown", "unsupported_requested_version")


def test_comparator_spacing_does_not_turn_exclusive_bound_inclusive():
    text = "Emberleaf versions greater\tthan v4.2 and less\tthan v4.7 use mode locked."
    assert check(text, "Which mode applies to Emberleaf v4.2?").status == "not_applicable"
    assert check(text, "Which mode applies to Emberleaf v4.7?").status == "not_applicable"


def test_full_range_excludes_following_other_release_branch():
    text = ZH_RANGE + "晴芷代理版本5.0的上限为200条。"
    result = check(text, ZH_QUERY)
    assert result.status == "applicable"
    selected = " ".join(text[a:b] for a, b in result.spans)
    assert "上限统一为9条" in selected and "200" not in selected


def test_numeric_quantity_range_is_not_a_software_release():
    result = check("The scan accepts a configurable page count.",
                   "Does the scan accept from 4 inclusive to 9 exclusive pages?")
    assert result.status == "applicable" and result.required is False


def test_running_target_precedes_later_unresolved_migration_question():
    text = "Emberleaf v4.2 requires mode locked."
    query = "Emberleaf runs version 4.2. Can a migration from v4.2 to v4.7 use that mode?"
    result = check(text, query)
    assert result.status == "applicable"


def test_range_grammar_cannot_strip_a_substring_from_product_identity():
    text = "Edgefrom releases greater than or equal to v4.2 and less than v4.7 require mode locked."
    assert check(text, EN_QUERY.replace("Emberleaf", "Edgefrom")).status == "applicable"
    assert check(text, EN_QUERY.replace("Emberleaf", "Edge")).status != "applicable"


@pytest.mark.parametrize("date,expected", [
    ("the twentieth of February 2032", "not_yet_valid"),
    ("February twenty-first 2032", "constraints_satisfied"),
    ("the twenty-fourth of February 2032", "constraints_satisfied"),
    ("25 February 2032", "validity_expired"),
])
def test_explicit_natural_as_of_uses_requested_time_for_future_window(date, expected):
    text = "Valid from: 2032-02-21\nValid until: 2032-02-25\nEmberleaf uses the cobalt inbox."
    result = check(text, f"As of {date}, which inbox does Emberleaf use?")
    assert result.reason == expected
    if result.status == "applicable":
        assert result.valid_until is None


def test_explicit_natural_as_of_historical_window_survives_wall_clock_expiry():
    text = "Valid from: 2025-03-01\nValid until: 2025-04-01\nEmberleaf uses the cobalt inbox."
    result = check(text, "As of March the twenty-fourth 2025, which inbox does Emberleaf use?")
    assert result.status == "applicable"
    assert result.valid_until is None
    assert check(text, "Which inbox does Emberleaf use?").reason == "validity_expired"


def test_incidental_calendar_does_not_override_current_validity_or_expiry_binding():
    text = "Valid from: 2026-01-01\nValid until: 2027-01-01\nEmberleaf uses the cobalt inbox."
    result = check(text, "The inventory dated February twenty-first 2032 mentions Emberleaf. Which inbox is used?")
    assert result.status == "applicable"
    assert result.valid_until == "2027-01-01T00:00:00+00:00"


def test_natural_as_of_requires_source_validity_and_agrees_with_explicit_context():
    query = "As of the twenty-fourth of February 2032, which inbox does Emberleaf use?"
    text = "Valid from: 2032-02-21\nValid until: 2032-02-25\nEmberleaf uses the cobalt inbox."
    assert check("Emberleaf uses the cobalt inbox.", query).reason == "time_applicability_unknown"
    assert check(text, query, task_context={"applicability": {"as_of": "2032-02-24"}}).status == "applicable"
    assert check(text, query, task_context={"applicability": {"as_of": "2032-02-23"}}).reason == "ambiguous_applicability_request"


def test_invalid_natural_as_of_does_not_fall_back_to_current_clock():
    text = "Valid from: 2026-01-01\nValid until: 2027-01-01\nEmberleaf uses the cobalt inbox."
    assert check(text, "As of February the thirty-first 2026, which inbox is used?").status == "unknown"


@pytest.mark.parametrize("product", ["Foxglade", "Nightfern"])
@pytest.mark.parametrize("quantity", ["probes", "lanes", "objects"])
def test_quantity_interval_does_not_borrow_a_later_version_anchor(product, quantity):
    text = f"{product} v6.4 requires mode strict."
    query = f"During the run, between 3 and 9 {quantity} were observed. Which mode does {product} v6.4 require?"
    assert check(text, query).status == "applicable"


@pytest.mark.parametrize("product", ["Foxglade", "Nightfern"])
def test_quantity_interval_does_not_borrow_an_earlier_version_anchor(product):
    text = f"{product} v6.4 requires mode strict."
    query = f"{product} version 6.4 has a capacity from 3 inclusive to 9 exclusive. Which mode is required?"
    assert check(text, query).status == "applicable"


def test_source_quantity_interval_does_not_replace_explicit_product_version():
    text = "Foxglade version 6.4 accepts a capacity from 3 inclusive to 9 exclusive and requires mode strict."
    assert check(text, "Which mode does Foxglade v6.4 require?").status == "applicable"
    assert check(text, "Which mode does Foxglade v6.5 require?").status == "not_applicable"


@pytest.mark.parametrize("separator", ["; ", ", "])
def test_range_continuation_stops_before_new_version_branch(separator):
    text = ("Foxglade versions greater than or equal to v6.2 and less than v6.7. "
            "In this range, mode strict applies" + separator + "Nightfern v9 uses mode adaptive.")
    result = check(text, "Which mode applies to Foxglade versions from v6.2 inclusive to v6.7 exclusive?")
    assert result.status == "applicable"
    selected = " ".join(text[a:b] for a, b in result.spans)
    assert "mode strict" in selected and "Nightfern" not in selected


@pytest.mark.parametrize("source_marker,query_marker", [("version 6.4", "v6.4"), ("v6.4", "version 6.4")])
def test_v_marker_is_not_part_of_product_identity(source_marker, query_marker):
    assert check(f"Foxglade {source_marker} requires mode strict.",
                 f"Which mode does Foxglade {query_marker} require?").status == "applicable"
    assert check(f"Nightfern {source_marker} requires mode strict.",
                 f"Which mode does Foxglade {query_marker} require?").status != "applicable"


def test_symbolic_comparison_endpoint_operators_are_exact():
    text = "Foxglade versions >6.2 and <=6.7 require mode strict."
    assert check(text, "Which mode does Foxglade version 6.2 require?").status == "not_applicable"
    assert check(text, "Which mode does Foxglade version 6.7 require?").status == "applicable"


def test_range_does_not_inherit_unbound_later_prose_or_neighbor_version():
    text = "晴芷代理适用区间：版本大于等于4.2且小于4.7。另一产品的批次上限为200条。晴芷代理版本5.0批次上限为300条。"
    result = check(text, ZH_QUERY)
    assert result.status == "applicable"
    selected = " ".join(text[a:b] for a, b in result.spans)
    assert "200" not in selected and "300" not in selected


def test_range_anaphor_cannot_inherit_across_newline():
    text = "晴芷代理适用区间：版本大于等于4.2且小于4.7。\n在该区间内，批次上限为200条。"
    result = check(text, ZH_QUERY)
    assert result.status == "applicable"
    assert "200" not in " ".join(text[a:b] for a, b in result.spans)


def test_withdrawn_source_and_expired_declaration_remain_rejected():
    assert check("Status: withdrawn\n" + EN_RANGE, EN_QUERY).status == "not_applicable"
    assert check("Valid from: 2026-01-01\nValid until: 2026-11-16\n" + EN_RANGE, EN_QUERY).reason == "validity_expired"


def test_current_range_expiry_and_historical_request_are_still_distinct():
    text = "Valid from: 2026-01-01\nValid until: 2027-01-01\n" + EN_RANGE
    assert check(text, EN_QUERY).valid_until == "2027-01-01T00:00:00+00:00"
    assert check(text, EN_QUERY, task_context={"applicability": {"as_of": "2026-06-01"}}).valid_until is None
