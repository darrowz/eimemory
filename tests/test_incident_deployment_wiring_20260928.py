"""Focused source wiring checks; not a substitute for a real systemd start."""
from pathlib import Path
import os
import shlex
import subprocess
import sys
import pytest

from deploy.verify_release_mount import inspect_mount

ROOT = Path(__file__).resolve().parents[1]


def test_common_dropin_requires_readonly_mount_before_start():
    raw=(ROOT/'deploy/systemd/eimemory-python-runtime.conf').read_text()
    assert 'PrivateUsers=yes' in raw
    assert 'ReadOnlyPaths=@EIMEMORY_READONLY_RELEASE_PATH@' in raw
    command=next(line[len('ExecStartPre='):] for line in raw.splitlines() if line.startswith('ExecStartPre='))
    argv=shlex.split(command)
    assert argv[1:4]==['-I','-B','-c']
    assert 'ST_RDONLY' in argv[4] and 'immutable_release_mount_not_readonly' in argv[4]


def test_mount_precheck_refuses_writable_tree(tmp_path):
    if not hasattr(os, 'statvfs') or not hasattr(os,'ST_RDONLY'):
        pytest.skip('POSIX statvfs required')
    raw=(ROOT/'deploy/systemd/eimemory-python-runtime.conf').read_text()
    command=next(line.split('=',1)[1] for line in raw.splitlines() if line.startswith('ExecStartPre='))
    argv=shlex.split(command);argv[0]=sys.executable;argv[-1]=str(tmp_path)
    result=subprocess.run(argv,capture_output=True,text=True,timeout=10)
    assert result.returncode!=0 and 'immutable_release_mount_not_readonly' in result.stderr
    assert not inspect_mount(tmp_path)['ok']


def test_installer_uses_memory_compilation_and_no_automatic_bytecode_cleanup():
    text=(ROOT/'deploy/install_immutable_release.sh').read_text()
    assert '-m compileall -q "$STAGE_DIR/eimemory"' not in text
    assert 'verify_python_sources.py" --root "$STAGE_DIR/eimemory"' in text
    assert text.count('--render-releases-root "$INSTALL_ROOT/releases"')==2
    for phase in ('build_outputs_checked', 'stage_built', 'console_verified', 'metadata_installed',
                  'services_started', 'hermes_verified', 'health_verified', 'before_business_closure',
                  'after_business_closure'):
        assert phase in text
    assert 'PRESERVE_FAILED_SOURCE=1' in text
    assert 'source_evidence_preserved=$FAILED_DIR' in text
    assert 'source_evidence_preserved=$STAGE_DIR' in text


def test_quality_wrapper_and_nightly_report_call_new_contracts():
    quality=(ROOT/'eimemory/evaluation/production_recall.py').read_text()
    jobs=(ROOT/'eimemory/scheduler/jobs.py').read_text()
    assert 'evaluate_quality_report(' in quality
    assert 'nightly_result_diagnostics(report, step_reports)' in jobs
    assert 'report["recall_quality_evidence"]' in jobs
    assert 'O_NONBLOCK' in jobs
    assert 'strict_json_loads(raw, max_bytes=MAX_PRODUCTION_RECALL_DATASET_BYTES' in jobs


def test_renderer_cli_and_callable_both_accept_releases_root():
    text=(ROOT/'deploy/install_managed_systemd_dropin.py').read_text()
    assert 'render_releases_root: str = ""' in text
    assert 'parser.add_argument("--render-releases-root"' in text
    assert 'render_releases_root=args.render_releases_root' in text
    assert 'immutable releases root must be an absolute systemd-safe path with commit' in text


def test_readonly_capability_preflight_is_before_service_switch():
    text=(ROOT/'deploy/install_immutable_release.sh').read_text()
    assert 'systemd-run --user --quiet --wait --collect --pipe' in text
    assert 'release_readonly_preflight=failed current_not_switched' in text
    assert '_source_checkpoint "$RELEASE_DIR" console_verified\n_preflight_release_readonly' in text


def test_consumer_readonly_policy_covers_current_link_switches():
    text=(ROOT/'deploy/install_managed_systemd_dropin.py').read_text()
    assert 'unit_name.endswith("-gateway.service")' in text
    assert '"eimemory-rpc.service"' in text
    assert 'protected_path = rendered_root if consumer else rendered_root / render_commit' in text
