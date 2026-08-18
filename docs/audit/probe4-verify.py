"""Probe 4: verify openclaw-loop-watch + timer-monitor + check eimemory CLI subcommands."""
import sys, paramiko

SCRIPT = r'''
set +e
echo "=== openclaw-loop-watch.timer status ==="
XDG_RUNTIME_DIR=/run/user/1001 systemctl --user status openclaw-loop-watch.timer 2>&1 | head -25
echo
echo "=== openclaw-loop-watch.service last status ==="
XDG_RUNTIME_DIR=/run/user/1001 systemctl --user status openclaw-loop-watch.service 2>&1 | head -15
echo
echo "=== eimemory-timer-monitor.timer status ==="
XDG_RUNTIME_DIR=/run/user/1001 systemctl --user status eimemory-timer-monitor.timer 2>&1 | head -20
echo
echo "=== eimemory-timer-monitor.service last ==="
XDG_RUNTIME_DIR=/run/user/1001 systemctl --user status eimemory-timer-monitor.service 2>&1 | head -20
echo
echo "=== eimemory-timer-monitor service definition ==="
cat /home/darrow/.config/systemd/user/eimemory-timer-monitor.service 2>&1
echo
echo "=== eimemory-timer-monitor timer definition ==="
cat /home/darrow/.config/systemd/user/eimemory-timer-monitor.timer 2>&1
echo
echo "=== eimemory CLI: release-closure subcommands ==="
/opt/eimemory/current/.venv/bin/eimemory learn --help 2>&1 | head -40
echo
echo "=== current eimemory CLI binary path ==="
ls -la /opt/eimemory/current/.venv/bin/eimemory 2>&1
file /opt/eimemory/current/.venv/bin/eimemory 2>&1
echo
echo "=== pending release-closure-pending.json (head only) ==="
head -30 /var/lib/eimemory/state/release-closure-pending.json 2>&1
echo
echo "=== last lines of release-closure-pending.json (status block) ==="
tail -30 /var/lib/eimemory/state/release-closure-pending.json 2>&1
echo
echo "=== check storage-release-transaction.py exists + run dry ==="
ls -la /opt/eimemory/libexec/storage-release-transaction.py 2>&1
/usr/bin/python3.14 /opt/eimemory/libexec/storage-release-transaction.py --help 2>&1 | head -20
echo
echo "=== check the .signal content ==="
ls -la /var/lib/eimemory/state/release-closure-channel-receipt.signal 2>&1
xxd /var/lib/eimemory/state/release-closure-channel-receipt.signal 2>&1 | head -5
'''

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect("honxin", username="darrow", password="Aa8465e6a4", timeout=10, allow_agent=False, look_for_keys=False)
sftp = client.open_sftp()
with sftp.file("/tmp/_eim_probe4.sh", "w") as f: f.write(SCRIPT)
sftp.chmod("/tmp/_eim_probe4.sh", 0o755)
sftp.close()
stdin, stdout, stderr = client.exec_command("/bin/bash /tmp/_eim_probe4.sh", timeout=60)
out = stdout.read().decode("utf-8", errors="replace")
err = stderr.read().decode("utf-8", errors="replace")
rc = stdout.channel.recv_exit_status()
client.close()
sys.stdout.write(out)
if err.strip():
    sys.stdout.write("---STDERR---\n" + err)
sys.stdout.write(f"---RC={rc}---\n")
sys.exit(rc)
