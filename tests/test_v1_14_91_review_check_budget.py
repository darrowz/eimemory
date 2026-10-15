from eimemory.llm.research_review import _check_timeout


def test_default_budget_covers_cold_import():
    assert _check_timeout({}) == 45


def test_budget_is_configurable_and_bounded():
    assert _check_timeout({"EIMEMORY_RESEARCH_REVIEW_CHECK_TIMEOUT_SECONDS": "20"}) == 20
    assert _check_timeout({"EIMEMORY_RESEARCH_REVIEW_CHECK_TIMEOUT_SECONDS": "999"}) == 120
    assert _check_timeout({"EIMEMORY_RESEARCH_REVIEW_CHECK_TIMEOUT_SECONDS": "1"}) == 5
    assert _check_timeout({"EIMEMORY_RESEARCH_REVIEW_CHECK_TIMEOUT_SECONDS": "x"}) == 45
