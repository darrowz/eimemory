# eimemory 1.9.24 L5 Live Evidence Closure Plan

- [x] Add dashboard tests that reject rehearsal, missing verifier evidence,
  forged evidence ids, and untrusted sources; keep verified failures in the
  denominator.
- [x] Add readiness tests proving `persist=False` is mutation-free and that L5
  requires sufficient verified live task evidence and task-type diversity.
- [x] Add live acceptance tests for ten current-deployment-bound, idempotent,
  read-only tasks and fail-closed identity checks.
- [x] Add installer behavior tests for dynamic service discovery,
  deduplication, symlink rejection, and non-regular unit rejection.
- [x] Implement strict live evidence metrics, L5 gate/report fields, pure-read
  readiness, Runtime/CLI live acceptance, and any installer hardening exposed
  by the behavior tests.
- [x] Run only focused and associated test layers, compileall with redirected
  cache, `git diff --check`, release metadata checks, and independent review.
- [ ] Bump 1.9.23 to 1.9.24, commit, push `master`, fast-forward the canonical
  honxin checkout, run remote focused tests, deploy the exact immutable release,
  restart services, record deployment receipt, execute live acceptance, and
  verify L5 plus health/commit/version/release identity.
