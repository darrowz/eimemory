# systemd Templates

This directory contains service templates for production eimemory deployments.

Copy templates into the selected systemd scope and replace placeholder values
before enabling them.

For the current OpenClaw user-service deployment, the active service lives under:

```bash
/home/darrow/.config/systemd/user/eimemory-console.service
```

The service should still point to canonical production paths:

```bash
/opt/eimemory/current
/opt/eimemory/current/.venv
/var/lib/eimemory
/etc/eimemory
```

User services should write logs to a user-writable location such as
`%h/.openclaw/logs`; using `/var/log/eimemory` as a user service output path can
fail with systemd `209/STDOUT` on some hosts.

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
1.0.0 autonomous learning loop in apply mode with bounded goal count, timeout,
dashboard output, promotion gates, and rollback metadata.

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
- `eimemory-learn-dashboard.timer`: weekly, summarize learned/applied/blocked/next items.

Install as user services:

```bash
mkdir -p ~/.config/systemd/user
cp /dev-project/eimemory/deploy/systemd/eimemory-learn-*.service ~/.config/systemd/user/
cp /dev-project/eimemory/deploy/systemd/eimemory-learn-*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now eimemory-learn-watch.timer eimemory-learn-think.timer eimemory-learn-dashboard.timer
systemctl --user list-timers 'eimemory-learn-*.timer'
```
