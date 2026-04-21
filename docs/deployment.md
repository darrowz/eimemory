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

