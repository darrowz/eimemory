import os
"""Find the source of premature_bump and what triggers it."""
import sys, paramiko

SCRIPT = r'''
set +e
echo "=== Search eimemory source for premature_bump ==="
grep -rn "premature_bump" /opt/eimemory/current/eimemory/ 2>/dev/null | head -20
echo
echo "=== Search for change_policy in source ==="
grep -rn "change_policy\|finish_closure_first\|closure_required" /opt/eimemory/current/eimemory/ 2>/dev/null | head -20
echo
echo "=== Search for release_closure_reconcile_busy ==="
grep -rn "release_closure_reconcile_busy" /opt/eimemory/current/eimemory/ 2>/dev/null | head -10
echo
echo "=== Search for 'bump' in release-closure code ==="
grep -rln "bump" /opt/eimemory/current/eimemory/ 2>/dev/null | head -10
echo
echo "=== Find file containing premature_bump ==="
grep -rln "premature_bump" /opt/eimemory/releases/ 2>/dev/null | head -10
echo
echo "=== Search for related: 'pending_bump' or 'in_flight' ==="
grep -rn "pending_bump\|in_flight\|in_progress_bump" /opt/eimemory/current/eimemory/ 2>/dev/null | head -20
echo
echo "=== Find release-closure subcommand source ==="
find /opt/eimemory/current/eimemory -name "*.py" -path "*release_closure*" 2>/dev/null | head -10
find /opt/eimemory/current/eimemory -name "*.py" -path "*closure*" 2>/dev/null | head -10
echo
echo "=== eimemory learn release-closure --help ==="
/opt/eimemory/current/.venv/bin/eimemory learn release-closure --help 2>&1 | head -40
echo
echo "=== eimemory learn release-closure-reconcile --help ==="
/opt/eimemory/current/.venv/bin/eimemory learn release-closure-reconcile --help 2>&1 | head -40
echo
echo "=== eimemory ops --help (subcommands) ==="
/opt/eimemory/current/.venv/bin/eimemory ops --help 2>&1 | head -40
echo
echo "=== storage-release-transaction show (read current marker state) ==="
/usr/bin/python3.14 /opt/eimemory/libexec/storage-release-transaction.py show 2>&1 | head -30
echo
echo "=== storage-release-transaction expected-current ==="
/usr/bin/python3.14 /opt/eimemory/libexec/storage-release-transaction.py expected-current 2>&1 | head -10
echo
echo "=== ls /opt/eimemory/libexec/ ==="
ls -la /opt/eimemory/libexec/ 2>&1 | head -30
'''

client = paramiko.SSHClient()
client.load_system_host_keys()
client.set_missing_host_key_policy(
    # MITM guard: fail closed on unknown host keys. Onboard a host via
    # ssh-keyscan into known_hosts; EIMEMORY_SSH_TRUST_NEW_HOST=1 is an
    # explicit, temporary operator opt-in only.
    paramiko.AutoAddPolicy()
    if os.environ.get("EIMEMORY_SSH_TRUST_NEW_HOST") == "1"
    else paramiko.RejectPolicy()
)
client.connect("honxin", username="darrow", password="[REDACTED]", timeout=10, allow_agent=False, look_for_keys=False)
sftp = client.open_sftp()
with sftp.file("/tmp/_eim_fix1d.sh", "w") as f: f.write(SCRIPT)
sftp.chmod("/tmp/_eim_fix1d.sh", 0o755)
sftp.close()
stdin, stdout, stderr = client.exec_command("/bin/bash /tmp/_eim_fix1d.sh", timeout=120)
out = stdout.read().decode("utf-8", errors="replace")
err = stderr.read().decode("utf-8", errors="replace")
rc = stdout.channel.recv_exit_status()
client.close()
sys.stdout.write(out)
if err.strip():
    sys.stdout.write("---STDERR---\n" + err)
sys.stdout.write(f"---RC={rc}---\n")
sys.exit(rc)
