#!/usr/bin/env python3
"""Render candidate runtime metadata privately, leaving serving identity alone."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import tempfile


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _check_live_targets(helper, root: Path, unit: str, names: tuple[str, ...], owner_uid: int):
    # Reuse the publication helper's path and managed-content checks, without
    # creating a missing directory. Actual publication repeats every check.
    if not root.is_absolute() or '..' in root.parts:
        raise helper.ManagedDropinError('systemd root must be an absolute normalized path')
    if not root.exists() and not root.is_symlink():
        existing = root.parent
        while not existing.exists() and existing != existing.parent:
            existing = existing.parent
        descriptor = helper._open_directory_without_symlinks(existing)
        os.close(descriptor)
        return
    root_fd = helper._open_directory_without_symlinks(root)
    try:
        allowed = {os.geteuid(), owner_uid}
        if os.fstat(root_fd).st_uid not in allowed:
            raise helper.ManagedDropinError("systemd root has an unexpected owner")
        try:
            parent_fd = os.open(unit + '.d', helper.DIRECTORY_FLAGS, dir_fd=root_fd)
        except FileNotFoundError:
            return
        try:
            if os.fstat(parent_fd).st_uid not in allowed:
                raise helper.ManagedDropinError("drop-in directory has an unexpected owner")
            for name in names:
                helper._validate_existing_target_at(parent_fd=parent_fd, target_name=name)
        finally:
            os.close(parent_fd)
    finally:
        os.close(root_fd)


def preflight(*, controller: Path, release: Path, commit: str, releases_root: str,
              systemd_root: Path, receipt_env_file: str, units: list[str], owner_uid: int):
    helper = _load(controller / 'deploy/install_managed_systemd_dropin.py', '_metadata_dropin')
    policy = _load(release / 'deploy/runtime_identity_policy.py', '_metadata_identity')
    name = policy.managed_dropin_name()
    retired = {'zzzz-eimemory-python-runtime.conf': '90-eimemory-python-runtime.conf',
               '90-eimemory-python-runtime.conf': 'zzzz-eimemory-python-runtime.conf'}
    if name not in retired:
        raise ValueError('runtime metadata drop-in name is unsupported')
    selected = list(dict.fromkeys(units))
    with tempfile.TemporaryDirectory(prefix='eimemory-metadata-preflight-') as directory:
        staging = Path(directory).resolve(strict=True)
        for unit in selected:
            if re.fullmatch(r'[A-Za-z0-9_.@-]+\.service', unit) is None:
                raise ValueError('runtime metadata service name is invalid')
            _check_live_targets(helper, systemd_root, unit, (name, retired[name]), owner_uid)
            target = staging / (unit + '.d') / name
            helper.install_managed_dropin(
                source=controller / 'deploy/systemd/eimemory-python-runtime.conf',
                target=target, root=staging, render_commit=commit,
                render_releases_root=releases_root,
                render_evidence_receipt_env_file=receipt_env_file,
            )
            rendered = target.read_bytes()
            if b'@EIMEMORY_' in rendered:
                raise ValueError('runtime metadata contains an unresolved template token')
    return {'ok': True, 'unit_count': len(selected), 'live_metadata_changed': False}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--controller-root', required=True, type=Path)
    parser.add_argument('--release-root', required=True, type=Path)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--releases-root', required=True)
    parser.add_argument('--systemd-root', required=True, type=Path)
    parser.add_argument('--receipt-env-file', required=True)
    parser.add_argument('--owner-uid', required=True, type=int)
    args = parser.parse_args()
    import sys
    report = preflight(controller=args.controller_root, release=args.release_root,
        commit=args.commit, releases_root=args.releases_root, systemd_root=args.systemd_root,
        receipt_env_file=args.receipt_env_file, owner_uid=args.owner_uid,
        units=[line.strip() for line in sys.stdin if line.strip()])
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
