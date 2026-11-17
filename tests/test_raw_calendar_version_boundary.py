"""Calendar language must not invent a software release constraint."""
import pytest
from eimemory.raw.applicability import assess_applicability, _inline_constraints


@pytest.mark.parametrize("date", ["23 April 2027", "April 23 2027", "23rd Apr 2027",
                                 "APRIL 23rd 2027", "29 February 2028", "May 2 2027",
                                 "April 23, 2027", "23rd Apr. 2027"])
def test_valid_calendar_date_does_not_create_version(date):
    query=f"On {date}, what duration did the temporary notice specify?"
    record={"status":"active", "content":{"text":"The temporary notice permits 25 minutes."}}
    result=assess_applicability(record,query=query)
    assert result.status=="applicable" and result.reason=="no_explicit_constraints"
    assert not _inline_constraints(query)


@pytest.mark.parametrize("query", ["Atlas 4 permits the field", "Version 4 permits the field",
                                  "On 23 April 2027 Atlas 4 permits the field"])
def test_actual_product_release_still_checked(query):
    record={"status":"active", "content":{"text":"Atlas 3 permits the field."}}
    assert assess_applicability(record,query=query).status!="applicable"


def test_invalid_calendar_date_does_not_hide_ambiguous_numeric_constraint():
    assert _inline_constraints("On 31 February 2027 the rule changed")


@pytest.mark.parametrize("separator", [" ", "\n"])
def test_product_major_version_cannot_be_swallowed_by_following_month(separator):
    record={"status":"active", "content":{"text":"Atlas 3 accepts signed tokens."}}
    query=f"Atlas 4{separator}May 2027: can this deployment accept signed tokens?"
    assert assess_applicability(record,query=query).status=="not_applicable"


@pytest.mark.parametrize("marker", ["Version 3", "v3"])
def test_explicit_version_marker_survives_calendar_overlap(marker):
    record={"status":"active", "content":{"text":f"Applies to versions: 4\n{marker} April 2027 accepts signed tokens."}}
    assert assess_applicability(record,query="version 4: can I accept signed tokens?").status!="applicable"


@pytest.mark.parametrize("product", ["January", "February", "March", "April", "May", "June", "July", "August",
                                     "September", "October", "November", "December",
                                     "Jan", "Feb", "Mar", "Apr", "Jun", "Jul", "Aug", "Sep", "Sept", "Oct", "Nov", "Dec"])
def test_month_named_product_before_date_retains_major_version(product):
    record={"status":"active", "content":{"text":f"{product} 3 accepts signed tokens."}}
    query=f"{product} 4 June 2027: can this deployment accept signed tokens?"
    assert assess_applicability(record,query=query).status!="applicable"
