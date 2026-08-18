"""Fix 1b: wait for release-closure to complete, get result."""
import sys, paramiko

SCRIPT = r'''
set +e
echo "=== Wait 60s for release-closure to settle ==="
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
  sleep 5
  state=$(XDG_RUNTIME_DIR=/run/user/1001 systemctl --user is-active eimemory-release-closure.service 2>&1)
  echo "  t+${i}*5s: $state"
  [ "$state" = "inactive" ] && break
  [ "$state" = "failed" ] && break
done
echo
echo "=== Full journal of this run ==="
XDG_RUNTIME_DIR=/run/user/1001 journalctl --user -u eimemory-release-closure.service --since "2 minutes ago" --no-pager 2>&1
echo
echo "=== status ==="
XDG_RUNTIME_DIR=/run/user/1001 systemctl --user status eimemory-release-closure.service 2>&1 | head -20
echo
echo "=== pending json after ==="
ls -la /var/lib/eimemory/state/release-closure-pending.json 2>&1
[ -f /var/lib/eimemory/state/release-closure-pending.json ] && tail -20 /var/lib/eimemory/state/release-closure-pending.json
echo
echo "=== channel receipt signal after ==="
ls -la /var/lib/eimemory/state/release-closure-channel-receipt.signal 2>&1
[ -f /var/lib/eimemory/state/release-closure-channel-receipt.signal ] && cat /var/lib/eimemory/state/release-closure-channel-receipt.signal
echo
echo "=== /var/lib/eimemory/state audit-pending list (any new files?) ==="
ls -la /var/lib/eimemory/state/ | grep -E "release-closure|channel-receipt" 2>&1
'''

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect("honxin", username="darrow", password="Aa8465e6a4", timeout=10, allow_agent=False, look_for_keys=False)
sftp = client.open_sftp()
with sftp.file("/tmp/_eim_fix1b.sh", "w") as f: f.write(SCRIPT)
sftp.chmod("/tmp/_eim_fix1b.sh", 0o755)
sftp.close()
stdin, stdout, stderr = client.exec_command("/bin/bash /tmp/_eim_fix1b.sh", timeout=120)
out = stdout.read().decode("utf-8", errors="replace")
err = stderr.read().decode("utf-8", errors="replace")
rc = stdout.channel.recv_exit_status()
client.close()
sys.stdout.write(out)
if err.strip():
    sys.stdout.write("---STDERR---\n" + err)
sys.stdout.write(f"---RC={rc}---\n")
sys.exit(rc)
