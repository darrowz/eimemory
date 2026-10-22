"""Request-owned Hermes context; never merge profile values into os.environ."""
from __future__ import annotations

from contextvars import copy_context
import os
from pathlib import Path
import re
import shlex
import stat
import threading
from typing import Any
from urllib.parse import urlsplit


IDENTITY_KEYS = ('EIMEMORY_TENANT_ID','EIMEMORY_AGENT_ID','EIMEMORY_DEPLOY_SCOPE_AGENT',
    'EIMEMORY_WORKSPACE_ID','EIMEMORY_DEPLOY_SCOPE_WORKSPACE','EIMEMORY_USER_ID')


def native_context(*, expected_home: str = '') -> dict[str, Any]:
    """Read native ContextVars, not a caller's tool metadata or process label."""
    try:
        from hermes_constants import get_hermes_home
    except ImportError:
        return {'native':False,'profile_name':'','hermes_home':str(Path(expected_home).expanduser().resolve()) if expected_home else ''}
    home=str(Path(get_hermes_home()).expanduser().resolve())
    if expected_home and str(Path(expected_home).expanduser().resolve())!=home:
        raise ValueError('hermes_native_profile_home_mismatch')
    try:
        from hermes_cli.profiles import get_active_profile_name
        profile=str(get_active_profile_name() or 'default')
    except ImportError:
        from hermes_constants import get_default_hermes_root
        root=Path(get_default_hermes_root()).resolve()
        path=Path(home)
        profile='default' if path==root else path.name if path.parent==root/'profiles' else 'custom'
    try:
        from agent.secret_scope import current_secret_scope_home
        secret_home=current_secret_scope_home()
    except ImportError:
        secret_home=None
    if secret_home and str(Path(secret_home).expanduser().resolve())!=home:
        raise ValueError('hermes_native_secret_home_mismatch')
    return {'native':True,'profile_name':profile,'hermes_home':home}


def profile_value(name: str, default: str | None = None) -> str | None:
    """Use the host's own profile secret resolution, including external sources."""
    try:
        from agent.secret_scope import get_secret
    except ImportError:
        return os.environ.get(name,default)
    # Native multiplexing raises on a missing ContextVar. Do not turn that into
    # a fallback to another profile's process environment.
    value=get_secret(name,default)
    return str(value) if value is not None else default


def current_home() -> str:
    context=native_context()
    return context['hermes_home'] if context['native'] else ''


def verify_owner(binding: dict[str, Any] | None) -> None:
    if not binding or not binding.get('native'):return
    current=native_context(expected_home=binding['hermes_home'])
    if current['profile_name']!=binding['profile_name']:
        raise ValueError('hermes_native_profile_owner_mismatch')
    try:
        from gateway.session_context import get_session_env
        user=get_session_env('HERMES_SESSION_USER_ID','')
    except ImportError:user=''
    expected_user=binding.get('scope',{}).get('user_id','')
    if user and expected_user and user!=expected_user:
        raise ValueError('hermes_native_request_user_mismatch')
    if binding.get('topology_digest'):
        from eimemory.contracts.hermes_topology import load_verified_topology
        if load_verified_topology()['topology_digest']!=binding['topology_digest']:
            raise ValueError('hermes_profile_topology_changed')


def spawn_owned_thread(target, *, owner_context=None, **kwargs):
    """Each worker gets a separate copy; contexts cannot be entered concurrently."""
    context=(owner_context.copy() if owner_context is not None else copy_context())
    args=kwargs.pop('args',())
    return threading.Thread(target=context.run,args=(target,*args),**kwargs)


def _env_file(path: Path) -> dict[str,str]:
    """Bounded, non-executing parser for the shared eiMemory service env file."""
    try:
        flags=os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_CLOEXEC',0)|getattr(os,'O_NONBLOCK',0)
        descriptor=os.open(path,flags)
    except FileNotFoundError:
        return {}
    try:
        before=os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_size>65536
                or os.name=='posix' and (before.st_uid not in {0,os.geteuid()}
                    or before.st_mode&0o027)):
            raise ValueError('hermes_rpc_environment_untrusted')
        raw=os.read(descriptor,65537)
        identity=lambda info:(info.st_dev,info.st_ino,info.st_mode,info.st_uid,info.st_gid,
            info.st_nlink,info.st_size,info.st_mtime_ns,info.st_ctime_ns)
        if (len(raw)!=before.st_size or identity(before)!=identity(os.fstat(descriptor))
                or identity(before)!=identity(path.lstat())):
            raise ValueError('hermes_rpc_environment_changed')
    finally:os.close(descriptor)
    values={}
    for line in raw.decode('utf-8-sig').splitlines():
        line=line.strip()
        if not line or line.startswith('#'):continue
        match=re.fullmatch(r'(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=\s*(.*)',line)
        if not match:raise ValueError('hermes_rpc_environment_invalid')
        key,value=match.groups()
        if key not in {'EIMEMORY_RPC_TOKEN','EIMEMORY_RPC_AUTH_TOKEN','EIMEMORY_RPC_URL'}:continue
        if key in values:raise ValueError('hermes_rpc_environment_duplicate')
        if value.startswith(('"',"'")):
            parts=shlex.split(value,comments=True,posix=True)
            if len(parts)!=1:raise ValueError('hermes_rpc_environment_invalid')
            value=parts[0]
        else:value=re.split(r'\s+#',value,maxsplit=1)[0].strip()
        values[key]=value
    return values


