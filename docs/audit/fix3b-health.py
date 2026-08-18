"""Verify rpc health after restart."""
import sys, paramiko

SCRIPT = r'''
set +e
sleep 5
echo "=== health endpoint test ==="
curl -v -m 10 http://127.0.0.1:8091/health 2>&1 | head -20
echo
echo "=== other endpoints ==="
curl -s -m 5 http://127.0.0.1:8091/ 2>&1 | head -5
echo
echo "=== /metrics ==="
curl -s -m 5 http://127.0.0.1:8091/metrics 2>&1 | head -10
echo
echo "=== rpc listen ==="
ss -tlnp 2>&1 | grep 8091
echo
echo "=== cgroup final ==="
CG=/sys/fs/cgroup/user.slice/user-1001.slice/user@1001.service/app.slice/eimemory-rpc.service
echo "memory.current: $(cat $CG/memory.current)  (RSS)"
echo "memory.peak:    $(cat $CG/memory.peak)    (HWM)"
echo "memory.high:    $(cat $CG/memory.high)    (throttle)"
echo "memory.max:     $(cat $CG/memory.max)     (hard kill)"
echo "memory.swap.max: $(cat $CG/memory.swap.max)"
echo "PID in cgroup: $(cat $CG/cgroup.procs)"
'''

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect("honxin", username="darrow", password="Aa8465e6a4", timeout=10, allow_agent=False, look_for_keys=False)
sftp = client.open_sftp()
with sftp.file("/tmp/_eim_fix3b.sh", "w") as f: f.write(SCRIPT)
sftp.chmod("/tmp/_eim_fix3b.sh", 0o755)
sftp.close()
stdin, stdout, stderr = client.exec_command("/bin/bash /tmp/_eim_fix3b.sh", timeout=30)
out = stdout.read().decode("utf-8", errors="replace")
err = stderr.read().decode("utf-8", errors="replace")
rc = stdout.channel.recv_exit_status()
client.close()
sys.stdout.write(out)
if err.strip():
    sys.stdout.write("---STDERR---\n" + err)
sys.stdout.write(f"---RC={rc}---\n")
sys.exit(rc)
