"""Fix 2: restart 6 stopped timers."""
import sys, time, paramiko

SCRIPT = r'''
set +e
TIMERS="eimemory-l5-effect-review.timer eimemory-learn-dashboard.timer eimemory-learn-think.timer eimemory-learn-watch.timer openclaw-loop-compact.timer openclaw-loop-watch.timer"

echo "=== BEFORE: timer states ==="
for t in $TIMERS; do
  state=$(XDG_RUNTIME_DIR=/run/user/1001 systemctl --user is-active $t 2>&1)
  last=$(XDG_RUNTIME_DIR=/run/user/1001 systemctl --user show $t --property=LastTriggerUSec 2>&1)
  echo "  $t -> active=$state | $last"
done
echo
echo "=== Enable + start all 6 timers ==="
for t in $TIMERS; do
  echo "--- $t ---"
  XDG_RUNTIME_DIR=/run/user/1001 systemctl --user enable --now $t 2>&1
done
echo
echo "=== Enable eimemory-timer-monitor.timer (self-healing, was disabled) ==="
XDG_RUNTIME_DIR=/run/user/1001 systemctl --user enable --now eimemory-timer-monitor.timer 2>&1
echo
echo "=== AFTER 5s: timer states ==="
sleep 5
for t in $TIMERS; do
  state=$(XDG_RUNTIME_DIR=/run/user/1001 systemctl --user is-active $t 2>&1)
  trigger=$(XDG_RUNTIME_DIR=/run/user/1001 systemctl --user show $t --property=LastTriggerUSec,NextElapseUSec 2>&1)
  echo "  $t -> active=$state"
  echo "    $trigger"
done
echo
echo "=== list-timers snapshot ==="
XDG_RUNTIME_DIR=/run/user/1001 systemctl --user list-timers --all 2>&1 | head -30
echo
echo "=== ensure no release-closure-pending.json.lock remains ==="
ls -la /var/lib/eimemory/state/.release-closure-pending.json.lock 2>&1
'''

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect("honxin", username="darrow", password="Aa8465e6a4", timeout=10, allow_agent=False, look_for_keys=False)
sftp = client.open_sftp()
with sftp.file("/tmp/_eim_fix2.sh", "w") as f: f.write(SCRIPT)
sftp.chmod("/tmp/_eim_fix2.sh", 0o755)
sftp.close()
stdin, stdout, stderr = client.exec_command("/bin/bash /tmp/_eim_fix2.sh", timeout=60)
out = stdout.read().decode("utf-8", errors="replace")
err = stderr.read().decode("utf-8", errors="replace")
rc = stdout.channel.recv_exit_status()
client.close()
sys.stdout.write(out)
if err.strip():
    sys.stdout.write("---STDERR---\n" + err)
sys.stdout.write(f"---RC={rc}---\n")
sys.exit(rc)
