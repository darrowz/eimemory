#!/usr/bin/env python3
"""Build and verify a user-owned immutable candidate without touching serving state."""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import secrets

sys.dont_write_bytecode=True


def run(argv,**kwargs):
    return subprocess.run([str(item) for item in argv],check=True,timeout=1800,**kwargs)


def git(repository,*args):
    return run(['/usr/bin/git','--no-optional-locks','-c','core.fsmonitor=false',
        '-C',repository,*args],capture_output=True,text=True).stdout.strip()


def coordinates():
    import pwd
    if os.geteuid()<=0 or os.getuid()!=os.geteuid():
        raise ValueError('user_release_nonroot_identity_required')
    value=pwd.getpwuid(os.geteuid());home=Path(value.pw_dir)
    return home/'.local/state/eimemory/control-plane',home/'.local/lib/eimemory/control-plane/empty-pycache'


def private_directory(path,mode=0o750):
    path=Path(path)
    if not path.is_absolute() or '..' in path.parts:raise ValueError('user_release_directory_invalid')
    missing=[];parent=path
    while not parent.exists():
        missing.append(parent);parent=parent.parent
    for item in reversed(missing):
        item.mkdir(mode=mode if item==path else 0o750)
        if item==path:
            # mkdir respects the service umask; establish the exact new leaf mode.
            fd=os.open(item,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            try:os.fchmod(fd,mode)
            finally:os.close(fd)
    for parent in (path,*path.parents):
        info=parent.lstat()
        if not __import__('stat').S_ISDIR(info.st_mode) or info.st_uid not in {0,os.geteuid()} or info.st_mode&0o022:
            raise ValueError('user_release_directory_untrusted')
    if path.lstat().st_uid!=os.geteuid():raise ValueError('user_release_directory_foreign_owner')


def verify(repository,commit,releases,record):
    root=releases/commit
    if root.is_symlink() or not root.is_dir() or root.resolve()!=root:
        raise ValueError('user_release_candidate_missing_or_untrusted')
    if record.get('schema')!='eimemory.prepared_user_release.v1' or record.get('commit')!=commit:
        raise ValueError('user_release_preparation_identity_invalid')
    if (record.get('repository_root')!=str(repository) or record.get('release_dir')!=str(root)
            or record.get('source_tree')!=git(repository,'rev-parse',commit+'^{tree}')
            or record.get('activated') is not False or record.get('release_accepted') is not False):
        raise ValueError('user_release_preparation_binding_invalid')
    _,cache=coordinates()
    python=[sys.executable,'-I','-B','-X','pycache_prefix='+str(cache)]
    run([*python,root/'deploy/clean_release_bytecode.py','--validate-source',
        '--release-dir',root,'--releases-root',releases,'--repo-root',repository,'--commit',commit])
    run([*python,Path(__file__).with_name('clean_release_bytecode.py'),'--verify-source-seal',
        '--release-dir',root,'--releases-root',releases,'--repo-root',repository,'--commit',commit])
    hashes=record.get('console_script_hashes')
    if not isinstance(hashes,dict) or set(hashes)!={'eimemory','eimemory-qmd','pip','pip3'}:
        raise ValueError('user_release_console_script_inventory_incomplete')
    for name,digest in hashes.items():
        if name not in {'eimemory','eimemory-qmd','pip','pip3'}:
            raise ValueError('user_release_console_script_invalid')
        path=root/'.venv/bin'/name
        if path.is_symlink() or sha256(path.read_bytes()).hexdigest()!=digest:
            raise ValueError('user_release_console_script_changed')
    run([root/'.venv/bin/python','-I','-B','-X','pycache_prefix='+str(cache),'-m','pip','check'])
    run([root/'.venv/bin/python','-I','-B','-X','pycache_prefix='+str(cache),'-c',
         'import pypdf,psycopg,yaml; from psycopg.rows import dict_row; assert callable(yaml.safe_load)'])
    run([root/'.venv/bin/python','-I','-B','-X','pycache_prefix='+str(cache),
        root/'.venv/bin/eimemory','--help'],stdout=subprocess.DEVNULL)
    return root


def allocate_stage(releases,commit):
    # The immutable-release contract admits alphanumeric stage suffixes.
    stage=Path(releases)/('.eimemory-stage-'+commit+'-'+secrets.token_hex(12))
    stage.mkdir(mode=0o750)
    return stage


def prepare(repository,commit,releases):
    state,cache=coordinates()
    for directory in (state/'prepared-releases',releases):private_directory(directory)
    if not cache.exists():private_directory(cache,0o555)
    info=cache.lstat()
    if cache.is_symlink() or not cache.is_dir() or info.st_uid!=os.geteuid() or info.st_mode&0o777!=0o555:
        raise ValueError('user_release_pycache_untrusted')
    if any(cache.iterdir()):raise ValueError('user_release_pycache_not_empty')
    root=releases/commit
    receipt=state/'prepared-releases'/(commit+'.json')
    if root.exists() or root.is_symlink() or receipt.exists():
        raise ValueError('user_release_existing_candidate_preserved')
    stage=allocate_stage(releases,commit)
    archive=stage.with_suffix('.tar')
    try:
        run(['/usr/bin/git','-C',repository,'archive','--format=tar','--output',archive,commit])
        import tarfile
        with tarfile.open(archive) as source:source.extractall(stage,filter='data')
        python=[sys.executable,'-I','-B','-X','pycache_prefix='+str(cache)]
        run([*python,stage/'deploy/clean_release_bytecode.py','--validate-source','--allow-stage',
            '--release-dir',stage,'--releases-root',releases,'--repo-root',repository,'--commit',commit])
        run([*python,'-m','venv',stage/'.venv'])
        run([stage/'.venv/bin/python','-I','-B','-X','pycache_prefix='+str(cache),
             '-m','pip','install','--no-compile',str(stage)+'[postgres,pdf,hermes]'])
        run([stage/'.venv/bin/python','-I','-B','-X','pycache_prefix='+str(cache),'-m','pip','check'])
        run([*python,stage/'deploy/clean_release_bytecode.py','--validate-source','--allow-stage',
            '--release-dir',stage,'--releases-root',releases,'--repo-root',repository,'--commit',commit])
        stage.rename(root)
        run([*python,root/'deploy/clean_release_bytecode.py','--relocate-venv',
            '--release-dir',root,'--releases-root',releases,'--from-stage',stage,'--to-release',root])
        run([*python,Path(__file__).with_name('clean_release_bytecode.py'),'--seal-source',
            '--release-dir',root,'--releases-root',releases,'--repo-root',repository,'--commit',commit])
        record={'schema':'eimemory.prepared_user_release.v1','commit':commit,
            'repository_root':str(repository),'source_tree':git(repository,'rev-parse',commit+'^{tree}'),
            'release_dir':str(root),'activated':False,'release_accepted':False,
            'console_script_hashes':{name:sha256((root/'.venv/bin'/name).read_bytes()).hexdigest()
                for name in ('eimemory','eimemory-qmd','pip','pip3') if (root/'.venv/bin'/name).is_file()}}
        verify(repository,commit,releases,record)
        temporary=receipt.with_suffix('.tmp')
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as stream:
            stream.write(json.dumps(record,sort_keys=True,separators=(',',':')).encode())
            stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,receipt)
        parent=os.open(receipt.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:os.fsync(parent)
        finally:os.close(parent)
        print(json.dumps({'prepared':True,'commit':commit,'receipt':str(receipt),
            'activated':False,'release_accepted':False}))
    finally:archive.unlink(missing_ok=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=('prepare','verify'))
    parser.add_argument('--repository-root',type=Path,required=True)
    parser.add_argument('--commit',required=True)
    parser.add_argument('--releases-root',type=Path,default=Path('/opt/eimemory/releases'))
    args=parser.parse_args()
    coordinates()
    if re.fullmatch('[0-9a-f]{40}',args.commit) is None:raise ValueError('user_release_full_commit_required')
    repository=args.repository_root
    if repository.resolve()!=repository or repository.is_symlink():raise ValueError('user_release_repository_untrusted')
    if git(repository,'rev-parse',args.commit+'^{commit}')!=args.commit:raise ValueError('user_release_commit_unverified')
    if args.operation=='prepare':prepare(repository,args.commit,args.releases_root)
    else:
        state,_=coordinates()
        # The same reviewed user authority reader seals receipt descriptor/name identity.
        sys.path.insert(0,str(repository))
        from eimemory.contracts.user_control_plane import read_file
        raw=read_file(state/'prepared-releases'/(args.commit+'.json'),modes=(0o600,))
        from eimemory.core.strict_json import loads
        record=loads(raw)
        verify(repository,args.commit,args.releases_root,record)
        print(json.dumps({'prepared':True,'commit':args.commit,'activated':False,'release_accepted':False}))


if __name__=='__main__':main()
