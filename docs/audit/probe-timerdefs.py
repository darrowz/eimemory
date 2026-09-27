import os
"""Probe timer definitions."""
import sys, paramiko

SCRIPT = r'''
for t in eimemory-l5-effect-review.timer openclaw-loop-watch.timer openclaw-loop-compact.timer eimemory-learn-dashboard.timer eimemory-learn-think.timer eimemory-learn-watch.timer eimemory-timer-monitor.timer; do
  echo "============================================================"
  echo "$t"
  echo "============================================================"
  echo "-- systemd cat --"
  XDG_RUNTIME_DIR=/run/user/1001 systemctl --user cat $t 2>&1
  echo "-- show (all properties) --"
  XDG_RUNTIME_DIR=/run/user/1001 systemctl --user show $t 2>&1 | head -25
  echo
done
'''

client = paramiko.SSHClient()
client.set_missing_host_key_policy(
    # MITM guard: fail closed on unknown host keys. Onboard a host via
    # ssh-keyscan into known_hosts; EIMEMORY_SSH_TRUST_NEW_HOST=1 is an
    # explicit, temporary operator opt-in only.
    paramiko.AutoAddPolicy()
    if os.environ.get("EIMEMORY_SSH_TRUST_NEW_HOST") == "1"
    else paramiko.RejectPolicy()
)
client.connect("honxin", username="darrow", password="[REDACTED]", timeout=10, allow_agent=False, look_for_keys=False)
sftp = client.open_sftp()
with sftp.file("/tmp/_eim_timerdefs.sh", "w") as f: f.write(SCRIPT)
sftp.chmod("/tmp/_eim_timerdefs.sh", 0o755)
sftp.close()
stdin, stdout, stderr = client.exec_command("/bin/bash /tmp/_eim_timerdefs.sh", timeout=30)
out = stdout.read().decode("utf-8", errors="replace")
err = stderr.read().decode("utf-8", errors="replace")
rc = stdout.channel.recv_exit_status()
client.close()
sys.stdout.write(out)
if err.strip():
    sys.stdout.write("---STDERR---\n" + err)
sys.stdout.write(f"---RC={rc}---\n")
sys.exit(rc)
