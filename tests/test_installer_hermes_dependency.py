"""Managed interpreters must supply their own Hermes configuration parser."""
from hashlib import sha256
import importlib.util
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tomllib
import venv

import pytest


def test_hermes_parser_is_a_declared_optional_package_dependency():
    project=tomllib.loads(Path('pyproject.toml').read_text(encoding='utf-8'))['project']
    assert project['dependencies']==[]
    assert project['optional-dependencies']['hermes']==['PyYAML>=6,<7']


def test_standalone_installer_explains_missing_extra_before_native_mutation(monkeypatch,tmp_path):
    path=Path('deploy/install_hermes_integration.py').resolve()
    spec=importlib.util.spec_from_file_location('missing_hermes_parser_installer',path)
    installer=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    monkeypatch.setitem(sys.modules,'yaml',None)
    with pytest.raises(RuntimeError,match=r'eimemory\[hermes\]'):
        installer.install_hermes_integration(release_root=tmp_path/'missing-release',hermes_home=tmp_path/'native')
    assert list(tmp_path.iterdir())==[]


@pytest.mark.skipif(sys.platform!='linux',reason='Native managed dependency stage')
@pytest.mark.parametrize('staged_has_yaml',[True,False])
def test_immutable_deployment_installs_extra_and_checks_real_staged_import(tmp_path,staged_has_yaml):
    from test_installer_pdf_dependency import interpreter
    prior,stage=tmp_path/'prior',tmp_path/'stage'
    interpreter(prior,pdf=False,prior=True)
    python=interpreter(stage,pdf=False)
    site=Path(subprocess.check_output([str(python),'-I','-B','-c',
        "import sysconfig; print(sysconfig.get_path('purelib'))"],text=True).strip())
    if not staged_has_yaml:(site/'yaml.py').unlink()
    script=Path('deploy/install_immutable_release.sh').read_text()
    start=script.index('"$STAGE_DIR/.venv/bin/python" -I -B -X pycache_prefix="$CONTROL_PYCACHE_PREFIX" -m pip install --no-compile --no-deps "$STAGE_DIR"')
    end=script.index('_source_checkpoint "$STAGE_DIR" build_outputs_checked',start)
    log=tmp_path/'pip.log'
    env={**os.environ,'EIMEMORY_INSTALL_PDF_EXTRA':'0','EIMEMORY_INSTALL_POSTGRES_EXTRA':'0',
         'PIP_TEST_LOG':str(log)}
    result=subprocess.run(['bash','-c','set -euo pipefail\n'+
        f'STAGE_DIR={shlex.quote(str(stage))}\nPREVIOUS_CURRENT={shlex.quote(str(prior))}\n'+
        script[start:end]+'echo candidate_dependencies_verified\n'],env=env,text=True,capture_output=True)
    assert '[hermes]' in log.read_text()
    assert 'check' in log.read_text()
    if staged_has_yaml:
        assert result.returncode==0,result.stderr
        assert 'candidate_dependencies_verified' in result.stdout
    else:
        assert result.returncode!=0
        assert 'hermes_dependency=failed staged_import' in result.stderr
        assert 'candidate_dependencies_verified' not in result.stdout


@pytest.mark.skipif(sys.platform!='linux',reason='Native prepared release interpreter')
@pytest.mark.parametrize('staged_has_yaml',[True,False])
def test_user_preparation_verifies_actual_hermes_import_before_accepting_receipt(tmp_path,monkeypatch,staged_has_yaml):
    spec=importlib.util.spec_from_file_location('prepared_hermes_dependency',Path('deploy/prepare_user_release.py').resolve())
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    commit='a'*40
    release=tmp_path/commit
    venv.EnvBuilder(with_pip=False).create(release/'.venv')
    python=release/'.venv/bin/python'
    site=Path(subprocess.check_output([str(python),'-I','-B','-c',
        "import sysconfig; print(sysconfig.get_path('purelib'))"],text=True).strip())
    (site/'pypdf.py').write_text('')
    (site/'psycopg').mkdir()
    (site/'psycopg/__init__.py').write_text('')
    (site/'psycopg/rows.py').write_text('dict_row=object()\n')
    if staged_has_yaml:(site/'yaml.py').write_text('def safe_load(value): return {}\n')
    hashes={}
    for name in ('eimemory','eimemory-qmd','pip','pip3'):
        path=release/'.venv/bin'/name
        path.write_bytes(b'reviewed console')
        hashes[name]=sha256(path.read_bytes()).hexdigest()
    receipt=dict(schema='eimemory.prepared_user_release.v1',commit=commit,
        repository_root=str(tmp_path),release_dir=str(release),source_tree='tree',activated=False,
        release_accepted=False,console_script_hashes=hashes)
    monkeypatch.setattr(module,'coordinates',lambda:(tmp_path,tmp_path/'cache'))
    monkeypatch.setattr(module,'git',lambda *args:'tree')
    calls=[]
    def run(argv,**kwargs):
        calls.append(argv)
        if '-c' in argv and 'import pypdf,psycopg,yaml' in argv[-1]:
            return subprocess.run([str(item) for item in argv],check=True,capture_output=True,text=True)
    monkeypatch.setattr(module,'run',run)
    if staged_has_yaml:
        assert module.verify(tmp_path,commit,tmp_path,receipt)==release
        assert any('--help' in argv for argv in calls)
    else:
        with pytest.raises(subprocess.CalledProcessError):module.verify(tmp_path,commit,tmp_path,receipt)
        assert not any('--help' in argv for argv in calls)
