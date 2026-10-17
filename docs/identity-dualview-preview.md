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

The remaining integration is the trusted human-request/governance adapter:
authenticate the actual requester and their exact owner/source authority,
durably bind that request to the manifest digest, both full versions and the
canonical exact view, and provide append-only conflict/revocation validation.
It must revalidate both versions and active authority in the transaction that
persists a decision, and on current-view consumption. An arbitrary reviewer,
`user_explicit` label, synthetic signature or deployment receipt is insufficient.
The existing governance record store supplies persistence, not that authority.
No adapter is invented here; production decisions, revocation persistence and
gate integration remain disabled until that authority contract is available.
Restoring an unresolved/revoked proposal needs no data rollback because preview
has no production effects. Revocation of a future durable decision must restore
the unresolved blocker while retaining both originals.
