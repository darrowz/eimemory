"""Explicit, digest-checked repair of selected native Hermes memory lines.

This is a one-time operator import, not a second authority or automatic mirror.
The native file is never changed; subsequent edits use the existing provider.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import stat

from eimemory.adapters.runtime.service import AgentRuntimeMemoryService
from eimemory.api.runtime import Runtime
from eimemory.models.records import ScopeRef


def import_native_memory_lines(runtime, *, path, lines, expected_digest, scope, apply=False):
    path = Path(path)
    if path.name not in {'MEMORY.md', 'USER.md'} or path.is_symlink():
        raise ValueError('native_memory_file_invalid')
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0))
    with os.fdopen(descriptor, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
            raise ValueError('native_memory_file_invalid')
        raw = handle.read(65537)
    digest = sha256(raw).hexdigest()
    if digest != expected_digest or len(raw) > 65536:
        raise ValueError('native_memory_digest_mismatch')
    if (not isinstance(lines, list) or not 1 <= len(lines) <= 50 or len(set(lines)) != len(lines)
            or any(isinstance(n, bool) or not isinstance(n, int) for n in lines)):
        raise ValueError('native_memory_lines_invalid')
    contents = raw.decode('utf-8-sig').splitlines()
    selected = []
    for number in lines:
        if not 1 <= number <= len(contents):
            raise ValueError('native_memory_line_invalid')
        text = contents[number-1].strip()
        if not 8 <= len(text) <= 4000 or text.startswith('#'):
            raise ValueError('native_memory_line_invalid')
        selected.append((number, text, sha256(text.encode('utf-8')).hexdigest()))
    report = {'ok':True, 'schema':'hermes_native_memory_import.v1', 'applied':bool(apply),
              'source_faithful':True, 'file_digest':digest, 'lines':lines, 'record_ids':[],
              'natural_benchmark_eligible':False}
    if apply:
        service = AgentRuntimeMemoryService(runtime)
        path_digest = sha256(str(path.resolve()).encode('utf-8')).hexdigest()
        for number, text, text_digest in selected:
            result = service.remember(channel='hermes', scope=scope,
                text=text, title=text[:72], memory_type='preference' if path.name == 'USER.md' else 'durable_fact',
                event_id=f'native-import:{path_digest}:{text_digest}',
                meta={'native_import':{'schema':report['schema'], 'file':str(path.resolve()),
                                      'file_digest':digest, 'line':number, 'text_digest':text_digest}})
            captured = result.get('record') or {}
            if result.get('ok') is not True or captured.get('status') != 'active':
                return {**report, 'ok':False, 'reason':'native_memory_capture_rejected'}
            report['record_ids'].append(captured['record_id'])
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    parser.add_argument('--file', required=True)
    parser.add_argument('--lines', type=int, nargs='+', required=True)
    parser.add_argument('--expected-digest', required=True)
    for field in ('tenant', 'agent', 'workspace', 'user'):
        parser.add_argument('--'+field, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    runtime = Runtime.create(root=args.root)
    try:
        report = import_native_memory_lines(runtime, path=args.file, lines=args.lines,
            expected_digest=args.expected_digest, scope=ScopeRef(args.tenant,args.agent,args.workspace,args.user),
            apply=args.apply)
        print(json.dumps(report))
        return 0 if report['ok'] else 1
    finally:
        runtime.close()


if __name__ == '__main__':
    raise SystemExit(main())
