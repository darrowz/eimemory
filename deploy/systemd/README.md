# systemd Templates

Immutable release installation trusts the deployment UID and all same-UID
processes as part of the deployment TCB. The installer locks the releases root
to its owner, rejects pre-existing links and other-UID ownership, and restores
the prior release on partial failure. A host that must defend against hostile
same-UID rename, injection, or ptrace activity must use a separate privileged
deployment account; that stronger isolation is outside the `darrow` deployment
model.

This directory contains service templates for production eimemory deployments.

Copy templates into the operator's user systemd scope and replace placeholder
values before enabling them. The production RPC owner is `systemctl --user`;
system-level `eimemory-rpc.service` ownership is unsupported.

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

The installer installs the RPC template under the user unit directory by
default. Use `/home/darrow/.config/systemd/user/eimemory-rpc.service` as the
single RPC owner. If an older system unit exists, disable it before starting the
user unit:

```bash
sudo systemctl disable --now eimemory-rpc.service
systemctl --user daemon-reload
systemctl --user enable --now eimemory-rpc.service
/opt/eimemory/current/deploy/check_user_systemd_owner.sh
```

Release closure waits for a real Feishu API receipt without polling. The
installer enables `eimemory-release-closure.path`, which watches the canonical
channel delivery ledger and starts the one-shot reconciler only when that file
changes and a release-closure checkpoint exists:

```bash
systemctl --user status eimemory-release-closure.path
systemctl --user status eimemory-release-closure.service
```

Runtime configuration is loaded from `/etc/eimemory/settings.json` when
`EIMEMORY_CONFIG_DIR=/etc/eimemory` is set. `EIMEMORY_CONFIG_PATH` can still
point at a specific settings file, and `EIMEMORY_ROOT` overrides the configured
root.

## Production Timer Set

The production schedule has a single governance owner. Install only these
timers unless a deployment document explicitly says otherwise:

| Timer | Purpose |
| --- | --- |
| `eimemory-nightly.timer` | Daily intake, governance, evaluation summaries, autonomous evolution, autonomous learning, and dashboards. |
| `eimemory-learn-watch.timer` | Capture real local/outcome/world signals every 15 minutes. |
| `eimemory-learn-think.timer` | Persist proactive thinking once per hour. |
| `eimemory-learn-dashboard.timer` | Refresh the operator dashboard daily at 03:45. |
| `eimemory-l5-effect-review.timer` | Capture one production-bound L5 readiness report after 48 hours. |

Do not install a standalone Karpathy-loop timer in production. The reusable
experiment helpers under `eimemory.autonomous` feed into the governance path;
they are not a second state owner.

The standard nightly schedule runs active knowledge intake and governance once
per day at 03:30 in the server's local timezone. L5 runs in apply mode so it
can persist world-model, roadmap, goal, assessment, and reward evidence.
Autonomous learning and L5 may each apply at most three promotions per cycle;
replay, isolated-evaluator, safety, canary, regression-watch, and rollback
gates remain mandatory.

Install as a user service for the OpenClaw/eimemory operator:

```bash
mkdir -p ~/.config/systemd/user
cp /dev-project/eimemory/deploy/systemd/eimemory-nightly.service ~/.config/systemd/user/
cp /dev-project/eimemory/deploy/systemd/eimemory-nightly.timer ~/.config/systemd/user/
cp /dev-project/eimemory/deploy/systemd/eimemory-learn-*.service ~/.config/systemd/user/
cp /dev-project/eimemory/deploy/systemd/eimemory-learn-*.timer ~/.config/systemd/user/
cp /dev-project/eimemory/deploy/systemd/eimemory-l5-effect-review.service ~/.config/systemd/user/
cp /dev-project/eimemory/deploy/systemd/eimemory-l5-effect-review.timer ~/.config/systemd/user/
cp /dev-project/eimemory/deploy/systemd/eimemory-l5-effect-review.sh ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now eimemory-nightly.timer
systemctl --user enable --now eimemory-learn-watch.timer eimemory-learn-think.timer eimemory-learn-dashboard.timer
systemctl --user enable --now eimemory-l5-effect-review.timer
systemctl --user list-timers eimemory-nightly.timer
```

Run one manual verification:

```bash
systemctl --user start eimemory-nightly.service
journalctl --user -u eimemory-nightly.service -n 100 --no-pager
```

## Managed Learning Timers

The immutable installer manages the lightweight observers alongside the single
nightly governance owner:

- `eimemory-learn-watch.timer`: every 15 minutes, capture lightweight local/outcome/world signals.
- `eimemory-learn-think.timer`: hourly, turn signals and long-term goals into persisted thoughts.
- `eimemory-learn-dashboard.timer`: daily at 03:45 local time, summarize learned/applied/blocked/next items.
- `eimemory-l5-effect-review.timer`: write one read-only, production-bound report to `~/.openclaw/reports/l5-48h-effect.json` after 48 hours.
- `eimemory-timer-monitor.timer`: every 5 minutes, alert when watch/think/nightly timers are masked, stale, inactive, or failed.

`eimemory-l5-observation-gate.timer` is a retired activation gate and is
disabled by deployment. It must not re-enable automatic commit/deploy behavior.

Run a helper manually only when debugging that path:

```bash
/opt/eimemory/current/.venv/bin/eimemory learn watch --persist
/opt/eimemory/current/.venv/bin/eimemory learn think --persist
/opt/eimemory/current/.venv/bin/eimemory learn dashboard --persist
/opt/eimemory/current/.venv/bin/eimemory ops timer-monitor --include-legacy-learning-timers
```
