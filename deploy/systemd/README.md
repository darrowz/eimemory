# systemd Templates

This directory contains service templates for production eimemory deployments.

Copy templates into the selected systemd scope and replace placeholder values
before enabling them.

For the current OpenClaw user-service deployment, the active service lives under:

```bash
/home/darrow/.config/systemd/user/eimemory-console.service
```

Runtime code is deployed to:

```bash
/opt/eimemory/current
```

Source remains in:

```bash
/dev-project/eimemory
```

RPC and user-facing service logs should be written to user-owned paths under:

```bash
/home/darrow/.openclaw/logs
```

Using `/var/log/eimemory` for user-owned RPC output is not supported and can
trigger systemd `209/STDOUT` restart storms.

The service templates also point to these runtime configuration paths:

```bash
/opt/eimemory/current/.venv
/var/lib/eimemory
/etc/eimemory
```

`/dev-project/eimemory` is the canonical source repository only. Runtime
services should not import or execute code from it. Promote a release with:

```bash
/dev-project/eimemory/deploy/install_immutable_release.sh
```

Runtime configuration is loaded from `/etc/eimemory/settings.json` when
`EIMEMORY_CONFIG_DIR=/etc/eimemory` is set. `EIMEMORY_CONFIG_PATH` can still
point at a specific settings file, and `EIMEMORY_ROOT` overrides the configured
root.

## Nightly Intake Timer

The standard production schedule runs active knowledge intake and governance once
per day at 03:30 in the server's local timezone. The template also enables the
1.0.0 autonomous learning loop in apply mode with bounded goal count,
promotion budget, timeout, dashboard output, promotion gates, and rollback
metadata.

Install as a user service for the OpenClaw/eimemory operator:

```bash
mkdir -p ~/.config/systemd/user
cp /dev-project/eimemory/deploy/systemd/eimemory-nightly.service ~/.config/systemd/user/
cp /dev-project/eimemory/deploy/systemd/eimemory-nightly.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now eimemory-nightly.timer
systemctl --user list-timers eimemory-nightly.timer
```

Run one manual verification:

```bash
systemctl --user start eimemory-nightly.service
journalctl --user -u eimemory-nightly.service -n 100 --no-pager
```

## Autonomous Learning Timers

The 1.0.0 proactive learning layer runs three additional timers:

- `eimemory-learn-watch.timer`: every 5 minutes, capture lightweight local/outcome/world signals.
- `eimemory-learn-think.timer`: hourly, turn signals and long-term goals into persisted thoughts.
- `eimemory-learn-dashboard.timer`: daily at 03:45 local time, summarize learned/applied/blocked/next items.

Install as user services:

```bash
mkdir -p ~/.config/systemd/user
cp /dev-project/eimemory/deploy/systemd/eimemory-learn-*.service ~/.config/systemd/user/
cp /dev-project/eimemory/deploy/systemd/eimemory-learn-*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now eimemory-learn-watch.timer eimemory-learn-think.timer eimemory-learn-dashboard.timer
systemctl --user list-timers 'eimemory-learn-*.timer'
```
