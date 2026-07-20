# OpenClaw Python Environment Isolation Design

## Problem

The production OpenClaw gateway drop-in exports a hard-coded Python 3.13
`PYTHONPATH`, while the immutable eimemory 1.9.76 release uses Python 3.14.
OpenClaw child processes therefore import eimemory from a different package
surface than the one captured by the release-bound `recall_version_truth`
acceptance probe. The fail-closed L5 validator correctly reports
`probe_execution_evidence_mismatch`, leaving `memory.recall` at 2/3 and the
system at L4.5.

The same production database and replay manifest report L5 when executed from
a clean shell. That clean-shell result is not authoritative because it does not
reproduce the gateway environment.

## Decision

The OpenClaw gateway service will explicitly remove ambient Python import
overrides:

```ini
UnsetEnvironment=PYTHONPATH PYTHONHOME VIRTUAL_ENV
```

The gateway will continue to invoke eimemory only through the absolute
immutable-release virtual-environment commands already present in the drop-in:

```text
/opt/eimemory/current/.venv/bin/eimemory openclaw-hook
/opt/eimemory/current/.venv/bin/eimemory ei-bridge feishu
```

The eimemory RPC service keeps its intentional `PYTHONPATH=/opt/eimemory/current`
because it executes the release interpreter with `python -m` and its deployment
receipt independently verifies that import surface.

## Safety Properties

- Do not weaken or normalize away `recall_version_truth` source-path evidence.
- Do not hard-code another Python minor-version site-packages directory.
- Do not depend on the systemd user manager's inherited environment.
- Preserve absolute release-bound hook and bridge commands.
- Fail deployment verification if the effective OpenClaw gateway process still
  contains `PYTHONPATH`, `PYTHONHOME`, or `VIRTUAL_ENV`.

## Verification

1. A regression test requires the gateway drop-in to unset all three ambient
   Python environment variables and forbids an explicit `PYTHONPATH` assignment.
2. Deployment-tool tests verify the immutable installer still installs the
   managed drop-in and restarts the gateway.
3. Production process inspection verifies the restarted gateway environment is
   free of the three variables.
4. Core acceptance replay is regenerated for the new release.
5. `l5-readiness` is executed from a child process with the effective OpenClaw
   gateway environment; it must report L5, score 1.0, and `memory.recall` 3/3.
6. Release identity, deployment receipt, RPC health, supervisor health, and Git
   master must all identify the same commit and version.

## Release Scope

This is a patch release. The version advances from 1.9.76 to 1.9.77 only after
the regression and deployment tests pass. The release is committed to and
pushed on `master`, deployed from the authoritative honxin repository, and
announced through the configured Feishu completion channel after live closure.
