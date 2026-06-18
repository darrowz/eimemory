"""Run a local shell script on the remote host via paramiko SFTP + exec."""
from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from pathlib import Path

import paramiko


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--user", default="root")
    ap.add_argument("--password", required=True)
    ap.add_argument("--port", type=int, default=22)
    ap.add_argument("--local-script", required=True)
    ap.add_argument("--timeout", type=float, default=600.0)
    args = ap.parse_args()

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(args.host, port=args.port, username=args.user, password=args.password, timeout=60)
    sftp = client.open_sftp()
    remote_path = f"/tmp/{uuid.uuid4().hex}.sh"
    try:
        sftp.put(args.local_script, remote_path)
        sftp.chmod(remote_path, 0o755)
    finally:
        sftp.close()

    stdin, stdout, stderr = client.exec_command(f"bash {remote_path}", timeout=args.timeout)
    out_b = stdout.read()
    err_b = stderr.read()
    rc = stdout.channel.recv_exit_status()
    print(out_b.decode("utf-8", errors="replace"))
    if err_b:
        print("---STDERR---", file=sys.stderr)
        print(err_b.decode("utf-8", errors="replace"), file=sys.stderr)

    # cleanup
    client.exec_command(f"rm -f {remote_path}")
    client.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
