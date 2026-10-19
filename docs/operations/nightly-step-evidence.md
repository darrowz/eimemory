# Nightly native step evidence

Nightly keeps the actual code maintenance result in a separate, exact-owner
`reflection` record with source `eimemory.scheduler.step_evidence`, status
`archived`, and schema `nightly_step_evidence.v1`. The record carries the real
nightly run ID, step name, scope and the release authority supplied by the
existing learning-record writer. It contains the complete JSON step report and
its canonical digest. Failed, processed, recovery-only and idle reports use the
same path. Unknown source facts remain JSON `null`; no SQL guard or missing
field is interpreted as a known fact.

The scheduler verifies exact-scope readback before publishing a reference.
Supervisor and independent command endings bind that same reference and verify
it again. The reference includes both the original report digest and the entire
envelope digest. Changing the report, record identity, scope, run, release or
status invalidates the original reference. Repeating the same run cannot replace
its original observation through the learning writer's existing idempotency
contract.

The complete record stays in the governed store. Public CLI/supervisor
diagnostics expose fixed operational fields and references, rather than the
report body, model prompts or provider exception text. `archive_complete` means
the original JSON report was retained without truncation. It does not mean the
repair succeeded, any terminal receipt exists, the observation period elapsed,
or L5 is qualified. `report_ok`, `report_execution_ok`, `report_status` and
`repair_complete` retain their separate meanings.

Native L5 exceptions carry an explicit fixed `failure_stage`, the actual bounded
exception class name and an allowlisted error code. Their actual failed result
is also archived under step `native_l5_assessment_failure`. If that archival
attempt fails, the original failure stage/type/code remain unchanged and the
reference is explicitly unverified. The code does not recurse or convert an
execution failure into an evidence wait. Successful and incomplete assessment
qualification rules are unchanged.

Historical compact summaries without these references remain historical
records. In particular, the failed 154 invocation
`a923827ce26845f78c440df58ea21605` discarded its maintenance subreport and the
native exception details. This change cannot reconstruct those missing facts
and never backfills a historical `idle` report. Its archived supervisor scope
was `default/hongrui/embodied/darrow`; it is not an observation of the separate
Hermes channel business scope.
