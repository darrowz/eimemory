"""Final state check."""
import sys, paramiko

SCRIPT = r'''
set +e
echo "================ FINAL STATE ================"
echo
echo "=== 1. release-closure ==="
echo "  current symlink:"
ls -la /opt/eimemory/current
echo "  current release version:"
grep '^version' /opt/eimemory/current/pyproject.toml
echo "  1.9.133 release dir:"
ls -ld /opt/eimemory/releases/86d2ca4d397abcb4e916056f7051dcd3413d5d28
echo "  pending closure status:"
if [ -f /var/lib/eimemory/state/release-closure-pending.json ]; then
  grep -E '"status"|"current_commit"|"prior_commit"' /var/lib/eimemory/state/release-closure-pending.json | head -3
else
  echo "    (no pending file)"
fi
echo "  release-closure service last state:"
XDG_RUNTIME_DIR=/run/user/1001 systemctl --user show eimemory-release-closure.service --property=ActiveState,SubState,Result --value 2>&1
echo
echo "=== 2. timers ==="
XDG_RUNTIME_DIR=/run/user/1001 systemctl --user list-timers eimemory-l5-effect-review.timer eimemory-learn-dashboard.timer eimemory-learn-think.timer eimemory-learn-watch.timer openclaw-loop-compact.timer openclaw-loop-watch.timer eimemory-timer-monitor.timer --all 2>&1
echo
echo "=== 3. eimemory-rpc memory ==="
XDG_RUNTIME_DIR=/run/user/1001 systemctl --user show eimemory-rpc.service --property=MemoryHigh,MemoryMax,MemorySwapMax,OOMScoreAdjust,ActiveState 2>&1
echo "  RSS now: $(cat /sys/fs/cgroup/user.slice/user-1001.slice/user@1001.service/app.slice/eimemory-rpc.service/memory.current) bytes"
echo "  HWM:     $(cat /sys/fs/cgroup/user.slice/user-1001.slice/user@1001.service/app.slice/eimemory-rpc.service/memory.peak) bytes"
echo
echo "=== storage dir size ==="
du -sh /opt/eimemory/releases 2>/dev/null
df -h /opt/eimemory 2>/dev/null | tail -1
'''

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect("honxin", username="darrow", password="Aa8465e6a4", timeout=10, allow_agent=False, look_for_keys=False)
sftp = client.open_sftp()
with sftp.file("/tmp/_eim_final.sh", "w") as f: f.write(SCRIPT)
sftp.chmod("/tmp/_eim_final.sh", 0o755)
sftp.close()
stdin, stdout, stderr = client.exec_command("/bin/bash /tmp/_eim_final.sh", timeout=30)
out = stdout.read().decode("utf-8", errors="replace")
err = stderr.read().decode("utf-8", errors="replace")
rc = stdout.channel.recv_exit_status()
client.close()
sys.stdout.write(out)
if err.strip():
    sys.stdout.write("---STDERR---\n" + err)
sys.stdout.write(f"---RC={rc}---\n")
sys.exit(rc)
