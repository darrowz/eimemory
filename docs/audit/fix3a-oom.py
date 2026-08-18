"""Fix 3: bump rpc OOM memory limits conservatively."""
import sys, time, paramiko

SCRIPT = r'''
set +e
echo "=== BEFORE 95-memory-guard.conf ==="
cat /home/darrow/.config/systemd/user/eimemory-rpc.service.d/95-memory-guard.conf
echo
echo "=== backup ==="
ts=$(date +%Y%m%d-%H%M%S)
cp -a /home/darrow/.config/systemd/user/eimemory-rpc.service.d/95-memory-guard.conf /home/darrow/.config/systemd/user/eimemory-rpc.service.d/95-memory-guard.conf.bak.$ts
ls -la /home/darrow/.config/systemd/user/eimemory-rpc.service.d/95-memory-guard.conf*
echo
echo "=== write new limits (conservative) ==="
cat > /home/darrow/.config/systemd/user/eimemory-rpc.service.d/95-memory-guard.conf <<'EOF'
# Bound an RPC runaway so it cannot starve the co-located OpenClaw gateway.
# Adjusted 2026-08-17 to absorb the Aug 12 OOM spike (peak hit MemoryMax=2G).
#   - MemoryHigh  1.5G -> 2G   (throttle earlier but with 33% headroom over observed 1.4G peak)
#   - MemoryMax   2G   -> 3G   (1.5x the historical hard ceiling; aligned with host's 7.2G RAM)
#   - MemorySwapMax 512M -> 1G (host has 8G swap, prior peak saturated the 512M cap)
#   - OOMScoreAdjust=-300     (reduce kernel OOM-kill probability vs other co-tenant procs)
[Service]
MemoryAccounting=yes
MemoryHigh=2G
MemoryMax=3G
MemorySwapMax=1G
OOMScoreAdjust=-300
EOF
echo "--- new content ---"
cat /home/darrow/.config/systemd/user/eimemory-rpc.service.d/95-memory-guard.conf
echo
echo "=== daemon-reload + show effective ==="
XDG_RUNTIME_DIR=/run/user/1001 systemctl --user daemon-reload 2>&1
echo
echo "--- show eimemory-rpc.service (effective settings) ---"
XDG_RUNTIME_DIR=/run/user/1001 systemctl --user show eimemory-rpc.service --property=MemoryHigh,MemoryMax,MemorySwapMax,OOMScoreAdjust,MemoryAccounting 2>&1
echo
echo "=== apply: restart eimemory-rpc so new limits take effect ==="
echo "WARNING: this briefly drops the eibrain-rpc service (a few seconds)."
XDG_RUNTIME_DIR=/run/user/1001 systemctl --user restart eimemory-rpc.service 2>&1
echo
sleep 8
echo "=== status after restart ==="
XDG_RUNTIME_DIR=/run/user/1001 systemctl --user status eimemory-rpc.service 2>&1 | head -25
echo
echo "=== verify cgroup picks up new limits ==="
CG=/sys/fs/cgroup/user.slice/user-1001.slice/user@1001.service/app.slice/eimemory-rpc.service
echo "memory.current: $(cat $CG/memory.current)"
echo "memory.peak:    $(cat $CG/memory.peak)"
echo "memory.high:    $(cat $CG/memory.high)"
echo "memory.max:     $(cat $CG/memory.max)"
echo "memory.swap.current: $(cat $CG/memory.swap.current)"
echo "memory.swap.peak:    $(cat $CG/memory.swap.peak)"
echo "memory.swap.max:     $(cat $CG/memory.swap.max)"
echo "memory.events (oom_kill): $(grep oom_kill $CG/memory.events)"
echo
echo "=== confirm rpc is responsive ==="
curl -s -m 5 http://127.0.0.1:8091/health 2>&1 | head -10
echo
echo "=== journal tail (errors) ==="
XDG_RUNTIME_DIR=/run/user/1001 journalctl --user -u eimemory-rpc.service --since "1 minute ago" --no-pager 2>&1 | tail -15
'''

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect("honxin", username="darrow", password="Aa8465e6a4", timeout=10, allow_agent=False, look_for_keys=False)
sftp = client.open_sftp()
with sftp.file("/tmp/_eim_fix3.sh", "w") as f: f.write(SCRIPT)
sftp.chmod("/tmp/_eim_fix3.sh", 0o755)
sftp.close()
stdin, stdout, stderr = client.exec_command("/bin/bash /tmp/_eim_fix3.sh", timeout=60)
out = stdout.read().decode("utf-8", errors="replace")
err = stderr.read().decode("utf-8", errors="replace")
rc = stdout.channel.recv_exit_status()
client.close()
sys.stdout.write(out)
if err.strip():
    sys.stdout.write("---STDERR---\n" + err)
sys.stdout.write(f"---RC={rc}---\n")
sys.exit(rc)
