#!/usr/bin/python3
"""Preserve legacy service settings as data, never as root-executed unit code.

The root bootstrap only extracts directives from its original user snapshot.
Preview and live EnvironmentFile reads run as the runtime user. No shell,
interpolation, inherited command, user/group directive or privilege prefix is
executed. Authority and interpreter coordinates remain root controlled.
"""
from __future__ import annotations
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import stat
import sys

SCHEMA='eimemory.legacy_runtime_settings.v1'
LIMIT=65536
PROTECTED={'HOME','USER','LOGNAME','SHELL','XDG_RUNTIME_DIR','DBUS_SESSION_BUS_ADDRESS',
    'OPENCLAW_TENANT','OPENCLAW_AGENT','OPENCLAW_WORKSPACE','OPENCLAW_USER'}
PREFIXES=('PYTHON','LD_','EIMEMORY_RUNTIME_','EIMEMORY_DEPLOY_',
    'EIMEMORY_SYSTEMD_','EIMEMORY_TRUSTED_','EIMEMORY_CODE_EVOLUTION_',
    'EIMEMORY_CODE_AUTOMATION_','EIMEMORY_ATTESTATION_')
FLAGS={'--host','--port','--loopback-health-host','--loopback-health-port'}


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()


def permitted(name):
    return (re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*',name) is not None
        and name not in PROTECTED and not name.startswith(PREFIXES))


def lines(text):
    pending=''
    for raw in text.splitlines():
        line=pending+raw.strip()
        backslashes=len(line)-len(line.rstrip('\\'))
        if backslashes%2:
            pending=line[:-1];continue
        pending=''
        yield line
    if pending:raise ValueError('manager_settings_incomplete_line')


def expand_specifiers(value):
    replacements={'%':'%','h':'/home/darrow','u':'darrow','U':'1001'}
    def replace(match):
        if match[1] not in replacements:raise ValueError('manager_settings_unsupported_specifier')
        return replacements[match[1]]
    return re.sub(r'%(.)',replace,value)


def rpc_arguments(command):
    argv=shlex.split(command,posix=True)
    if not argv:return None
    if (re.fullmatch(r'/opt/eimemory/(?:current|releases/[0-9a-f]{40})/\.venv/bin/python',argv[0]) is None
            or 'serve-eibrain-rpc' not in argv):
        raise ValueError('manager_settings_legacy_rpc_command_unsupported')
    position=argv.index('serve-eibrain-rpc')
    if argv[position-2:position]!=['-m','eimemory.cli.main']:
        raise ValueError('manager_settings_legacy_rpc_command_unsupported')
    prefix=argv[1:position-2];i=0
    while i<len(prefix):
        if prefix[i] in ('-I','-B','-s','-E','-u'):i+=1
        elif prefix[i]=='-X' and i+1<len(prefix) and prefix[i+1].startswith('pycache_prefix='):i+=2
        else:raise ValueError('manager_settings_legacy_rpc_command_unsupported')
    values={'--host':'127.0.0.1','--port':'8091'};i=position+1
    while i<len(argv):
        flag,sep,value=argv[i].partition('=')
        if flag not in FLAGS:raise ValueError('manager_settings_rpc_argument_unsupported')
        if not sep:
            if i+1>=len(argv):raise ValueError('manager_settings_rpc_argument_missing')
            i+=1;value=argv[i]
        if not value or any(x in value for x in '\r\n\0'):raise ValueError('manager_settings_rpc_argument_invalid')
        values[flag]=value;i+=1
    for name in ('--port','--loopback-health-port'):
        if name in values:
            if not values[name].isdigit() or not 0<=int(values[name])<=65535:raise ValueError('manager_settings_rpc_port_invalid')
            values[name]=str(int(values[name]))
    if ('--loopback-health-host' in values)!=('--loopback-health-port' in values):
        raise ValueError('manager_settings_rpc_auxiliary_address_incomplete')
    if '--loopback-health-port' in values:
        main=values['--host'].lower();aux=values['--loopback-health-host'].lower()
        main='127.0.0.1' if main=='localhost' else main
        aux='127.0.0.1' if aux=='localhost' else aux
        # The main HTTP server already serves health on this same endpoint.
        # A distinct Tailscale bind plus a loopback bind remains distinct.
        if values['--port']!='0' and values['--port']==values['--loopback-health-port'] and (main==aux or (main=='0.0.0.0' and aux=='127.0.0.1')):
            values.pop('--loopback-health-host');values.pop('--loopback-health-port')
    return [item for flag in ('--host','--port','--loopback-health-host','--loopback-health-port') if flag in values for item in (flag,values[flag])]


