# eimemory Standard Production Deployment

eimemory is a core production memory component. It should not be deployed under
an operator home directory except for local development.

## Canonical Paths

Use these paths on Linux production hosts:

| Purpose | Path |
| --- | --- |
| Main source repository | `/dev-project/eimemory` |
| Immutable releases | `/opt/eimemory/releases/<commit>` |
| Active release symlink | `/opt/eimemory/current` |
| Release virtual environment | `/opt/eimemory/current/.venv` |
| Runtime data root | `/var/lib/eimemory` |
| Configuration root | `/etc/eimemory` |
| User service logs and generated reports | `/home/darrow/.openclaw/logs` |
| OpenClaw bridge extension | `/var/lib/eimemory/openclaw/extensions/eimemory-bridge` |
| Governance console HTML | `/var/lib/eimemory/governance/evolution-console.html` |

## Runtime Environment

The source repository is not a production runtime. Promote a commit into an
immutable release directory, then run services only through
`/opt/eimemory/current`:

```bash
/dev-project/eimemory/deploy/install_immutable_release.sh
```

The release script copies the current repository commit into
`/opt/eimemory/releases/<commit>`, creates a release-local virtual environment,
installs eimemory non-editably, and atomically updates `/opt/eimemory/current`.
The RPC service is always owned by the user systemd layer
(`systemctl --user`). Do not install or enable a system-level
`eimemory-rpc.service`; it is not a supported deployment owner.

The runtime service environment should set:

```bash
EIMEMORY_ROOT=/var/lib/eimemory
EIMEMORY_CONFIG_DIR=/etc/eimemory
```

## Service Rules

- Services must not depend on `/home/<user>/dev-project`.
- Services must not import production code from `/dev-project/eimemory`.
- systemd units must execute binaries under `/opt/eimemory/current/.venv`.
- Rollback is performed by repointing `/opt/eimemory/current` to an older
  directory under `/opt/eimemory/releases`.
- Runtime data must not be stored inside the source repository.
- OpenClaw bridge files may be copied from the repository into the production
  extension path.
- Governance Console is read-only. It may expose static HTML through a tokenized
  URL, but it must not provide mutation endpoints.
- Rotate the Governance Console token after sharing it or after operator changes.
  Use `deploy/rotate_console_token.py`, then reload and restart the user service.
- Prefer firewall or reverse-proxy allowlists for port `8765`; the tokenized URL
  is a lightweight guard, not a replacement for network access control.
- Backups should be written under `/var/lib/eimemory/backups` and verified with
  `eimemory backup verify`.


## eibrain RPC Service

Install the dedicated user service for the eibrain-facing RPC boundary:

```bash
mkdir -p ~/.config/systemd/user
cp /dev-project/eimemory/deploy/systemd/eimemory-rpc.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now eimemory-rpc.service
```

Verify the listener before switching `eibrain` to the RPC provider:

```bash
systemctl --user status eimemory-rpc.service --no-pager
ss -ltn | grep 100.105.189.120:8091
/opt/eimemory/current/deploy/check_user_systemd_owner.sh
```

The RPC service should bind to honxin's Tailscale address on port `8091`, separate from the Governance Console on `8765`, so honjia can reach it over MagicDNS.
If `/etc/systemd/system/eimemory-rpc.service` exists from an older deployment,
disable it before starting the user unit:

```bash
sudo systemctl disable --now eimemory-rpc.service
```

## Nightly Knowledge Intake

Production deployments should install the standard systemd user timer:

| Unit | Purpose |
| --- | --- |
| `eimemory-nightly.service` | Runs `eimemory nightly` once. |
| `eimemory-nightly.timer` | Triggers the service daily at `03:30` local server time. |

Install it for the OpenClaw/eimemory operator account:

```bash
mkdir -p ~/.config/systemd/user
cp /dev-project/eimemory/deploy/systemd/eimemory-nightly.service ~/.config/systemd/user/
cp /dev-project/eimemory/deploy/systemd/eimemory-nightly.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now eimemory-nightly.timer
```

Verify the timer and run one manual smoke:

```bash
systemctl --user list-timers eimemory-nightly.timer
systemctl --user start eimemory-nightly.service
journalctl --user -u eimemory-nightly.service -n 100 --no-pager
```

The timer uses server local time. If the host timezone changes, systemd will
apply the new local 03:30 schedule automatically.

## Autonomous Learning Companions

The daily `eimemory-nightly.timer` is the only production governance owner. It
may call autonomous evolution and autonomous learning internally, with gates and
rollback evidence. The companion timers are lightweight helpers:

| Unit | Purpose |
| --- | --- |
| `eimemory-learn-watch.timer` | Capture local/outcome/world signals every 15 minutes. |
| `eimemory-learn-think.timer` | Turn signals, corrections, and stale goals into persisted thoughts hourly. |
| `eimemory-learn-dashboard.timer` | Write the operator dashboard after nightly at 03:45. |

Install them only when the host should run proactive learning:

```bash
mkdir -p ~/.config/systemd/user
cp /dev-project/eimemory/deploy/systemd/eimemory-learn-*.service ~/.config/systemd/user/
cp /dev-project/eimemory/deploy/systemd/eimemory-learn-*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now eimemory-learn-watch.timer eimemory-learn-think.timer eimemory-learn-dashboard.timer
```

Do not install a separate Karpathy-loop timer in production. Experimental
autonomy helpers under `eimemory.autonomous` are reusable mechanisms, not a
second scheduler writing competing learning state.

## Verification

After deployment, run:

```bash
/opt/eimemory/current/.venv/bin/python -m pytest \
  /opt/eimemory/current/tests/test_governance_console.py \
  /opt/eimemory/current/tests/test_cli_governance.py \
  /opt/eimemory/current/tests/test_governance.py \
  /opt/eimemory/current/tests/test_runtime.py \
  /opt/eimemory/current/tests/test_storage.py \
  -q --basetemp /tmp/eimemory-prod-verify

EIMEMORY_ROOT=/var/lib/eimemory /opt/eimemory/current/.venv/bin/eimemory quality stats
EIMEMORY_ROOT=/var/lib/eimemory /opt/eimemory/current/.venv/bin/eimemory governance snapshot
/opt/eimemory/current/deploy/check_user_systemd_owner.sh
```

## Governance Console Token Rotation

The console serves static read-only HTML at `http://<host>:8765/<token>`.
Rotate the token with:

```bash
/opt/eimemory/current/.venv/bin/python /opt/eimemory/current/deploy/rotate_console_token.py \
  --unit ~/.config/systemd/user/eimemory-console.service
systemctl --user daemon-reload
systemctl --user restart eimemory-console.service
```

The script prints the new URL shape. Do not commit generated production tokens
into the repository.
