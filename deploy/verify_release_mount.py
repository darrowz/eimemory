#!/usr/bin/env python3
"""Read-only verification of a release's effective mount in this process.

Run in the service mount namespace; running on the host does not inspect a
service's namespace. This does not attempt a write or certify source content.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys

sys.dont_write_bytecode = True


def inspect_mount(root: Path) -> dict:
    root = root.resolve(strict=True)
    if not root.is_dir() or not hasattr(os, 'ST_RDONLY'):
        raise ValueError('release_mount_verification_unavailable')
    readonly = bool(os.statvfs(root).f_flag & os.ST_RDONLY)
    return {'schema': 'release_mount_check.v1', 'ok': readonly,
            'release_path': str(root), 'effective_mount_readonly': readonly,
            'pid': os.getpid(), 'namespace': 'calling_process',
            'source_content_verified': False, 'write_probe_performed': False,
            'reason': '' if readonly else 'immutable_release_mount_not_readonly'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release-root', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        report = inspect_mount(args.release_root)
    except (OSError, ValueError) as exc:
        report = {'ok': False, 'reason': 'release_mount_verification_failed', 'error_type': type(exc).__name__}
    print(json.dumps(report, ensure_ascii=True))
    return 0 if report['ok'] else 2

if __name__ == '__main__':
    raise SystemExit(main())
