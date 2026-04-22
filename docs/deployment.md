# eimemory Standard Production Deployment

eimemory is a core production memory component. It should not be deployed under
an operator home directory except for local development.

## Canonical Paths

Use these paths on Linux production hosts:

| Purpose | Path |
| --- | --- |
| Main source repository | `/dev-project/eimemory` |
| Python virtual environment | `/opt/eimemory/venv` |
| Runtime data root | `/var/lib/eimemory` |
| Configuration root | `/etc/eimemory` |
| Logs and generated reports | `/var/log/eimemory` |
| OpenClaw bridge extension | `/var/lib/eimemory/openclaw/extensions/eimemory-bridge` |
| Governance console HTML | `/var/lib/eimemory/governance/evolution-console.html` |

## Runtime Environment

Install the package in editable mode from the canonical source repository:

```bash
python3 -m venv /opt/eimemory/venv
/opt/eimemory/venv/bin/python -m pip install -e /dev-project/eimemory
```

The runtime service environment should set:

```bash
EIMEMORY_ROOT=/var/lib/eimemory
EIMEMORY_CONFIG_DIR=/etc/eimemory
```

## Service Rules

- Services must not depend on `/home/<user>/dev-project`.
- Runtime data must not be stored inside the source repository.
- OpenClaw bridge files may be copied from the repository into the production
  extension path.
- Governance Console is read-only. It may expose static HTML through a tokenized
  URL, but it must not provide mutation endpoints.
- Backups should be written under `/var/lib/eimemory/backups` and verified with
  `eimemory backup verify`.

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

## Verification

After deployment, run:

```bash
/opt/eimemory/venv/bin/python -m pytest \
  /dev-project/eimemory/tests/test_governance_console.py \
  /dev-project/eimemory/tests/test_cli_governance.py \
  /dev-project/eimemory/tests/test_governance.py \
  /dev-project/eimemory/tests/test_runtime.py \
  /dev-project/eimemory/tests/test_storage.py \
  -q --basetemp /tmp/eimemory-prod-verify

EIMEMORY_ROOT=/var/lib/eimemory /opt/eimemory/venv/bin/eimemory quality stats
EIMEMORY_ROOT=/var/lib/eimemory /opt/eimemory/venv/bin/eimemory governance snapshot
```