def _endpoint(value: str) -> tuple:
    parsed=urlsplit(value)
    return parsed.scheme,parsed.hostname,parsed.port,parsed.path.rstrip('/'),parsed.query


def rpc_configuration() -> dict[str,str]:
    """Profile credentials win; shared credentials stay on the shared endpoint."""
    shared_file=Path(os.environ.get('EIMEMORY_RPC_ENV_FILE','/etc/eimemory/rpc.env'))
    shared=_env_file(shared_file)
    shared_url=os.environ.get('EIMEMORY_RPC_URL','').strip() or shared.get('EIMEMORY_RPC_URL','') or 'http://127.0.0.1:8091/'
    local_url=profile_value('EIMEMORY_RPC_URL')
    url=str(local_url if local_url is not None else shared_url).strip()
    token=profile_value('EIMEMORY_RPC_TOKEN')
    if token is None:token=profile_value('EIMEMORY_RPC_AUTH_TOKEN')
    if token is None and _endpoint(url)==_endpoint(shared_url):
        token=(shared.get('EIMEMORY_RPC_TOKEN') or shared.get('EIMEMORY_RPC_AUTH_TOKEN')
            or os.environ.get('EIMEMORY_RPC_TOKEN','') or os.environ.get('EIMEMORY_RPC_AUTH_TOKEN',''))
    return {'base_url':url,'auth_token':str(token or '').strip()}


def resolve_host_binding(context: dict[str,Any]) -> dict[str,Any]:
    """Bind the host request to the shared, independently verified declaration."""
    native=native_context(expected_home=str(context.get('hermes_home') or ''))
    if not native['native']:return native
    from eimemory.contracts.hermes_topology import (
        load_topology,resolve_profile_binding,resolve_operator_binding,
    )
    topology=load_topology()
    if topology is None:
        if native['profile_name']!='default':raise ValueError('hermes_profile_identity_unbound')
        return native
    user=str(context.get('user_id') or '').strip()
    if not user:
        # Native CLI/maintenance contexts omit a platform user. They may use
        # only the independently declared local operator scope, never a
        # generic USER_ID from the gateway process or a profile's dotenv.
        local_platforms={'cli','cron','desktop','oneshot','subagent','flush'}
        local_owner=(os.name=='posix' and os.getuid()==os.geteuid()==topology['runtime_uid']
                     and os.getgid()==os.getegid()==topology['runtime_gid'])
        if str(context.get('platform') or '') not in local_platforms or not local_owner:
            raise ValueError('hermes_profile_identity_unbound')
        binding=resolve_operator_binding(native['profile_name'],native['hermes_home'],topology=topology)
    else:
        workspace=str(context.get('agent_workspace') or '').strip()
        binding=resolve_profile_binding(native['profile_name'],native['hermes_home'],user_id=user,
            workspace_id=workspace if workspace not in {'','default','hermes'} else None,topology=topology)
    if binding.get('identity_status')=='unbound' or not binding.get('scope'):
        raise ValueError('hermes_profile_identity_unbound')
    return {**native,**binding}


def source_home(gateway: Any,source: Any,explicit: str | Path | None = None) -> Path:
    """Resolve native pinned routing before freezing a delivery callback owner."""
    resolved=None
    authoritative=False
    try:
        from gateway.session_identity import identity_of
        identity=identity_of(source)
        if identity is not None:
            resolved=Path(identity.runtime_home)
            authoritative=True
    except ImportError:pass
    if resolved is None:
        resolver=getattr(gateway,'_resolve_profile_home_for_source',None)
        if callable(resolver):
            resolved=Path(resolver(source))
            authoritative=True
    if resolved is None:
        active=current_home()
        resolved=Path(active) if active else Path(os.environ.get('HERMES_HOME','') or Path.home()/'.hermes')
    resolved=resolved.expanduser().resolve()
    if explicit is not None and Path(explicit).expanduser().resolve()!=resolved:
        # Explicit fixtures/legacy callers without a native routing API own
        # their supplied home. A live routed source may never be overridden.
        if authoritative or current_home():
            raise ValueError('hermes_delivery_profile_home_mismatch')
        resolved=Path(explicit).expanduser().resolve()
    return resolved
