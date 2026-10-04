"""Ordinary dataset shapes and text normalization, using synthetic values."""
from types import SimpleNamespace

import pytest

from eimemory.evaluation import actionable_memory, task_replay
from eimemory.evaluation.contracts import object_entries
from eimemory.models.records import ScopeRef


@pytest.mark.parametrize("field", ["seed", "cases"])
@pytest.mark.parametrize("rows", [[None], [{}, 17], ["case"]])
def test_object_entries_do_not_silently_drop_bad_rows(field, rows):
    with pytest.raises(ValueError, match=field):
        object_entries(rows, field_name=field)


def test_object_entries_copy_valid_rows():
    original = [{"id": "one"}]
    result = object_entries(original, field_name="cases")
    assert result == original
    assert result[0] is not original[0]


@pytest.mark.parametrize("normalizer", [actionable_memory.normalize_actionable_memory_dataset, task_replay.normalize_real_task_replay_dataset])
def test_normalizers_surface_malformed_case_rows(normalizer):
    with pytest.raises(ValueError, match="cases"):
        normalizer({"cases": [{"query": "valid"}, None]})


@pytest.mark.parametrize("value", [None, 123, True, "dataset"])
def test_task_replay_root_types_have_explicit_errors(value):
    with pytest.raises(ValueError, match="JSON object or list"):
        task_replay.normalize_real_task_replay_dataset(value)


@pytest.mark.parametrize("value,expected", [(" exact phrase ", [" exact phrase "]), ([" exact phrase "], [" exact phrase "]), (None, []), ("  ", [])])
def test_actionable_scalar_terms_are_not_split_into_characters(value, expected):
    assert actionable_memory._expectation_terms(value) == expected


def test_task_replay_scalar_and_list_terms_strip_equally():
    assert task_replay._strings(" expected ") == task_replay._strings([" expected "]) == ["expected"]


def test_bad_seed_field_is_reported_without_aborting_later_seed():
    runtime = SimpleNamespace(memory=SimpleNamespace(ingest=lambda **kwargs: SimpleNamespace(status="active", record_id="good")))
    records, errors = actionable_memory._seed_records(runtime,
        [{"id": "bad", "meta": 17}, {"id": "good", "text": "hello"}], default_scope=ScopeRef())
    assert len(errors) == 1 and errors[0]["id"] == "bad"
    assert [seed_id for seed_id, record in records] == ["good"]


def test_stripping_remains_explicit_for_forbidden_and_constraint_terms():
    assert actionable_memory._expectation_terms([" padded "], strip=True) == ["padded"]


def test_exact_title_expectations_preserve_existing_list_spacing():
    row = SimpleNamespace(record_id="record", kind="memory", title=" padded ", summary="", detail="", content={})
    runtime = SimpleNamespace(memory=SimpleNamespace(recall=lambda **kwargs: SimpleNamespace(items=[row], confidence=0.7, explanation={})))
    report = actionable_memory._run_recall_case(runtime=runtime,
        case={"query": "neutral query", "query_type": "chat", "expect_any_title": [" padded "]},
        index=0, case_seed=None, scope=ScopeRef(), dataset_scope=ScopeRef())
    assert report["passed"] is True
