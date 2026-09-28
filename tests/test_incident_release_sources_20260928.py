import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import pytest

from deploy.verify_python_sources import verify as verify_sources
from deploy.release_source_checkpoint import checkpoint, bytecode_inventory
from deploy.diagnose_recall_release import diagnose, read_report


def test_syntax_validation_has_no_bytecode_side_effect(tmp_path):
    (tmp_path / 'mod.py').write_text('x = 1\n')
    report = verify_sources(tmp_path)
    assert report['ok'] is True
    assert list(tmp_path.rglob('*.pyc')) == []


def test_syntax_error_is_not_ignored(tmp_path):
    (tmp_path / 'mod.py').write_text('def broken(\n')
    with pytest.raises(SyntaxError):
        verify_sources(tmp_path)


def test_existing_bytecode_is_preserved_and_rejected(tmp_path):
    (tmp_path / 'mod.py').write_text('x = 1\n')
    pyc = tmp_path / 'mod.pyc'; pyc.write_bytes(b'original-scene')
    with pytest.raises(ValueError, match='source_bytecode_pollution'):
        verify_sources(tmp_path)
    assert pyc.read_bytes() == b'original-scene'


def test_readonly_checkpoint_does_not_clean_even_if_validator_claims_ok(tmp_path):
    repo=tmp_path/'repo';(repo/'deploy').mkdir(parents=True)
    (repo/'deploy/clean_release_bytecode.py').write_text('import sys\nassert "--validate-source" in sys.argv\n')
    root=tmp_path/'releases';release=root/('a'*40);release.mkdir(parents=True)
    cache=release/'bad.pyc';cache.write_bytes(b'preserve')
    result=checkpoint(release=release,releases_root=root,repo=repo,commit='a'*40,phase='services_started')
    assert result['ok'] is False and result['bytecode_count']==1
    assert cache.read_bytes()==b'preserve'
    assert result['writer_attribution']=='not_established'


def test_checkpoint_records_private_output(tmp_path):
    repo=tmp_path/'repo';(repo/'deploy').mkdir(parents=True)
    (repo/'deploy/clean_release_bytecode.py').write_text('pass\n')
    root=tmp_path/'releases';release=root/('a'*40);release.mkdir(parents=True)
    result=checkpoint(release=release,releases_root=root,repo=repo,commit='a'*40,phase='stage_built',report_dir=tmp_path/'reports')
    path=Path(result['report_path']);assert path.exists()
    if os.name=='posix':assert path.stat().st_mode & 0o777 == 0o600


def test_checkpoint_rejects_symlink_release(tmp_path):
    root=tmp_path/'releases';release=root/('a'*40);release.mkdir(parents=True)
    alias=root/('b'*40)
    try:alias.symlink_to(release,target_is_directory=True)
    except OSError:pytest.skip('symlink unavailable')
    with pytest.raises(ValueError):
        checkpoint(release=alias,releases_root=root,repo=tmp_path,commit='b'*40,phase='start')


def test_missing_inputs_stay_unverified():
    r=diagnose(expected_commit='a'*40,reports={})
    assert r['first_blocking_boundary']=='release_source'
    assert r['first_failed_boundary']==''
    assert r['closure_certified'] is False


def test_source_failure_precedes_quality_wait():
    r=diagnose(expected_commit='a'*40,reports={
        'release_source':{'ok':False,'release_commit':'a'*40,'reason':'release_bytecode_pollution'},
        'nightly':{'ok':False,'recall_quality_gate':{'ok':False,'blocked_reason':'recall_quality_evidence_incomplete'}}})
    assert r['first_failed_boundary']=='release_source'
    assert r['first_unverified_boundary']=='health'


def test_binding_identity_mismatch_remains_failure():
    r=diagnose(expected_commit='a'*40,expected_receipt='r',expected_session='s',reports={
        'binding':{'ok':True,'commit':'b'*40,'receipt_id':'r','session_id':'s'}})
    assert r['first_failed_boundary']=='binding'
    assert r['first_blocking_boundary']=='release_source'


@pytest.mark.parametrize('raw',['{"ok":true,"ok":false}','{"n":NaN}','{"n":1e999}'])
def test_evidence_reader_rejects_ambiguous_json(tmp_path, raw):
    p=tmp_path/'report.json';p.write_text(raw)
    with pytest.raises(ValueError):read_report(p)


def test_fifo_evidence_read_finishes_instead_of_waiting(tmp_path):
    if not hasattr(os,'mkfifo'):pytest.skip('FIFO requires POSIX')
    fifo=tmp_path/'fifo';os.mkfifo(fifo)
    command=[sys.executable,'-I','-B',str(Path(__file__).resolve().parents[1]/'deploy/diagnose_recall_release.py'),
             '--expected-commit','a'*40,'--nightly',str(fifo)]
    result=subprocess.run(command,capture_output=True,text=True,timeout=5)
    assert result.returncode==2


def test_diagnosis_can_read_persisted_supervisor_envelope():
    reports={'nightly':{'runs':{'nightly':{'ok':False,'nightly_diagnostics':{
        'execution_ok':False,'first_failed_step':'provider_binding',
        'recall_quality_evidence':{'status':'insufficient','missing_roles':['rewrite']}}}}}}
    result=diagnose(expected_commit='a'*40,reports=reports)
    by={r['boundary']:r for r in result['boundaries']}
    assert by['nightly_execution']['first_failed_step']=='provider_binding'
    assert by['recall_quality']['state']=='waiting'
    assert result['closure_certified'] is False
