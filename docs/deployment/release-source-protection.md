# Release source protection: honrui incident, 2026-09-28

The `7b73dc44` deployment returned installer success but its controller rejected
the source tree. Read-only comparison found 168 extra `.pyc` files, with no
tracked-file content or executable-mode changes. Evidence was archived on the
host before cleanup, under
`/var/lib/eimemory/state/source-protection-20260928/before-protection.tar.gz`.

## Causes and repairs

* The old installer ran `python -I -B -m compileall` inside the staged source
  tree. `-B` suppresses import caching, not explicit compilation; `-I` ignores
  the environment cache prefix. Commit `0f7861d9` replaces this with in-memory
  compilation and validates source checkpoints. Later cache file timestamps
  alone do not identify the process that rewrote them.
* On this Ubuntu host, AppArmor's `unprivileged_userns` profile denied
  `sys_admin` to `/usr/lib/systemd/systemd-executor`. A real transient service
  with `PrivateUsers=yes` and `ReadOnlyPaths` still observed a writable mount.
  Do not treat accepted unit settings as proof of effective protection.
* The renderer classified only names ending in `-gateway.service` as consumers.
  Named Hermes profiles were given protection for one release instead of the
  releases directory. They now receive the same protection as the main gateway.

The host-specific AppArmor profile at
`/etc/apparmor.d/eimemory-systemd-executor` uses the distribution's named
unconfined-profile pattern with `userns,` for the systemd executor. This permits
systemd namespace setup without changing the global unprivileged-userns sysctl.
It affects executor-launched services, not only eimemory; review that scope when
applying on another host. Do not disable AppArmor globally as a workaround.

After archiving caches and configuration, use the existing bounded
`clean_release_bytecode.py` helper, followed by `--validate-source` against the
exact commit. Render the common managed drop-in for every installed consumer
and job, reload systemd, and restart active consumers one at a time. Preserve
local drop-ins. Validate `ST_RDONLY` inside each running process's mount
namespace, RPC readiness, and gateway connectivity. A host-side filesystem
check does not prove the service's mount state.

On honrui, 17 managed drop-ins were refreshed. The RPC and all four Hermes
gateway processes subsequently observed read-only release mounts, and RPC
readiness passed. Rendering regression coverage includes all three named
profiles as well as the main gateway, RPC, and a job that must retain sibling
candidate-build access.

Source integrity and service readiness are technical evidence. Neither is a
substitute for naturally captured queries, reviewed relevance labels, or
release-bound real-task evidence required by business acceptance.
