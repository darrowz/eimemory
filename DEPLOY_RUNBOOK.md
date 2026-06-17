# eimemory — Test Server Deployment Runbook

**Date prepared:** 2026-06-18  
**Target:** Other internal test host (TBD — fill in before run)  
**Scope:** Full eimemory stack + karpathy loop nightly cron  
**Source:** `E:\eimemory` @ commit `b0e4996` (master, post R1+R8 audit)

---

## 0. Pre-flight (operator fills in)

```
DEPLOY_HOST=<hostname or IP>            # e.g. test-internal-01.lab
DEPLOY_USER=<ssh user>                 # e.g. deploy
DEPLOY_PORT=22                          # change if non-default
DEPLOY_PATH=/opt/eimemory               # install path on the host
DEPLOY_PYTHON=python3.12                # must be >= 3.10
DEPLOY_SANDBOX_HOSTNAME=$(hostname)     # what hostname the box will report
DEPLOY_SANDBOX_FILE=/var/lib/eimemory/state/sandbox_hostnames.txt
```

The host's hostname must be in `sandbox_hostnames.txt` (see §4) or every
paid-API call will be blocked by `spend_guard.assert_no_paid_spend`.

---

## 1. Copy code to the test host

```
rsync -avz --delete \
  -e "ssh -p ${DEPLOY_PORT}" \
  --exclude='.git' --exclude='__pycache__' --exclude='.harness/stash-*' \
  ./ ${DEPLOY_USER}@${DEPLOY_HOST}:${DEPLOY_PATH}/
```

---

## 2. Install Python deps + venv (on the host)

```
ssh ${DEPLOY_USER}@${DEPLOY_HOST} -p ${DEPLOY_PORT} <<'EOF'
set -euo pipefail
cd ${DEPLOY_PATH}
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e .
.venv/bin/pip install pytest==9.0.3
EOF
```

---

## 3. State directory + autonomy profile (sandbox default)

```
ssh ${DEPLOY_USER}@${DEPLOY_HOST} -p ${DEPLOY_PORT} <<EOF
set -euo pipefail
sudo mkdir -p /var/lib/eimemory/state /var/lib/eimemory/state/exp_log
sudo chown -R ${DEPLOY_USER}:${DEPLOY_USER} /var/lib/eimemory
sudo mkdir -p /etc/eimemory
sudo tee /etc/eimemory/eimemory.ini >/dev/null <<INI
[autonomy]
profile = learning
started_at = $(date -u +%Y-%m-%dT%H:%M:%SZ)
profile_history = /var/lib/eimemory/state/autonomy/profile_history.jsonl
INI
EOF
```

`learning` profile allows Phase 2 (the Karpathy loop) but not
`autonomous` (which is the 30-day-floor gate). Bump to `progressive`
or `autonomous` only after 14-day clean run + `approval.md` exists.

---

## 4. Sandbox hostnames allow-list (CRITICAL for paid-API guard)

```
ssh ${DEPLOY_USER}@${DEPLOY_HOST} -p ${DEPLOY_PORT} <<EOF
set -euo pipefail
sudo tee ${DEPLOY_SANDBOX_FILE} >/dev/null <<HOSTS
${DEPLOY_SANDBOX_HOSTNAME}
HOSTS
sudo chmod 0644 ${DEPLOY_SANDBOX_FILE}
EOF
```

`honxin` must NOT be in this list — the production host is blocked.

---

## 5. Install systemd unit (nightly karpathy loop at 02:30)

```
ssh ${DEPLOY_USER}@${DEPLOY_HOST} -p ${DEPLOY_PORT} <<'EOF'
set -euo pipefail
sudo cp ${DEPLOY_PATH}/deploy/systemd/eimemory-karpathy-loop.service \
        /etc/systemd/system/
sudo cp ${DEPLOY_PATH}/deploy/systemd/eimemory-karpathy-loop.timer \
        /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now eimemory-karpathy-loop.timer
sudo systemctl list-timers | grep eimemory
EOF
```

Verify the timer is scheduled and the service is enabled. Next run
will be the next 02:30 local time (or immediately if you used
`Persistent=true` + missed a window).

---

## 6. Hourly audit-chain verifier

The `audit_verifier.py` should run hourly. Add a separate systemd
timer (not in this commit) or piggyback on the host's cron:

