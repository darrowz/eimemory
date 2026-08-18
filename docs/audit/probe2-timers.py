"""Probe 2: timer states + rpc OOM + cgroup."""
import sys, paramiko

SCRIPT = r'''
set +e
echo "=============================================="
echo "=== ALL EIMEMORY + OPENCLAW TIMERS ==="
echo "=============================================="
for t in eimemory-l5-effect-review eimemory-learn-dashboard eimemory-learn-think eimemory-learn-watch eimemory-release-closure openclaw-loop-compact openclaw-loop-watch; do
  echo
  echo "###### $t.timer ######"
  XDG_RUNTIME_DIR=/run/user/1001 systemctl --user status $t.timer 2>&1 | head -20
  echo "--- $t.service last status ---"
  XDG_RUNTIME_DIR=/run/user/1001 systemctl --user status $t.service 2>&1 | head -15
  echo "--- list-timers row ---"
  XDG_RUNTIME_DIR=/run/user/1001 systemctl --user list-timers --all $t.timer 2>&1 | head -5
done
echo
echo "=============================================="
echo "=== ALL USER TIMERS (sanity) ==="
echo "=============================================="
XDG_RUNTIME_DIR=/run/user/1001 systemctl --user list-timers --all 2>&1 | head -40
echo
echo "=============================================="
echo "=== eimemory-rpc.service status ==="
echo "=============================================="
XDG_RUNTIME_DIR=/run/user/1001 systemctl --user status eimemory-rpc.service 2>&1 | head -40
echo
echo "=== 95-memory-guard.conf drop-in ==="
cat /home/darrow/.config/systemd/user/eimemory-rpc.service.d/95-memory-guard.conf 2>&1
ls -la /home/darrow/.config/systemd/user/eimemory-rpc.service.d/ 2>&1
echo
echo "=== rpc last 100 journal lines (3 day window) ==="
XDG_RUNTIME_DIR=/run/user/1001 journalctl --user -u eimemory-rpc.service --since "7 days ago" --no-pager 2>&1 | tail -80
echo
echo "=== dmesg OOM for eimemory ==="
dmesg -T 2>/dev/null | grep -iE "oom|killed process|memory" | grep -iE "eimemory|openclaw" | tail -30
echo
echo "=== cgroup path ==="
ls -la /sys/fs/cgroup/user.slice/user-1001.slice/user@1001.service/ 2>/dev/null | head -30
cat /sys/fs/cgroup/user.slice/user-1001.slice/user@1001.service/app.slice/eimemory-rpc.service/memory.peak 2>/dev/null
cat /sys/fs/cgroup/user.slice/user-1001.slice/user@1001.service/app.slice/eimemory-rpc.service/memory.current 2>/dev/null
cat /sys/fs/cgroup/user.slice/user-1001.slice/user@1001.service/app.slice/eimemory-rpc.service/memory.max 2>/dev/null
cat /sys/fs/cgroup/user.slice/user-1001.slice/user@1001.service/app.slice/eimemory-rpc.service/memory.high 2>/dev/null
echo
echo "=== swap ==="
free -h
swapon --show 2>&1
echo
echo "=== rpc memory events from journal ==="
XDG_RUNTIME_DIR=/run/user/1001 journalctl --user -u eimemory-rpc.service --since "30 days ago" --no-pager 2>&1 | grep -iE "memory|oom|killed|signal" | head -30
'''

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect("honxin", username="darrow", password="Aa8465e6a4", timeout=10, allow_agent=False, look_for_keys=False)
sftp = client.open_sftp()
with sftp.file("/tmp/_eim_probe2.sh", "w") as f: f.write(SCRIPT)
sftp.chmod("/tmp/_eim_probe2.sh", 0o755)
sftp.close()
stdin, stdout, stderr = client.exec_command("/bin/bash /tmp/_eim_probe2.sh", timeout=120)
out = stdout.read().decode("utf-8", errors="replace")
err = stderr.read().decode("utf-8", errors="replace")
rc = stdout.channel.recv_exit_status()
client.close()
sys.stdout.write(out)
if err.strip():
    sys.stdout.write("---STDERR---\n" + err)
sys.stdout.write(f"---RC={rc}---\n")
sys.exit(rc)
