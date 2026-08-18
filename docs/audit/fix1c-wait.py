"""Wait longer for reconcile."""
import sys, paramiko

SCRIPT = r'''
set +e
echo "=== Wait up to 4 more minutes ==="
for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 31 32 33 34 35 36 37 38 39 40 41 42 43 44 45 46 47 48; do
  sleep 5
  state=$(XDG_RUNTIME_DIR=/run/user/1001 systemctl --user is-active eimemory-release-closure.service 2>&1)
  echo "  t+${i}*5s: $state"
  [ "$state" = "inactive" ] && break
  [ "$state" = "failed" ] && break
done
echo
echo "=== Final journal ==="
XDG_RUNTIME_DIR=/run/user/1001 journalctl --user -u eimemory-release-closure.service --since "10 minutes ago" --no-pager 2>&1 | tail -60
echo
echo "=== final status ==="
XDG_RUNTIME_DIR=/run/user/1001 systemctl --user status eimemory-release-closure.service 2>&1 | head -20
echo
echo "=== pending json mtime + status field ==="
ls -la /var/lib/eimemory/state/release-closure-pending.json 2>&1
[ -f /var/lib/eimemory/state/release-closure-pending.json ] && grep -E '"status"|"current_commit"|"prior_commit"|"release_commit"' /var/lib/eimemory/state/release-closure-pending.json | head -10
echo
echo "=== state dir mtime-sorted (recent) ==="
ls -lt /var/lib/eimemory/state/ | head -15
'''

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect("honxin", username="darrow", password="Aa8465e6a4", timeout=10, allow_agent=False, look_for_keys=False)
sftp = client.open_sftp()
with sftp.file("/tmp/_eim_fix1c.sh", "w") as f: f.write(SCRIPT)
sftp.chmod("/tmp/_eim_fix1c.sh", 0o755)
sftp.close()
stdin, stdout, stderr = client.exec_command("/bin/bash /tmp/_eim_fix1c.sh", timeout=300)
out = stdout.read().decode("utf-8", errors="replace")
err = stderr.read().decode("utf-8", errors="replace")
rc = stdout.channel.recv_exit_status()
client.close()
sys.stdout.write(out)
if err.strip():
    sys.stdout.write("---STDERR---\n" + err)
sys.stdout.write(f"---RC={rc}---\n")
sys.exit(rc)
