import pytest
from eimemory.api.runtime import Runtime
from eimemory.models.records import RecordEnvelope, ScopeRef


@pytest.mark.parametrize("organ,expected", [("fixture-organ", {"fixture-organ"}), ("", set())])
def test_runtime_labels_own_filter_values_in_memory_and_sqlite(tmp_path, organ, expected):
    runtime = Runtime.create(root=tmp_path)
    try:
        record = RecordEnvelope.create(kind="reflection", title="fixture", scope=ScopeRef(),
            content={"organ": "stale-organ", "modality": "stale-modality"},
            meta={"runtime_meta": {"organ": organ, "modality": "fixture-modality"}})
        assert runtime.memory._record_filter_labels(record)["organs"] == expected
        with runtime.store._lock:
            labels = runtime.store.sqlite._record_filter_labels(record)
        assert labels["organs"] == expected
        assert labels["modalities"] == {"fixture-modality"}
    finally:
        runtime.close()
