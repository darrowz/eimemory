"""Fix step 1a: remove only the release-closure-pending lock, retrigger via .signal, observe."""
import sys, time, paramiko

SCRIPT = r'''
set +e
echo "=== Step 1: inspect lock + signal BEFORE ==="
ls -la /var/lib/eimemory/state/.release-closure-pending.json.lock 2>&1
ls -la /var/lib/eimemory/state/release-closure-channel-receipt.signal 2>&1
echo
echo "=== Step 2: delete the stale release-closure lock ==="
rm -v /var/lib/eimemory/state/.release-closure-pending.json.lock 2>&1
ls -la /var/lib/eimemory/state/.release-closure-pending.json.lock 2>&1
echo
echo "=== Step 3: touch .signal to retrigger .path unit ==="
ls -la /var/lib/eimemory/state/release-closure-channel-receipt.signal
# Just touch the file to retrigger PathChanged
touch /var/lib/eimemory/state/release-closure-channel-receipt.signal 2>&1
echo "after touch:"
ls -la /var/lib/eimemory/state/release-closure-channel-receipt.signal
echo
echo "=== Step 4: wait 15s and read journal ==="
sleep 15
XDG_RUNTIME_DIR=/run/user/1001 journalctl --user -u eimemory-release-closure.service --since "30 seconds ago" --no-pager 2>&1 | tail -40
echo
echo "=== Step 5: status after ==="
XDG_RUNTIME_DIR=/run/user/1001 systemctl --user status eimemory-release-closure.service 2>&1 | head -20
'''

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect("honxin", username="darrow", password="Aa8465e6a4", timeout=10, allow_agent=False, look_for_keys=False)
sftp = client.open_sftp()
with sftp.file("/tmp/_eim_fix1a.sh", "w") as f: f.write(SCRIPT)
sftp.chmod("/tmp/_eim_fix1a.sh", 0o755)
sftp.close()
stdin, stdout, stderr = client.exec_command("/bin/bash /tmp/_eim_fix1a.sh", timeout=60)
out = stdout.read().decode("utf-8", errors="replace")
err = stderr.read().decode("utf-8", errors="replace")
rc = stdout.channel.recv_exit_status()
client.close()
sys.stdout.write(out)
if err.strip():
    sys.stdout.write("---STDERR---\n" + err)
sys.stdout.write(f"---RC={rc}---\n")
sys.exit(rc)
