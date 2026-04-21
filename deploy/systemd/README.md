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

