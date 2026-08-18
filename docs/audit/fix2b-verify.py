"""Verify timers actually fired + clean stale lock + check rpc memory actuals."""
import sys, time, paramiko

SCRIPT = r'''
set +e
echo "=== Wait 30s, then re-check timer firings ==="
sleep 30
for t in eimemory-l5-effect-review eimemory-learn-dashboard eimemory-learn-think eimemory-learn-watch openclaw-loop-compact openclaw-loop-watch; do
  state=$(XDG_RUNTIME_DIR=/run/user/1001 systemctl --user show $t.timer --property=ActiveState,LastTriggerUSec,NextElapseUSec 2>&1)
  echo "--- $t.timer ---"
  echo "$state"
done
echo
echo "=== clean stale .release-closure-pending.json.lock ==="
ls -la /var/lib/eimemory/state/.release-closure-pending.json.lock 2>&1
# Process exited, lock is stale; check
fuser /var/lib/eimemory/state/.release-closure-pending.json.lock 2>&1 || echo "  (no process holds it)"
rm -v /var/lib/eimemory/state/.release-closure-pending.json.lock 2>&1
echo
echo "=== rpc memory snapshot ==="
CG=/sys/fs/cgroup/user.slice/user-1001.slice/user@1001.service/app.slice/eimemory-rpc.service
echo "memory.current: $(cat $CG/memory.current 2>&1)  (bytes)"
echo "memory.peak:    $(cat $CG/memory.peak 2>&1)  (bytes)"
echo "memory.high:    $(cat $CG/memory.high 2>&1)  (bytes)"
echo "memory.max:     $(cat $CG/memory.max 2>&1)  (bytes)"
echo "memory.swap.current: $(cat $CG/memory.swap.current 2>&1)"
echo "memory.swap.peak:    $(cat $CG/memory.swap.peak 2>&1)"
echo "memory.swap.max:     $(cat $CG/memory.swap.max 2>&1)"
echo "memory.events (oom_kill): $(grep oom_kill $CG/memory.events 2>&1)"
echo
echo "=== rpc process RSS from ps ==="
ps -o pid,rss,vsz,cmd -p 1383768 2>&1 || pgrep -f "eimemory.cli.main serve-eibrain-rpc" | xargs -I{} ps -o pid,rss,vsz,etime,cmd -p {} 2>&1
echo
echo "=== whole host memory ==="
free -h
echo
echo "=== /var/lib/eimemory state summary ==="
du -sh /var/lib/eimemory 2>&1
'''

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect("honxin", username="darrow", password="Aa8465e6a4", timeout=10, allow_agent=False, look_for_keys=False)
sftp = client.open_sftp()
with sftp.file("/tmp/_eim_fix2b.sh", "w") as f: f.write(SCRIPT)
sftp.chmod("/tmp/_eim_fix2b.sh", 0o755)
sftp.close()
stdin, stdout, stderr = client.exec_command("/bin/bash /tmp/_eim_fix2b.sh", timeout=60)
out = stdout.read().decode("utf-8", errors="replace")
err = stderr.read().decode("utf-8", errors="replace")
rc = stdout.channel.recv_exit_status()
client.close()
sys.stdout.write(out)
if err.strip():
    sys.stdout.write("---STDERR---\n" + err)
sys.stdout.write(f"---RC={rc}---\n")
sys.exit(rc)
