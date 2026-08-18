"""
SSH helper for honxin eimemory ops.
Usage:
  python _ssh_honxin.py "<remote bash command>"  [timeout]
Output: stdout + stderr merged, with '---RC=N---' trailer on its own line.
Exit code: 0 if RC=0, else RC.
"""
import sys, paramiko

if len(sys.argv) < 2:
    sys.stdout.write("usage: _ssh_honxin.py <remote-bash-cmd-or-stdin-marker> [timeout]\n  - reads from stdin if argv[1] is '-'\n")
    sys.exit(2)

if sys.argv[1] == "-":
    remote_cmd = sys.stdin.read()
    timeout = int(sys.argv[2]) if len(sys.argv) > 2 else 60
else:
    remote_cmd = sys.argv[1]
    timeout = int(sys.argv[2]) if len(sys.argv) > 2 else 60

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(
    "honxin",
    username="darrow",
    password="Aa8465e6a4",
    timeout=10,
    allow_agent=False,
    look_for_keys=False,
)
# Source profile so we get full PATH and env, but stay non-interactive.
# `bash -lc` strips PATH on some Ubuntu setups; use a manual source.
final = f"/bin/bash -c 'set -a; . /etc/environment 2>/dev/null; . ~/.bashrc 2>/dev/null; . ~/.profile 2>/dev/null; set +a; export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin; {remote_cmd}'"
stdin, stdout, stderr = client.exec_command(final, timeout=timeout)
out = stdout.read().decode("utf-8", errors="replace")
err = stderr.read().decode("utf-8", errors="replace")
rc = stdout.channel.recv_exit_status()
client.close()

# Single stream, no stderr noise
sys.stdout.write(out)
if not out.endswith("\n"):
    sys.stdout.write("\n")
if err.strip():
    sys.stdout.write("---STDERR---\n")
    sys.stdout.write(err)
    if not err.endswith("\n"):
        sys.stdout.write("\n")
sys.stdout.write(f"---RC={rc}---\n")
sys.exit(rc)
