# Identity dual-view preview

`eimemory identity manifest-preview --database /path/to/state/eimemory.sqlite --manifest /private/manifest.json`

This command opens `mode=ro`, enables `query_only`, and reads one transaction.
It never constructs Runtime, initializes schema, calls a model, changes either
record, persists a decision, or changes nightly's identity or formal gates.
Output contains counts, a manifest digest, fixed reasons and item ordinals;
record bodies, identities and submitted authorization fields are not printed.

The bounded JSON manifest has schema `identity_dualview_preview.v1`, a complete
`owner_scope` (tenant_id, agent_id, workspace_id, user_id), a `source_id` and
1–1000 `items`. Use separate manifests for different exact owner/source groups.
Each item contains:

- `source_ref` and `target_ref`: six-element arrays in tenant, agent, workspace,
  user, source_id, record_id order, matching existing identity-repair refs.
- `source_version` and `target_version`: the existing complete three-element
  snapshot tokens: stored envelope digest, all-column physical-row digest,
  storage key. These are not digests of a permissive model round trip.
- `decision`: `retain_both`, `unresolved` or `revoked`; `current`: `target`.

The owner scope is a proposed boundary, not authenticated ownership. Both sides
must have the same tenant, user, source partition, producer source and kind;
the existing identity normalization must map the source to the exact target.
Both active originals must match every supplied version component. Duplicate or
overlapping refs conflict for all affected entries. Historical exact refs keep
resolving the original scope and version. The preview's current view verifies
the original canonical target through the existing exact reader. General
`canonical_first` recall can fall back to a legacy union; it does not by itself
enforce an owner decision. No general recall behavior changes in this release.

Exit 1 means unresolved governance, including a structurally valid preview.
Exit 2 means malformed/unreadable input or database failure. `validation_ok`
only reports the data checks; `ok` and `authorization_verified` remain false,
`resolved_count` and `repaired_count` remain zero, and `unresolved_count` includes
every manifest entry. Counts describe the fixed manifest, not a fresh global
conflict inventory. Snapshot comparisons do not authorize a later write.

The required integration boundary is a trusted human-request/governance adapter:
authenticate the actual requester and their exact owner/source authority,
durably bind that request to the manifest digest, both full versions and the
canonical exact view, and provide append-only conflict/revocation validation.
It must revalidate both versions and active authority in the transaction that
persists a decision, and on current-view consumption. An arbitrary reviewer,
`user_explicit` label, synthetic signature or deployment receipt is insufficient.
The existing governance record store supplies persistence, not that authority.
The preview command itself never authorizes a production decision.
Restoring an unresolved/revoked proposal needs no data rollback because preview
has no production effects. Revocation of a future durable decision must restore
the unresolved blocker while retaining both originals.

## Protected local operator adapter (1.14.124)

An explicitly configured Unix deployment root operator can act for one exact
owner/source policy. This is a local administrator authority model, not an
assertion that an arbitrary service account, model, review label, signature or
release receipt is the human data owner. Root and the host filesystem are the
trusted boundary; deployments needing an independent identity provider must
supply that integration instead of assuming this model authenticates one.

`identity authority-init --owner-scope '<complete JSON scope>' --source-id
<partition> --service-group <runtime group>` creates
`/etc/eimemory-identity-governance` with root-only write access. Both it and its
ancestors must be protected from non-root modification. `--authority-dir` can
select another equally protected directory. The service reads the default or
`EIMEMORY_IDENTITY_AUTHORITY_DIR`; this setting cannot make a user-owned policy
trusted.

The authenticated root operator issues a request with `identity authorize
--database <sqlite> --manifest <manifest> --request-text <actual request>`.
The grant binds the exact policy, manifest, both complete versions and
canonical target. It rejects overlapping active requests. The root-owned
append-only journal records the actual effective Unix UID, request, event ID
and hash chain; no caller-supplied identity or synthetic signature authorizes it.

The runtime user runs `identity decision-apply --grant-id <id>`. A shared
authority lock remains held through the memory database transaction. Both
original versions are revalidated before a new governance record and its export
are committed. Original records, indexes, exact refs and payloads are untouched.
`identity decision-status --grant-id <id>` revalidates current state.

Nightly reports valid conflicts as `retained_both_current_canonical`, separately
from rewritten records. It also preserves the selected target's original
metadata, reporting `retained_current_original` if that row would otherwise
need an identity backfill. Canonical recall filters historical alternatives and
rechecks authority/versions at consumption; it never injects an unmatched target
or expands permission. Historical exact reads remain unchanged. An invalid,
stale, missing or revoked decision does not certify resolution.

The authenticated operator can append a revocation with `identity decision-revoke
--grant-id <id> --reason <reason>`. Exclusive journal locking serializes it with
decision commits and current-view checks. Revocation restores the unresolved
blocker without rolling back or deleting either original. A changed policy
also invalidates grants bound to its previous digest. The journal and policy
must be included in administrator-controlled host backups; ordinary memory
exports deliberately cannot manufacture or restore operator authority.

### Linux service user namespaces (1.14.126)

`PrivateUsers=yes` maps host root to an overflow UID. An overflow UID alone is
never accepted as root authority. Before a service in such a namespace can
consume requests, the initial-namespace root operator runs `identity
authority-seal`: the policy and authority directory receive the Linux immutable
inode flag, and the journal receives the append-only flag. Reads verify those
kernel flags on the actual open descriptors on a Linux ext filesystem. FUSE,
tmpfs and unsupported platforms fail closed. These flags require host
`CAP_LINUX_IMMUTABLE`; an unprivileged namespace or a caller-supplied UID cannot
manufacture the seal. See the [Linux inode flag contract](https://man7.org/linux/man-pages/man2/FS_IOC_SETFLAGS.2const.html).

Authorization and revocation append with `O_APPEND` while holding the journal
lock. `identity authority-unseal` is an explicit host-root maintenance operation;
namespace reads become unresolved while any required seal is absent. Host
backups must restore root ownership and reapply the seal before service use.
The service's user namespace and immutable source mounts remain enabled.
