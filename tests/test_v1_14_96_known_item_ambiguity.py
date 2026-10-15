from types import SimpleNamespace

from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.scheduler import jobs

SCOPE = ScopeRef(tenant_id="t", agent_id="a", workspace_id="w", user_id="u")


def _runtime(records):
    def list_records(**kw):
        return records[kw["offset"]:kw["offset"] + kw["limit"]]
    return SimpleNamespace(store=SimpleNamespace(list_records=list_records))


def test_shared_query_text_is_not_a_known_item(monkeypatch):
    monkeypatch.setattr(jobs, "build_recall_index_document",
                        lambda r: SimpleNamespace(lane="primary", visibility="default", source_class="x", projection_type="y"))
    monkeypatch.setattr(jobs, "is_outcome_pollution_record", lambda r: False)
    monkeypatch.setattr(jobs, "default_recall_blocked_lane", lambda *a: False)
    shared = [RecordEnvelope.create(kind="memory", title=f"t{i}", summary="same generic summary", scope=SCOPE)
              for i in range(2)]
    unique = [RecordEnvelope.create(kind="memory", title=f"u{i}", summary=f"unique fact {i}", scope=SCOPE)
              for i in range(3)]
    out = jobs._production_recall_smoke_dataset(_runtime(shared + unique), scope={'tenant_id':'t','agent_id':'a','workspace_id':'w','user_id':'u'})
    expected = {c["expected_record_ids"][0] for c in out["cases"]}
    assert expected == {r.record_id for r in unique}
    assert all("_query_key" not in c for c in out["cases"])
