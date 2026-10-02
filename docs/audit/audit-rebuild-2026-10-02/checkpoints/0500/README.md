# Source coverage checkpoint · 2026-10-02 05:00 UTC

Baseline: 4763001d1c4f3f4af6e6dda17e008e1b4c9b5609, version 1.14.31.

First pass 811/7140 and independent paired 811/7140, both 11.36%. [Checkpoint](checkpoint.json) preserves all 30 module groups including zero-reviewed modules. This is baseline source-function accounting, not a runtime or full-suite pass.

## Exact public evidence

The public coverage manifest uses repository-relative evidence links and neutral reviewer labels. Source paths, hashes, graph IDs, function boundaries and pass states are identical to the sealed source snapshot. Original and normalized artifact hashes are distinct in [evidence-index.json](evidence-index.json).

The original snapshot fingerprint is retained for traceability; published parts reconstruct the public-normalized bytes, whose SHA-256 is 3fee8a339455eb097ea77de8c0263db566df7ffe2b76c08acebda9bfd1c7431a.

Publication proceeds in bounded commits. An index alone does not mean all data is present. Check publication-receipt.json for completion; until it exists, some listed parts/evidence may still be pending.

## Reassemble and verify

The 16 UTF-8 segments in [the shard index](manifest-shards/index.json) are not standalone JSON. Concatenate their bytes in the listed order, checking each part's length and SHA-256, then verify the complete 907789-byte result and its SHA-256 against the index before parsing it. Do not sort lexicographically instead of respecting the explicit index order.

Published evidence has no machine-specific absolute paths or private task labels. Normalization does not add audit coverage or test cases. Counts and test evidence remain separate, and no merge/deployment is implied.
