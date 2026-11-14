"""Public-API regressions for independent review findings; synthetic only."""
import signal
from pathlib import Path

import pytest

from eimemory.raw.retrieval import rerank_raw_results
from eimemory.governance.release.release_impact import _domains_for_change, _ignored_change


def row(name, text, score, day):
    return {"record": {"record_id": name, "kind": "raw_chunk", "text": text,
                       "occurred_at": day + "T00:00:00Z"}, "base_score": score}


@pytest.mark.parametrize("query,old,new", [
    ("Alice archive backend", "Alice archive backend is SQLite.",
     "Correction: Bob archive backend changed to PostgreSQL."),
    ('"Ann" archive backend', "Ann archive backend is SQLite.",
     "Correction: Joanne archive backend changed to PostgreSQL."),
    ("ticket 314 status", "Ticket 314 status is open.",
     "Correction: ticket 271 status changed to resolved."),
])
def test_unrelated_entity_or_id_cannot_become_a_local_correction(query, old, new):
    ranked = rerank_raw_results(query=query, results=[
        row("right", old, .9, "2026-01-01"), row("wrong", new, .1, "2026-02-01")])
    assert ranked[0]["record"]["record_id"] == "right"


def test_numeric_only_query_retains_exact_identifier_evidence():
    ranked = rerank_raw_results(query="314", results=[
        row("wrong", "Ticket 271 status is open.", .9, "2026-01-01"),
        row("right", "Ticket 314 status is open.", .1, "2026-01-01")])
    assert ranked[0]["record"]["record_id"] == "right"


@pytest.mark.parametrize("query,old,new", [
    ("When is the archive backup scheduled?", "Archive backup is scheduled at 01:00.",
     "Correction: archive backup scheduled time changed to 03:00."),
    ("Which RFC2001 archive backend is used?", "RFC2001 archive backend is SQLite.",
     "Correction: RFC2001 archive backend changed to PostgreSQL."),
    ("Which ticket-2025 status applies?", "Ticket-2025 status is open.",
     "Correction: ticket-2025 status changed to resolved."),
])
def test_time_answers_and_numeric_identifier_years_do_not_disable_current_corrections(query, old, new):
    ranked = rerank_raw_results(query=query, results=[
        row("old", old, .9, "2026-01-01"), row("new", new, .1, "2026-02-01")])
    assert ranked[0]["record"]["record_id"] == "new"


def test_unmatched_cjk_quotes_have_bounded_query_parse_cost():
    def expired(_signum, _frame):
        raise AssertionError("100000 unmatched quote characters exceeded isolated 1s parse budget")
    previous = signal.signal(signal.SIGALRM, expired)
    try:
        signal.setitimer(signal.ITIMER_REAL, 1.0)
        ranked = rerank_raw_results(query="archive backend",
            task_context={"metadata_question": "“" * 100000},
            results=[row("right", "Archive backend is SQLite.", .9, "2026-01-01")])
        assert ranked[0]["record"]["record_id"] == "right"
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


@pytest.mark.parametrize("path,domains", [
    ("eimemory/raw/_relevance.py", {"memory.recall"}),
    ("eimemory/raw/KROUTER_NOTICE.txt", {"deployment.runtime"}),
])
def test_new_raw_artifacts_have_exact_release_impact_registration(path, domains):
    assert _domains_for_change(repo=Path('/nonexistent'), path=path,
        ancestor='0' * 40, current='1' * 40) == domains


def test_unrelated_new_raw_modules_still_fail_closed():
    assert not _domains_for_change(repo=Path('/nonexistent'),
        path='eimemory/raw/unregistered_module.py', ancestor='0' * 40, current='1' * 40)
    assert not _ignored_change(repo=Path('/nonexistent'),
        path='eimemory/raw/unregistered_module.py', ancestor='0' * 40, current='1' * 40)
