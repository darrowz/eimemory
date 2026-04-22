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
/opt/eimemory/venv
/var/lib/eimemory
/etc/eimemory
```

## Nightly Intake Timer

The standard production schedule runs active knowledge intake and governance once
per day at 03:30 in the server's local timezone.

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
