# Runtime Data Accumulation Activation Design

## Goal

Start collecting real OpenClaw/eimemory operating evidence immediately without
granting automatic candidate-to-canary promotion or bypassing the L5 48-hour
observation gate.

## Activation Boundaries

- Enable `eimemory-learn-watch.timer`, `eimemory-learn-think.timer`, and
  `eimemory-learn-dashboard.timer`.
- Enable both ordinary eimemory prompt injection and the live prompt bridge for
  OpenClaw by requiring the host policy and the two gateway environment flags.
- Keep automatic promotion disabled with
  `EIMEMORY_AUTONOMOUS_LEARNING_MAX_PROMOTIONS=0` and
  `EIMEMORY_L5_MAX_PROMOTIONS=0`.
- Start the L5 learning/evidence loop immediately with
  `EIMEMORY_L5_LOOP_APPLY=1`, while keeping its promotion budget at zero.
- Disable the legacy `eimemory-l5-observation-gate.timer` because its service
  enables autonomous code commit/deploy rather than merely observing results.
- Enable a one-shot `eimemory-l5-effect-review.timer` that writes a
  production-bound readiness report after 48 hours without persisting
  readiness, promoting candidates, or deploying code.
- Do not change application source, release version, deployed commit, L5 stage,
  or accumulated evidence.

## Host Changes

The OpenClaw policy remains in `/home/darrow/.openclaw/openclaw.json`. The
`eimemory-bridge` hook receives `allowPromptInjection=true`; unrelated JSON
fields are preserved and the previous file is backed up before an atomic
replacement.

Persistent user-systemd drop-ins carry the runtime switches:

- `openclaw-gateway.service.d/40-eimemory-prompt-bridge.conf`
  - `EIMEMORY_ENABLE_PROMPT_INJECTION=1`
  - `EIMEMORY_ENABLE_PROMPT_BRIDGE=1`
- `eimemory-nightly.service.d/zz-disable-auto-promotion.conf`
  - `EIMEMORY_AUTONOMOUS_LEARNING_MAX_PROMOTIONS=0`
  - `EIMEMORY_L5_MAX_PROMOTIONS=0`
- `eimemory-nightly.service.d/zz-l5-start-now.conf`
  - `EIMEMORY_L5_LOOP_ENABLED=1`
  - `EIMEMORY_L5_LOOP_APPLY=1`
  - `EIMEMORY_L5_MAX_PROMOTIONS=0`
- `eimemory-l5-effect-review.service` and `.timer`
  - run once after 48 hours
  - write `~/.openclaw/reports/l5-48h-effect.json`
  - do not call the legacy activation gate

## Verification and Rollback

Verification must show all three learn timers and the effect-review timer
enabled and active, the legacy activation gate disabled/inactive, the gateway
process carrying both bridge flags, the bridge status tool
reporting prompt injection and prompt bridge enabled, one successful real
OpenClaw prompt, one persisted L5 cycle with zero applied promotions, no failed
user units, and an unchanged deployment commit.

Rollback disables the three learn timers and effect-review timer, restores the
backed-up OpenClaw JSON, removes the three drop-ins and two effect-review units,
reloads user systemd, restarts the gateway, and re-enables the legacy L5 gate
only when explicitly requested. It does not delete accumulated data.