```
ssh ${DEPLOY_USER}@${DEPLOY_HOST} -p ${DEPLOY_PORT} <<'EOF'
set -euo pipefail
(crontab -l 2>/dev/null; echo "0 * * * * ${DEPLOY_PATH}/.venv/bin/python -m eimemory.governance.safety.audit_verifier") | crontab -
EOF
```

On `ChainBroken` the verifier will call `emergency_stop()`, killing
the karpathy loop and any other eimemory process — so a chain break
will halt the loop until a human investigates.

---

## 7. Smoke test (do this BEFORE you leave it overnight)

```
ssh ${DEPLOY_USER}@${DEPLOY_HOST} -p ${DEPLOY_PORT} <<'EOF'
set -euo pipefail
cd ${DEPLOY_PATH}
# Full test suite (851 tests, ~2.5min on a 4-core box)
.venv/bin/pytest tests/ --ignore=tests/test_platform.py -q

# Dry-run karpathy loop with a tiny budget (1 exp, 60s cap)
EXP_BUDGET=1 TIME_BUDGET_SECONDS=60 \
  EIMEMORY_ROOT=/var/lib/eimemory \
  EIMEMORY_CONFIG_DIR=/etc/eimemory \
  bash scripts/karpathy_loop_cron.sh
EOF
```

If the smoke test hangs longer than ~3 minutes, kill the process and
check `/var/lib/eimemory/state/audit.jsonl` for the last appended
row — the timestamp tells you whether the gate is being hit.

---

## 8. Live monitor (operator runs the night of)

```
# Watch the audit log grow
ssh ${DEPLOY_USER}@${DEPLOY_HOST} -p ${DEPLOY_PORT} \
  'tail -f /var/lib/eimemory/state/audit.jsonl | jq -c .'

# Watch the cron run
ssh ${DEPLOY_USER}@${DEPLOY_HOST} -p ${DEPLOY_PORT} \
  'journalctl -u eimemory-karpathy-loop.service -f'

# Manually run the cron now (off-schedule)
ssh ${DEPLOY_USER}@${DEPLOY_HOST} -p ${DEPLOY_PORT} \
  'sudo systemctl start eimemory-karpathy-loop.service'
```

---

## 9. Emergency stop (if something looks wrong)

```
ssh ${DEPLOY_USER}@${DEPLOY_HOST} -p ${DEPLOY_PORT} <<'EOF'
.venv/bin/python -m eimemory.governance.safety.kill_switch
# Or the CLI:
python -c "from eimemory.governance.safety.kill_switch import emergency_stop; emergency_stop()"
EOF
```

Audit row will be written to `/var/lib/eimemory/state/audit.jsonl`
even if the kill itself failed (best-effort write).

---

## 10. Known issues to watch

- **test_platform::test_openclaw_js_bridge_*** — 2 tests fail because
  `node` is not installed on this dev box. The JS bridge is part of
  pre-existing eimemory, not Phase 0-4. On the test host, if `node`
  is available the tests should pass; otherwise leave them skipped.
- **Thread leak on experiment timeout** — `loop._run_with_time_box`
  starts a daemon thread that Python cannot kill. If an experiment
  hangs, the thread is leaked (but the loop moves on). 5-min cap
  bounds the cost. `karpathy_loop_cron.sh` enforces 50 exp / 4h, so
  at most 50 leaked threads per night, each bounded to 5 min.
- **`tests/test_audit_chain.py` was originally UTF-16** — fixed in
  commit `c286ba1` (encoding). The fix is included in this snapshot.

---

## 11. After the test run

Capture these and bring them back:

1. `cat /var/lib/eimemory/state/audit.jsonl | wc -l` — total events
2. `cat /var/lib/eimemory/state/exp_log/kept-$(date -u +%Y%m%d).log`
3. `journalctl -u eimemory-karpathy-loop.service --since '24h ago'`
4. `tail -50 /var/lib/eimemory/state/circuit_breaker.json` (if any)
5. `.venv/bin/pytest tests/ -q` final pass

If audit chain is intact and ≥1 kept experiment, Phase 0-4 is
green for the first night. Promote to progressive after 14
clean days; autonomous after 30 (per spec §10 6-criteria gate).
