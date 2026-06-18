"""Push eimemory code to test host via paramiko + rsync-like copy.
Skips: .git, __pycache__, .pytest_cache, .harness/stash-*, state/, tmp/.
"""
from __future__ import annotations

import argparse
import os
import posixpath
import sys
from pathlib import Path

import paramiko


SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "state", "tmp", ".venv"}
SKIP_PREFIXES = (".harness/stash-",)


def should_skip(path: Path, root: Path) -> bool:
    rel = path.relative_to(root).as_posix()
    parts = set(Path(rel).parts)
    if parts & SKIP_DIRS:
        return True
    for prefix in SKIP_PREFIXES:
        if rel.startswith(prefix):
            return True
    return False


def upload_dir(sftp: paramiko.SFTPClient, local_root: Path, remote_root: str) -> tuple[int, int]:
    files_count = 0
    bytes_count = 0
    local_root = local_root.resolve()
    for local in sorted(local_root.rglob("*")):
        if local.is_dir():
            continue
        if should_skip(local, local_root):
            continue
        rel = local.relative_to(local_root).as_posix()
        remote = posixpath.join(remote_root, rel.replace("\\", "/"))
        remote_dir = posixpath.dirname(remote)
        try:
            sftp.stat(remote_dir)
        except IOError:
            # mkdir -p
            parts = remote_dir.split("/")
            for i in range(1, len(parts) + 1):
                sub = "/".join(parts[:i]) or "/"
                try:
                    sftp.stat(sub)
                except IOError:
                    sftp.mkdir(sub)
        sftp.put(str(local), remote)
        files_count += 1
        bytes_count += local.stat().st_size
        if files_count % 50 == 0:
            print(f"  uploaded {files_count} files, {bytes_count/1024/1024:.1f} MB", file=sys.stderr, flush=True)
    return files_count, bytes_count


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--user", default="root")
    ap.add_argument("--password", required=True)
    ap.add_argument("--port", type=int, default=22)
    ap.add_argument("--local", required=True, help="local eimemory root")
    ap.add_argument("--remote", required=True, help="remote target dir, e.g. /opt/eimemory")
    ap.add_argument("--timeout", type=float, default=60.0)
    args = ap.parse_args()

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(args.host, port=args.port, username=args.user, password=args.password, timeout=args.timeout)
    sftp = client.open_sftp()
    try:
        try:
            sftp.stat(args.remote)
        except IOError:
            sftp.mkdir(args.remote)
        fc, bc = upload_dir(sftp, Path(args.local), args.remote)
        print(f"OK uploaded {fc} files, {bc/1024/1024:.1f} MB to {args.user}@{args.host}:{args.remote}")
    finally:
        sftp.close()
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
