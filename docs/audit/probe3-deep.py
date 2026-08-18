"""Probe 3: lock files, .path trigger, rpc cgroup history, OOM more detail."""
import sys, paramiko

SCRIPT = r'''
set +e
echo "==============================================="
echo "=== LOCK FILES content + holder check ==="
echo "==============================================="
for lock in /var/lib/eimemory/state/.release-closure-pending.json.lock \
            /var/lib/eimemory/state/.storage-release-transaction.json.lock \
            /var/lib/eimemory/state/.storage-maintenance.lock; do
  echo "--- $lock ---"
  ls -la $lock 2>&1
  echo "content (hex first 32):"
  xxd $lock 2>/dev/null | head -3
  echo "as text:"
  cat $lock 2>&1
  echo
  pid_in_lock=$(cat $lock 2>/dev/null | tr -d '\0')
  if [ -n "$pid_in_lock" ]; then
    echo "  -> PID in lock: $pid_in_lock"
    if kill -0 $pid_in_lock 2>/dev/null; then
      echo "     STILL ALIVE!"
      ps -o pid,etime,cmd -p $pid_in_lock 2>&1
    else
      echo "     process NOT running (stale lock)"
    fi
  fi
  echo
done
echo "==============================================="
echo "=== /var/lib/eimemory state sizes (top 10) ==="
echo "==============================================="
du -sh /var/lib/eimemory/state/* 2>/dev/null | sort -hr | head -10
echo
echo "==============================================="
echo "=== release-closure.path unit ==="
echo "==============================================="
XDG_RUNTIME_DIR=/run/user/1001 systemctl --user cat eimemory-release-closure.path 2>&1 | head -40
echo
echo "=== .path drop-in ==="
ls /home/darrow/.config/systemd/user/eimemory-release-closure.path.d/ 2>&1
ls /home/darrow/.config/systemd/user/eimemory-release-closure.service.d/ 2>&1
cat /home/darrow/.config/systemd/user/eimemory-release-closure.path 2>&1 | head -40
echo
echo "==============================================="
echo "=== storage-release-transaction.json (if any) ==="
echo "==============================================="
ls -la /var/lib/eimemory/state/storage-release-transaction.json 2>&1
cat /var/lib/eimemory/state/storage-release-transaction.json 2>&1
echo
echo "=== release-closure-pending.json (if any) ==="
ls -la /var/lib/eimemory/state/release-closure-pending.json 2>&1
cat /var/lib/eimemory/state/release-closure-pending.json 2>&1
echo
echo "=== release-closure-channel-receipt.signal ==="
ls -la /var/lib/eimemory/state/release-closure-channel-receipt.signal 2>&1
cat /var/lib/eimemory/state/release-closure-channel-receipt.signal 2>&1
echo
echo "==============================================="
echo "=== RPC cgroup memory detail (current service) ==="
echo "==============================================="
CG=/sys/fs/cgroup/user.slice/user-1001.slice/user@1001.service/app.slice/eimemory-rpc.service
for f in memory.current memory.peak memory.high memory.max memory.swap.current memory.swap.peak memory.swap.max memory.events memory.events.local memory.stat; do
  echo "--- $f ---"
  cat $CG/$f 2>&1
  echo
done
echo
echo "==============================================="
echo "=== All OOM-related journal entries (60d) ==="
echo "==============================================="
XDG_RUNTIME_DIR=/run/user/1001 journalctl --user -u eimemory-rpc.service --since "60 days ago" --no-pager 2>&1 | grep -iE "oom|killed|memory high|memory max|swap|memory.*peak" | head -40
echo
echo "==============================================="
echo "=== full eimemory-release-closure.service (last 50 lines) ==="
echo "==============================================="
XDG_RUNTIME_DIR=/run/user/1001 journalctl --user -u eimemory-release-closure.service --no-pager 2>&1 | tail -50
echo
echo "==============================================="
echo "=== count of eimemory/openclaw unit files ==="
echo "==============================================="
ls /home/darrow/.config/systemd/user/ | grep -E "eimemory|openclaw" | sort
echo
echo "==============================================="
echo "=== dmesg tail with sudo (if possible) ==="
echo "==============================================="
sudo -n dmesg -T 2>&1 | tail -5
'''

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect("honxin", username="darrow", password="Aa8465e6a4", timeout=10, allow_agent=False, look_for_keys=False)
sftp = client.open_sftp()
with sftp.file("/tmp/_eim_probe3.sh", "w") as f: f.write(SCRIPT)
sftp.chmod("/tmp/_eim_probe3.sh", 0o755)
sftp.close()
stdin, stdout, stderr = client.exec_command("/bin/bash /tmp/_eim_probe3.sh", timeout=120)
out = stdout.read().decode("utf-8", errors="replace")
err = stderr.read().decode("utf-8", errors="replace")
rc = stdout.channel.recv_exit_status()
client.close()
sys.stdout.write(out)
if err.strip():
    sys.stdout.write("---STDERR---\n" + err)
sys.stdout.write(f"---RC={rc}---\n")
sys.exit(rc)
