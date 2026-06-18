"""Tiny SSH helper: run commands on a remote host via paramiko (password auth)."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import paramiko


def run(host: str, user: str, password: str, command: str, *, port: int = 22, timeout: float = 30.0) -> dict[str, Any]:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(host, port=port, username=user, password=password, timeout=timeout, banner_timeout=timeout, auth_timeout=timeout)
    except Exception as exc:
        return {"ok": False, "error": f"connect_failed: {exc!r}", "host": host}
    try:
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        out_b = stdout.read()
        err_b = stderr.read()
        rc = stdout.channel.recv_exit_status()
        return {
            "ok": rc == 0,
            "rc": rc,
            "stdout": out_b.decode("utf-8", errors="replace"),
            "stderr": err_b.decode("utf-8", errors="replace"),
            "host": host,
        }
    except Exception as exc:
        return {"ok": False, "error": f"exec_failed: {exc!r}", "host": host}
    finally:
        client.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--user", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--command", required=True)
    ap.add_argument("--port", type=int, default=22)
    ap.add_argument("--timeout", type=float, default=60.0)
    args = ap.parse_args()
    result = run(args.host, args.user, args.password, args.command, port=args.port, timeout=args.timeout)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