def build(snapshot,units):
    if snapshot.get('schema')!='eimemory.runtime_owner_user_files.v1':raise ValueError('manager_settings_snapshot_invalid')
    result={}
    for unit in sorted(units):
        environment={};files=[];command=None
        names=[unit,*sorted(name for name in snapshot['files'] if name.startswith(unit+'.d/') and name.endswith('.conf') and not name.endswith('/zzzz-eimemory-host-manager.conf'))]
        for name in names:
            record=snapshot['files'].get(name)
            if record is None or record.get('kind')=='mask':continue
            raw=bytes.fromhex(record['raw_hex'])
            if len(raw)>LIMIT or sha256(raw).hexdigest()!=record['sha256']:raise ValueError('manager_settings_snapshot_digest_invalid')
            section=None
            for line in lines(raw.decode('utf-8')):
                if not line or line.startswith(('#',';')):continue
                if line.startswith('[') and line.endswith(']'):section=line;continue
                if section!='[Service]':continue
                key,separator,value=line.partition('=')
                if not separator:continue
                if key=='Environment':
                    if not value.strip():environment.clear();continue
                    for token in shlex.split(value,posix=True):
                        k,sep,v=token.partition('=')
                        if not sep or re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*',k) is None:raise ValueError('manager_settings_environment_invalid')
                        if permitted(k):environment[k]=expand_specifiers(v)
                elif key=='EnvironmentFile':
                    if not value.strip():files.clear();continue
                    for token in shlex.split(value,posix=True):
                        optional=token.startswith('-');path=expand_specifiers(token[1:] if optional else token)
                        parsed=PurePosixPath(path)
                        if (not parsed.is_absolute() or parsed.as_posix()!=path or '..' in parsed.parts
                                or any(c in path for c in '\r\n\0*?[')):
                            raise ValueError('manager_settings_environment_file_path_invalid')
                        files.append({'path':path,'optional':optional})
                elif key=='ExecStart' and unit=='eimemory-rpc.service':command=value or None
        result[unit]={'environment':environment,'environment_files':files,
            'rpc_argv':rpc_arguments(command) if command is not None else None}
    value={'schema':SCHEMA,'source_snapshot_sha256':sha256(canonical(snapshot)).hexdigest(),'units':result}
    validate(value)
    if len(canonical(value))>LIMIT:raise ValueError('manager_settings_output_limit')
    return value


def validate(value):
    if (not isinstance(value,dict) or set(value)!={'schema','source_snapshot_sha256','units'} or value['schema']!=SCHEMA
            or re.fullmatch('[0-9a-f]{64}',value['source_snapshot_sha256'] or '') is None
            or not isinstance(value['units'],dict) or len(value['units'])>100):
        raise ValueError('manager_settings_manifest_invalid')
    for unit,item in value['units'].items():
        if re.fullmatch(r'eimemory-[A-Za-z0-9_-]+\.service',unit) is None or set(item)!={'environment','environment_files','rpc_argv'}:
            raise ValueError('manager_settings_unit_invalid')
        if (not isinstance(item['environment'],dict) or any(not permitted(k) or not isinstance(v,str) or '\0' in v for k,v in item['environment'].items())
                or not isinstance(item['environment_files'],list) or len(item['environment_files'])>100):
            raise ValueError('manager_settings_environment_invalid')
        for f in item['environment_files']:
            if (not isinstance(f,dict) or set(f)!={'path','optional'} or type(f['optional']) is not bool
                    or not isinstance(f['path'],str) or not f['path'].startswith('/') or '..' in PurePosixPath(f['path']).parts
                    or any(c in f['path'] for c in '\r\n\0*?[')):
                raise ValueError('manager_settings_environment_file_path_invalid')
        args=item['rpc_argv']
        if args is not None:
            if unit!='eimemory-rpc.service' or not isinstance(args,list) or len(args)>8 or len(args)%2:
                raise ValueError('manager_settings_rpc_argument_invalid')
            if any(not isinstance(x,str) or '\0' in x for x in args):raise ValueError('manager_settings_rpc_argument_invalid')
            if rpc_arguments('/opt/eimemory/current/.venv/bin/python -m eimemory.cli.main serve-eibrain-rpc '+shlex.join(args))!=args:
                raise ValueError('manager_settings_rpc_argument_invalid')
    return value


