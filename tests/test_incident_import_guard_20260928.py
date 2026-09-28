"""Real child processes; no audit hook is installed into the pytest process."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import pytest

SOURCE = Path(__file__).resolve().parents[1]


def package(root):
    p=root/'eimemory';(p/'core').mkdir(parents=True)
    shutil.copyfile(SOURCE/'eimemory/__init__.py',p/'__init__.py')
    shutil.copyfile(SOURCE/'eimemory/core/release_source_guard.py',p/'core/release_source_guard.py')
    (p/'version.py').write_text('__version__ = "fixture"\n')
    (p/'api').mkdir();(p/'api/runtime.py').write_text('class Runtime: pass\n')
    return p


def run(root, code, *args, flags=('-I','-B')):
    return subprocess.run([sys.executable,*flags,'-c',
        'import sys;sys.path.insert(0,sys.argv[1]);'+code,str(root),*map(str,args)],
        capture_output=True,text=True,timeout=10)


def test_leaf_import_does_not_initialize_runtime(tmp_path):
    package(tmp_path)
    p=run(tmp_path,'import eimemory.version;print("eimemory.api.runtime" in sys.modules)')
    assert p.returncode==0 and p.stdout.strip()=='False'


def test_public_runtime_import_still_available(tmp_path):
    package(tmp_path)
    p=run(tmp_path,'from eimemory import Runtime;import eimemory;print(Runtime is eimemory.Runtime)')
    assert p.returncode==0 and p.stdout.strip()=='True'


@pytest.mark.parametrize('operation', ['write','compileall','rename'])
def test_release_denied_write_retains_pid_and_stack(tmp_path, operation):
    root=tmp_path/'releases'/('a'*40);root.mkdir(parents=True);package(root)
    code='import eimemory,pathlib,json,os;'
    if operation=='write':code+='pathlib.Path(sys.argv[1],"created.pyc").write_bytes(b"x")'
    elif operation=='rename':code+='os.rename(pathlib.Path(sys.argv[1],"eimemory/version.py"),pathlib.Path(sys.argv[1],"gone.py"))'
    else:code+='import compileall;sys.exit(0 if compileall.compile_file(str(pathlib.Path(sys.argv[1],"eimemory/version.py")),force=True,quiet=2) else 1)'
    p=run(root,code)
    assert p.returncode!=0
    lines=[json.loads(x) for x in p.stderr.splitlines() if x.startswith('{')]
    assert lines and lines[0]['event']=='eimemory_release_write_denied'
    assert lines[0]['pid']>0 and lines[0]['stack']
    assert list(root.rglob('*.pyc'))==[]
    assert (root/'eimemory/version.py').exists()


def test_developer_checkout_not_implicitly_readonly(tmp_path):
    package(tmp_path)
    p=run(tmp_path,'import eimemory,pathlib;pathlib.Path(sys.argv[1],"allowed.txt").write_text("ok")')
    assert p.returncode==0 and (tmp_path/'allowed.txt').exists()


def test_init_alone_cannot_prevent_its_own_preexecution_cache(tmp_path):
    # Explicitly retain this limitation: never claim __init__ replaces -B or RO mounts.
    root=tmp_path/'releases'/('a'*40);root.mkdir(parents=True);package(root)
    p=run(root,'import eimemory.version',flags=('-I',))
    assert p.returncode==0
    files=list(root.rglob('*.pyc'))
    assert len(files)==1 and files[0].name.startswith('__init__.')


def test_unprivileged_import_and_explicit_compile_cannot_write_readonly_source():
    if os.name!='posix' or not hasattr(os,'geteuid') or os.geteuid()!=0:
        pytest.skip('requires a privileged isolated test process to drop uid')
    import pwd
    uid=pwd.getpwnam('nobody').pw_uid;gid=pwd.getpwnam('nobody').pw_gid
    with tempfile.TemporaryDirectory(prefix='eimemory-ro-permissions-') as temp:
        base=Path(temp);root=base/'releases'/('a'*40);root.mkdir(parents=True);package(root)
        for path in base.rglob('*'):
            path.chmod(0o555 if path.is_dir() else 0o444)
        base.chmod(0o755)
        def drop():
            os.setgroups([]);os.setgid(gid);os.setuid(uid)
        child='import sys,compileall;sys.path.insert(0,sys.argv[1]);import eimemory.version;print(compileall.compile_dir(sys.argv[1],force=True,quiet=2))'
        p=subprocess.run([sys.executable,'-I','-c',child,str(root)],text=True,capture_output=True,timeout=10,preexec_fn=drop)
        assert p.returncode==0 and p.stdout.rstrip().endswith('False')
        assert list(root.rglob('*.pyc'))==[]


def test_linux_readonly_bind_mount_covers_import_and_explicit_compile(tmp_path):
    if sys.platform != 'linux' or not shutil.which('unshare') or not shutil.which('mount'):
        pytest.skip('Linux user/mount namespace tools required')
    capability = subprocess.run(['unshare', '--user', '--map-root-user', '--mount', 'true'],
                                capture_output=True, timeout=10)
    if capability.returncode:
        pytest.skip('host does not allow unprivileged mount namespaces')
    root=tmp_path/'releases'/('a'*40);root.mkdir(parents=True);package(root)
    sibling=root.parent/('b'*40);sibling.mkdir();(sibling/'other.py').write_text('x=1\n')
    probe=tmp_path/'probe.py'
    probe.write_text('''import sys,os,compileall,pathlib,json
root=pathlib.Path(sys.argv[1]); outside=pathlib.Path(sys.argv[2])
assert os.statvfs(root).f_flag & os.ST_RDONLY
# No -B: the filesystem blocks the __init__ cache before Python executes it.
sys.path.insert(0,str(root)); import eimemory.version
assert not list(root.rglob('*.pyc'))
sys.dont_write_bytecode=False
assert compileall.compile_dir(str(root),force=True,quiet=2) is False
try: (root/'native-write.txt').write_text('denied')
except OSError: pass
else: raise AssertionError('write unexpectedly allowed')
try: (root.parent/('b'*40)/'other.pyc').write_bytes(b'denied')
except OSError: pass
else: raise AssertionError('sibling release write unexpectedly allowed')
outside.write_text('runtime-state-remains-writable')
assert not list(root.rglob('*.pyc'))
print(json.dumps({'readonly':True,'outside_writable':True,'bytecode_count':0}))
''')
    command=['unshare','--user','--map-root-user','--mount','--propagation','private',
             'sh','-ec','mount --bind "$5" "$5"; mount -o remount,bind,ro "$5"; exec "$2" -I "$3" "$1" "$4"',
             'mount-probe',str(root),sys.executable,str(probe),str(tmp_path/'outside-state'),str(root.parent)]
    result=subprocess.run(command,capture_output=True,text=True,timeout=20)
    assert result.returncode==0, result.stderr+result.stdout
    assert json.loads(result.stdout.splitlines()[-1])=={'readonly':True,'outside_writable':True,'bytecode_count':0}
    assert list(root.rglob('*.pyc'))==[]
