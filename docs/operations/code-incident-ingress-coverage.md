# Incident ingress chronology

Native maintenance checkpoints and incident observations provide a separate,
bounded diagnostic for SQLite records and events received after an actual
checkpoint. They do not establish global novelty, authorize repairs, start the
eight-hour observation, or satisfy the original L5 completion gates.

The original product definition remains in
`docs/superpowers/specs/2026-08-27-production-data-and-code-evolution-closure-design.md`
and `docs/operations/l5-native-maintenance.md`. The global
`known_before_detection`, `prior_user_reported`, and `manual_bootstrap` facts
remain unknown when absence cannot be proved. Existing adverse `true` facts
remain adverse. A diagnostic's `covered_*` fields describe only its explicit
SQLite domain; `qualifies_for_product_completion` is always false.

## Continuous collection

The additive `code.incident.coverage.v1` migration installs triggers on the
existing records/events tables. Every INSERT, UPDATE (old and new versions),
and DELETE records minimal version metadata and actual row scope in an
append-only sequence. It copies no record body or user phrase. Existing
records are not backfilled. Separate database connections use the same
triggers and SQLite transaction ordering.

Maintenance takes a checkpoint before running the real detectors. Closure
failure recording and maintenance observe only already persisted incidents;
the facade also verifies their existing native source contract. HMAC sealing
reuses the protected evidence receipt key and its key rotation verification
set. A plain digest is insufficient. Missing keys or unsupported stores yield
an unproven diagnostic while the original business write and repair gates
retain their own behavior. The L5 reader only reads persisted observations.

## Bounds and current verification

Daily checkpoints process the increment from their previous high water,
using indexed exact-scope and global capture-gap ranges. They do not scan
record/event bodies or the entire journal. A checkpoint and incident epoch
are bounded to 5,000 global sequence entries, 128 checkpoints, and a one-hour
checkpoint freshness interval. An exceeded bound, stale checkpoint, changed
producer, or repaired capture contract starts an explicit new epoch; it
never certifies the skipped interval. Incident observation and readback
recheck every signed physical interval within that bounded epoch, including
prefix holes, metadata changes, parent signatures, and the new unsigned
increment. Recall budgets are unchanged.

Schema readiness checks exact trigger and index definitions on existing
databases too. Index repair preserves evidence; repaired capture or
append-only triggers append a visible `CAPTURE_GAP`. Missing or altered
signed history remains unproven after schema repair. Ordinary ingress fails
its transaction if its journal insert fails, preserving atomic collection.

Earlier source timestamps arriving after detection, pre-anchor data, changed
incident versions, unavailable segmented payloads, and unclassified/free text
cannot prove absence. They yield unknown rather than being filtered out.
Actual sequence and database receive time determine ordering; caller event
time is only an invalidation signal. SQLite receive time has millisecond
precision, recorded explicitly in observations.

## Limits and operations

PostgreSQL and external knowledge/report channels are explicitly uncovered,
whether PostgreSQL recall is enabled or disabled. This mechanism does not
claim all-backend coverage or infer meaning from free text. Pre-epoch
knowledge remains unknown. No historical incident facts are rewritten.

The additive `code.incident.coverage.archive.v1` migration installs separate
append-only archive manifests and exact-scope key indices. Startup only
installs this schema: it never scans, backfills or prunes existing history.
Native maintenance archives old prefixes before taking a checkpoint. Its
default hot limits are 10,000 journal rows and 512 checkpoint/observation rows
each. A maintenance call moves at most 16 batches of 256 rows per table, so
an existing backlog drains gradually rather than holding an unbounded write
transaction. The separate archive report exposes actual hot rows and backlog.

Raw metadata is preserved in private compressed payload segments beside the
SQLite database. Each frame is fsynced and independently read back before its
HMAC manifest, parent digest, exact sequence range and lookup keys are committed
with pruning in one SQLite transaction. The original delete guard is restored
inside that same transaction. A crash or failed readback rolls back the SQL
prune; an uncommitted orphan frame is never accepted as evidence. Ordinary
UPDATE/DELETE on both hot evidence and archive indices remain blocked.

Historical readers verify original signatures and exact raw rows across hot
and cold ranges, including physical first-INSERT versions. They never reseal
an old observation or reset its sequence, source time, epoch or unknown global
facts. Reads are bounded to 64 frames per range and maintenance to 8 MiB per
raw frame. Missing frames, changed signatures, gaps and excessive read spans
produce unknown. Late or unclassified input retains its original invalidating
effect after archival.

Compression bounds the hot SQL footprint; preserving raw evidence still
requires growing cold storage and small indices. Maintenance stops before
8 GiB of segments, 256 segment files, or insufficient disk space, reports
`incident_archive_capacity_requires_export`, and preserves the original SQL
prefix. This limit requires an operator-reviewed export and readback path;
there is no automatic expiry or silent deletion. Archive bytes and manifests
belong in the database backup set. Manual deletion, truncation or moving cold
segments without their verified pointers invalidates proof.
