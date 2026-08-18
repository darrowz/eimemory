"""Generate a shell script and push it via SSH, then read the output."""
import os, sys, paramiko

SCRIPT_BODY = r'''
set +e
echo "=== newest release version content ==="
for d in 86d2ca4d397abcb4e916056f7051dcd3413d5d28 20af6e54f49fc97248243107651b062fcd66d4be 0bab0f6ca3c17b6b862b1b9d6413612664575dd4; do
  echo "--- $d ---"
  ls /opt/eimemory/releases/$d/ | head -20
  echo "VERSION:"; cat /opt/eimemory/releases/$d/VERSION 2>/dev/null
  echo ".version:"; cat /opt/eimemory/releases/$d/.version 2>/dev/null
  echo "pyproject version:"; grep -E "^version" /opt/eimemory/releases/$d/pyproject.toml 2>/dev/null | head -3
  echo "git head:"; cd /opt/eimemory/releases/$d 2>/dev/null && git log -1 --oneline 2>/dev/null
  cd /opt/eimemory/releases/$d 2>/dev/null && git log -1 --format='%h %s' 2>/dev/null
done
echo
echo "=== /opt/eimemory/releases file count ==="
ls /opt/eimemory/releases/ | wc -l
echo
echo "=== current.bak.* ==="
ls -la /opt/eimemory/current.bak.* 2>&1 | head -20
echo
echo "=== /var/lib/eimemory/state ==="
ls -la /var/lib/eimemory/state/ 2>&1 | head -30
echo
echo "=== systemd --user release-closure service ==="
XDG_RUNTIME_DIR=/run/user/1001 systemctl --user status eimemory-release-closure.service 2>&1 | head -25
echo
echo "=== systemd --user release-closure timer ==="
XDG_RUNTIME_DIR=/run/user/1001 systemctl --user status eimemory-release-closure.timer 2>&1 | head -25
'''

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect("honxin", username="darrow", password="Aa8465e6a4", timeout=10, allow_agent=False, look_for_keys=False)

sftp = client.open_sftp()
with sftp.file("/tmp/_eim_probe1b.sh", "w") as f:
    f.write(SCRIPT_BODY)
sftp.chmod("/tmp/_eim_probe1b.sh", 0o755)
sftp.close()

stdin, stdout, stderr = client.exec_command("/bin/bash /tmp/_eim_probe1b.sh", timeout=60)
out = stdout.read().decode("utf-8", errors="replace")
err = stderr.read().decode("utf-8", errors="replace")
rc = stdout.channel.recv_exit_status()
client.close()

sys.stdout.write(out)
if err.strip():
    sys.stdout.write("---STDERR---\n" + err)
sys.stdout.write(f"---RC={rc}---\n")
sys.exit(rc)
