"""Full Runtime integration; run in a complete repository, not the audit harness."""
from pathlib import Path
import json
from eimemory.api.runtime import Runtime
from eimemory.models.records import ScopeRef
from eimemory.ops.closure_capture import process_capture
from eimemory.ops.release_closure_failure import record_release_closure_failure
from test_closure_capture_pipeline import SCOPE
from test_closure_pipeline_contract import blocked


def test_full_runtime_registers_identical_snapshot_once(tmp_path):
    runtime=Runtime.create(root=tmp_path/'runtime')
    try:
        report=blocked('release_closure_report_contract_invalid')
        kwargs=dict(scope=SCOPE,closure_report=report,execution={'attempt_id':'same-run'},detected_at='2026-09-28T08:00:00Z')
        one=record_release_closure_failure(runtime,**kwargs)
        two=record_release_closure_failure(runtime,**kwargs)
        rows=runtime.store.list_records(kinds=['incident'],scope=ScopeRef.from_dict(SCOPE),limit=100)
        assert one['incident_record_id']==two['incident_record_id']
        assert sum(r.record_id==one['incident_record_id'] for r in rows)==1
        saved=next(r for r in rows if r.record_id==one['incident_record_id'])
        assert not saved.content['repair_complete']
        assert saved.content['detector_report']['report_digest']==one['report_digest']
    finally:runtime.close()


def test_full_runtime_diagnosis_stays_ineligible(tmp_path):
    runtime=Runtime.create(root=tmp_path/'runtime')
    try:
        out=record_release_closure_failure(runtime,scope=SCOPE,closure_report=blocked(),detected_at='2026-09-28T08:00:00Z')
        assert out['incident_record_id'] and out['status']=='diagnosis_required'
        assert out['repair_eligible'] is False
        assert not out['repair_complete']
    finally:runtime.close()


def test_full_runtime_capture_survives_reopen(tmp_path):
    source=tmp_path/'source.json';source.write_text(json.dumps(blocked('storage_failed')),encoding='utf-8')
    result=process_capture(source,evidence_dir=tmp_path/'evidence',scope=SCOPE,
                           expected_commit='a'*40,runtime_factory=lambda:Runtime.create(root=tmp_path/'runtime'))
    runtime=Runtime.create(root=tmp_path/'runtime')
    try:
        record=runtime.store.get_by_id(result['incident_record_id'],scope=ScopeRef.from_dict(SCOPE))
        assert record and record.content['capture']['source_sha256']==result['capture']['source_sha256']
        assert Path(record.content['capture']['path'],'source.bin').read_bytes()==source.read_bytes()
    finally:runtime.close()