def environment_file(path,optional=False):
    # Only runtime-user reads are permitted; root never reads these paths.
    if os.getuid()==0 or os.geteuid()==0:raise ValueError('manager_settings_runtime_uid_required')
    try:fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
    except FileNotFoundError:
        if optional:return {}
        raise ValueError('manager_settings_environment_file_missing') from None
    try:
        before=os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size>LIMIT:raise ValueError('manager_settings_environment_file_untrusted')
        raw=b''
        while len(raw)<=LIMIT:
            chunk=os.read(fd,LIMIT+1-len(raw))
            if not chunk:break
            raw+=chunk
        after=os.fstat(fd);named=Path(path).lstat()
        key=lambda s:(s.st_dev,s.st_ino,s.st_mode,s.st_uid,s.st_gid,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
        if len(raw)>LIMIT or key(before)!=key(after) or key(after)!=key(named):raise ValueError('manager_settings_environment_file_changed')
    finally:os.close(fd)
    result={}
    for line in lines(raw.decode('utf-8-sig')):
        if not line or line.startswith(('#',';')):continue
        name,separator,value=line.partition('=');name=name.strip();value=value.strip()
        if not separator or re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*',name) is None:raise ValueError('manager_settings_environment_file_syntax')
        if value.startswith(('"',"'")):
            tokens=shlex.split(value,posix=True)
            if len(tokens)!=1:raise ValueError('manager_settings_environment_file_syntax')
            value=tokens[0]
        else:value=re.sub(r'\\(.)',lambda match:match[1],value)
        if '\0' in value:raise ValueError('manager_settings_environment_file_syntax')
        if permitted(name):result[name]=value
    return result


def values(item):
    environment=dict(item['environment'])
    for f in item['environment_files']:environment.update(environment_file(f['path'],f['optional']))
    return environment


def apply(value,unit,module,args):
    validate(value)
    if unit not in value['units']:raise ValueError('manager_settings_unit_not_registered')
    item=value['units'][unit];os.environ.update(values(item))
    if item['rpc_argv'] is not None:
        if module!='eimemory.cli.main' or 'serve-eibrain-rpc' not in args:raise ValueError('manager_settings_rpc_module_mismatch')
        index=args.index('serve-eibrain-rpc')
        # Validate the reviewed new command's tail too; never drop new flags.
        rpc_arguments('/opt/eimemory/current/.venv/bin/python -m eimemory.cli.main '+shlex.join(args[index:]))
        args=[*args[:index+1],*item['rpc_argv']]
    return args


def main():
    if sys.argv[1:]!=['--preview'] or os.getuid()!=1001 or os.geteuid()!=1001:raise ValueError('manager_settings_preview_runtime_uid_required')
    raw=sys.stdin.buffer.read(LIMIT+1)
    if len(raw)>LIMIT:raise ValueError('manager_settings_output_limit')
    value=validate(json.loads(raw));report={}
    for unit,item in value['units'].items():
        report[unit]={'environment_names':sorted(values(item)),'rpc_argv':item['rpc_argv']}
    print(json.dumps({'ok':True,'schema':SCHEMA,'manifest_sha256':sha256(raw).hexdigest(),'units':report},sort_keys=True,separators=(',',':')))


if __name__=='__main__':main()
